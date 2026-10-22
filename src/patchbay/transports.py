"""Alert transports (ADR-0003 Decision 3, issue #61): who hears about it.

The primitive is an HTTP webhook. A preset shapes the request for a known
receiver, and v1 ships exactly two:

    kuma     Uptime Kuma push monitor. Stateful: every poll pushes `down`
             while any active alert routed to the transport meets the
             route's minimum severity, and `up` otherwise. Because the push
             happens every cycle, the Kuma monitor also catches a poller
             that stopped running (a dead-man switch).
    generic  Event-style: one JSON POST per raise, remind, clear, or event.

A route is a transport plus a minimum severity. `alert_rules.route` holds
one per rule in this format:

    NULL          the site's default route
    'none'        the attention list only; never dispatched
    '<id>:<sev>'  transport `id`, items at or above `sev` (info < warn < crit)

The default route is stored in app_state; until a site sets one it is "the
first transport, warn and above", so configuring one transport is enough.

A transport URL is a credential (Kuma's push token is in its path). It is
stored in the database, masked on every page, and never written to a log
line, an error message, an event, or a snapshot: every text that leaves
this module passes through `_redact`.

Dispatch runs after the poll transaction commits and holds no write
transaction while a request is in flight. A failed generic delivery waits
in an outbox and is retried next cycle; three consecutive failed cycles
raise a `source` item (attention.py). Every request has a wall-clock
limit, and the whole cycle has a time budget, so a slow receiver delays
the poll's exit by a bounded amount and never its data.

One dispatch runs at a time (a lease in app_state, #110). A transport's
notes go out in the order the polls decided them, and a raise whose alert
has cleared since is dropped, so a receiver never hears a stale raise
after its clear. An outcome the database will not take is kept beside it
and written, once, by the next dispatch.
"""
from __future__ import annotations

import dataclasses
import glob
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

import httpx

from . import alerting, db
from .attention import human_age

PRESETS = {"kuma": "Uptime Kuma push monitor", "generic": "generic webhook (JSON POST)"}
SEVERITIES = ("info", "warn", "crit")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# Per request: a receiver on the LAN answers in milliseconds, so five
# seconds is a dead receiver, not a slow one. REQUEST_S is a wall-clock
# limit on the whole exchange (see _request); TIMEOUT bounds each phase.
REQUEST_S = 5.0
TIMEOUT = httpx.Timeout(REQUEST_S)
# a receiver's reply is read for its status and a short snippet only; a
# receiver that answers with megabytes must not fill the poller's memory
BODY_MAX = 4096
# Per cycle: the poll runs every few minutes, so the transports together
# may delay its exit by at most this much. What did not fit waits for the
# next cycle (generic) or is simply pushed next cycle (kuma).
CYCLE_BUDGET_S = 20.0
# Each failed generic notification waits here; the cap bounds a receiver
# that is gone for days. The oldest drop first: the newest state matters.
OUTBOX_MAX = 50

DEFAULT_ROUTE_KEY = "alert_default_route"
LINK_BASE_KEY = "alert_link_base"
OUTBOX_KEY = "alert_outbox"

# Tests swap in an httpx.MockTransport here; production leaves it None.
HTTP_TRANSPORT: httpx.BaseTransport | None = None

_KUMA_MSG_MAX = 250   # Kuma shows msg in a table cell and in its own alerts


@dataclass(frozen=True)
class Route:
    transport_id: int
    min_severity: str = "warn"


# -- routes ---------------------------------------------------------------

def parse_route(raw: str | None) -> Route | None | str:
    """`'default'` for NULL, None for nowhere, else the Route. An unparseable
    value routes nowhere: a typo must not page someone unexpectedly."""
    if raw is None or raw == "":
        return "default"
    if raw == "none":
        return None
    tid, _, sev = raw.partition(":")
    if not tid.isdigit():
        return None
    sev = sev or "warn"
    return Route(int(tid), sev) if sev in _RANK else None


def format_route(route: Route | None) -> str:
    return "none" if route is None else f"{route.transport_id}:{route.min_severity}"


def default_route(conn: sqlite3.Connection) -> Route | None:
    """The site's default route: what it set, else the first transport by
    id at warn and above. Disabling that transport silences the default
    rather than quietly moving it to another receiver."""
    raw = db.get_state(conn, DEFAULT_ROUTE_KEY)
    if raw:
        got = parse_route(raw)
        return got if isinstance(got, Route) else None
    row = conn.execute("SELECT id FROM alert_transports ORDER BY id LIMIT 1").fetchone()
    return Route(row["id"]) if row else None


def resolve(raw: str | None, default: Route | None) -> Route | None:
    got = parse_route(raw)
    return default if got == "default" else got


def meets(severity: str | None, route: Route) -> bool:
    # an unknown severity counts as warn, the engine's everyday level
    return _RANK.get(severity or "", 1) >= _RANK[route.min_severity]


def rule_routes(conn: sqlite3.Connection) -> dict[str, str | None]:
    """Each rule's raw route: the stored row, else the catalog default."""
    out = {name: r.route for name, r in alerting.RULES.items()}
    for r in conn.execute("SELECT name, route FROM alert_rules"):
        out[r["name"]] = r["route"]
    return out


def route_label(conn: sqlite3.Connection, raw: str | None) -> str:
    """Human text for a stored route, for read-only display."""
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM alert_transports")}

    def one(route: Route | None) -> str:
        if route is None:
            return "nowhere"
        name = names.get(route.transport_id, f"missing transport {route.transport_id}")
        return f"{name}, {route.min_severity}+"

    got = parse_route(raw)
    if got == "default":
        return f"default ({one(default_route(conn))})"
    return one(got)


# -- secrets --------------------------------------------------------------

def mask_url(url: str) -> str:
    """Scheme and host only. The path and query carry the token, and the
    userinfo carries a password; none of them help an operator recognize
    which receiver this is."""
    try:
        u = httpx.URL(url)
        host = u.host + (f":{u.port}" if u.port else "")
        return f"{u.scheme}://{host}/…" if host else "…"
    except Exception:
        return "…"


# Kuma's own parameters: patchbay overwrites them on every push, so their
# values ("up", "OK") are not secrets, and redacting them would mangle
# ordinary words in an error message
_KUMA_PARAMS = {"status", "msg", "ping"}


def _secret_pieces(url: str, *, kind: str | None = None) -> set[str]:
    """Every substring of `url` that could carry a credential, raw and
    percent-decoded: the whole URL, the URL without its query, each query
    value, the username and password, and every path segment longer than
    one character."""
    from urllib.parse import unquote

    pieces = {url, unquote(url)}
    try:
        u = httpx.URL(url)
    except Exception:
        return {p for p in pieces if p}
    pieces |= {str(u), str(u.copy_with(query=None))}
    raw_path = u.raw_path.decode("ascii", "replace").split("?", 1)[0]
    for path in (raw_path, u.path):
        if len(path) > 1:
            pieces.add(path)
        # every segment, short ones included: a receiver may put a token
        # anywhere in the path, and an over-redacted error is still useful.
        # A single character carries no secret, and redacting one would
        # erase that letter from the whole message.
        pieces |= {seg for seg in path.split("/") if len(seg) >= 2}
    raw_query = u.query.decode("ascii", "replace")
    if raw_query:
        pieces |= {raw_query, unquote(raw_query)}
        for pair in raw_query.split("&"):
            key, _, value = pair.partition("=")
            if kind == "kuma" and unquote(key) in _KUMA_PARAMS:
                continue
            pieces |= {value, unquote(value), unquote(value.replace("+", " "))}
    if u.userinfo:
        info = u.userinfo.decode("ascii", "replace")
        pieces.add(info)
        for part in info.split(":", 1):
            pieces |= {part, unquote(part)}
    pieces |= {u.username, u.password or ""}
    return {p for p in pieces if p}


def _redact(text: str, url: str, *, kind: str | None = None,
            sent: str | None = None) -> str:
    """Remove every piece of `url` (and of `sent`, the exact URL a request
    went to, which differs for Kuma) that could carry a credential from a
    message bound for a log line, the page, or the history."""
    if not text:
        return text
    pieces = _secret_pieces(url, kind=kind)
    if sent:
        pieces |= _secret_pieces(sent, kind=kind)
    for p in sorted(pieces, key=len, reverse=True):
        text = _hex_insensitive(p).sub("<redacted>", text)
    return text


def _hex_insensitive(piece: str) -> "re.Pattern[str]":
    """A pattern for `piece` that matches %c3 and %C3 alike. Only the hex
    digits of a percent escape ignore case; the rest stays exact, so
    unrelated text is never altered."""
    parts = re.split(r"(%[0-9a-fA-F]{2})", piece)
    return re.compile("".join(
        "%" + "".join(f"[{c.lower()}{c.upper()}]" for c in part[1:])
        if i % 2 else re.escape(part)
        for i, part in enumerate(parts)))


def _error_text(e: Exception, url: str, *, kind: str | None = None,
                sent: str | None = None) -> str:
    detail = str(e).strip()
    return _redact(f"{type(e).__name__}: {detail}" if detail else type(e).__name__,
                   url, kind=kind, sent=sent)


def validate_url(url: str) -> str | None:
    """None when usable, else why not. Only http and https: the poller
    should never be talked into reading a local file or another scheme."""
    try:
        u = httpx.URL(url)
    except Exception:
        return "not a URL"
    if u.scheme not in ("http", "https") or not u.host:
        return "the URL must start with http:// or https:// and name a host"
    return None


# -- messages -------------------------------------------------------------

def _link(base: str | None, href: str | None) -> str:
    if not href:
        return ""
    return (base or "").rstrip("/") + href if base and href.startswith("/") else href


def _firing(raised_at: float, now: float) -> str:
    return human_age(max(0.0, now - raised_at) / 60)


def generic_payload(n: alerting.Notification, *, transport: str, base: str | None,
                    now: float, category: str = "") -> dict:
    """The generic preset's body: the full alert, plus the derived fields a
    receiver would otherwise recompute (an absolute link, seconds firing)."""
    return {
        "source": "patchbay", "event": n.kind, "transport": transport,
        "key": n.key, "rule": n.rule, "category": category, "severity": n.severity,
        "text": n.text, "href": n.href, "url": _link(base, n.href),
        "raised_at": n.raised_at, "firing_s": round(max(0.0, now - n.raised_at)),
        "sent_at": now,
        # why, when the event alone does not say: a clear for a disabled
        # rule ("rule disabled") or a retired cable; empty otherwise
        "detail": n.detail,
    }


def kuma_message(alerts: list, *, base: str | None, now: float) -> str:
    """One line for Kuma's msg: the count, then the worst, oldest alert with
    its severity, rule, text, how long it has fired, and the link."""
    if not alerts:
        return "OK"
    worst = sorted(alerts, key=lambda a: (-_RANK.get(a["severity"], 1), a["raised_at"]))[0]
    head = f"{len(alerts)} alert{'' if len(alerts) == 1 else 's'}: "
    body = (f"[{worst['severity']}] {worst['rule']}: {worst['text'] or worst['key']} "
            f"(for {_firing(worst['raised_at'], now)})")
    link = _link(base, worst["href"])
    msg = head + body + (f" {link}" if link else "")
    return msg if len(msg) <= _KUMA_MSG_MAX else msg[:_KUMA_MSG_MAX - 1] + "…"


def kuma_url(url: str, status: str, msg: str) -> httpx.URL:
    """Kuma's push URL, as copied from Kuma, already carries
    `?status=up&msg=OK&ping=`; replace those and keep anything else."""
    return (httpx.URL(url).copy_remove_param("ping")
            .copy_set_param("status", status).copy_set_param("msg", msg))


# -- sending ----------------------------------------------------------------

def _client(transport: httpx.BaseTransport | None) -> httpx.Client:
    # redirects are not followed: a receiver that moved is a configuration
    # to fix, and following one could carry the payload somewhere unexpected
    return httpx.Client(timeout=TIMEOUT, follow_redirects=False,
                        transport=transport or HTTP_TRANSPORT)


class _Deadline(Exception):
    """The response did not finish within the request's wall-clock limit."""


def _exchange(client: httpx.Client, t, status: str | None, msg: str,
              payload: dict | None, max_s: float, deadline: float) -> tuple[bool, str]:
    """The request itself; `_request` bounds it in wall-clock time."""
    sent = ""
    try:
        # building can fail for a stored URL that passed validate_url, so it
        # sits inside the try: one bad transport is one failed delivery
        if t["kind"] == "kuma":
            req = client.build_request("GET", kuma_url(t["url"], status or "up", msg),
                                       timeout=httpx.Timeout(max_s))
        else:
            req = client.build_request("POST", t["url"], json=payload,
                                       timeout=httpx.Timeout(max_s))
        sent = str(req.url)
        r = client.send(req, stream=True)
        try:
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) >= BODY_MAX:
                    break
                if time.monotonic() > deadline:
                    raise _Deadline(f"no complete response within {max_s:g} s")
        finally:
            r.close()
    except Exception as e:  # any failure is a failed delivery, never a crash
        return False, _error_text(e, t["url"], kind=t["kind"], sent=sent or None)
    text = body[:BODY_MAX].decode("utf-8", "replace")
    # redact the whole body first, then shorten: cutting first could leave
    # the front half of a token that no longer matches the full one
    clean = " ".join(_redact(text, t["url"], kind=t["kind"], sent=sent).split())
    snippet = clean[:120] + ("…" if len(clean) > 120 else "")
    result = f"HTTP {r.status_code}" + (f" {snippet}" if snippet else "")
    # Kuma answers 200 with {"ok": false, "msg": ...} for a bad token
    if r.is_success and not (t["kind"] == "kuma" and '"ok":false' in text.replace(" ", "")):
        return True, result
    return False, result


def _request(client: httpx.Client, t, *, status: str | None = None, msg: str = "",
             payload: dict | None = None, max_s: float = REQUEST_S) -> tuple[bool, str]:
    """One delivery. Returns (ok, result); the result is safe to show.

    httpx's timeout bounds each phase (connect, write, the wait for the
    headers, each body read), not the whole request, so phases add up and a
    receiver that trickles its body would hold the poll forever. The
    exchange runs in a worker thread that the caller waits on for `max_s`
    at most: past that it is a failed delivery, and the thread is left to
    end on its own phase timeouts (the request may still land; the retry
    makes delivery at-least-once). The body read also stops at the
    deadline and after BODY_MAX bytes."""
    max_s = max(0.1, min(REQUEST_S, max_s))
    deadline = time.monotonic() + max_s
    out: list[tuple[bool, str]] = []
    worker = threading.Thread(
        target=lambda: out.append(_exchange(client, t, status, msg, payload, max_s,
                                            deadline)),
        name="patchbay-notify", daemon=True)
    worker.start()
    worker.join(max_s)
    if out:
        return out[0]
    return False, f"_Deadline: no complete response within {max_s:g} s"


def kuma_alerts(conn: sqlite3.Connection, default: Route | None) -> dict[int, list]:
    """Active alerts per transport they hold down. Only `active` counts:
    pending has not held long enough, and a silenced alert (#62) has its
    own state, so a silence releases the monitor on the next push."""
    routes = rule_routes(conn)
    out: dict[int, list] = {}
    for a in conn.execute("SELECT * FROM alerts WHERE state = 'active' ORDER BY raised_at"):
        route = resolve(routes.get(a["rule"]), default)
        if route and meets(a["severity"], route):
            out.setdefault(route.transport_id, []).append(dict(a))
    return out


def drop_outbox(conn: sqlite3.Connection, tid: int) -> None:
    """Forget a transport's queued retries (on delete). The caller commits."""
    try:
        outbox = json.loads(db.get_state(conn, OUTBOX_KEY) or "[]")
    except ValueError:
        outbox = []
    kept = [e for e in outbox if isinstance(e, dict) and e.get("t") != tid] \
        if isinstance(outbox, list) else []
    db.set_state(conn, OUTBOX_KEY, json.dumps(kept))


def _note_dict(n: alerting.Notification) -> dict:
    return dataclasses.asdict(n)


def _note_from(d: dict) -> alerting.Notification | None:
    names = {f.name for f in dataclasses.fields(alerting.Notification)}
    try:
        return alerting.Notification(**{k: v for k, v in d.items() if k in names})
    except TypeError:
        return None


def _entry(tid: int, n: alerting.Notification) -> dict:
    return {"t": tid, "note": _note_dict(n)}


def _noted_at(e: dict) -> float:
    """An outbox entry's place in line: the poll that decided its note.
    An entry from before #110 carries none and sorts first, as the oldest."""
    note = e.get("note") if isinstance(e, dict) else None
    at = note.get("noted_at") if isinstance(note, dict) else None
    return at if isinstance(at, (int, float)) else 0.0


def _in_order(entries: list[dict], cap: int | None = None) -> list[dict]:
    """Each transport's entries in the order their notes arose, the newest
    `cap` of each when one is given. Two dispatches that overlap each store what they
    hold; arrival order in the list says nothing, so a clear stored before
    its raise would otherwise reach the receiver first and leave it
    firing. The sort is stable: one poll's notes keep their own order."""
    per: dict[int, list[dict]] = {}
    for e in entries:
        per.setdefault(e["t"], []).append(e)
    return [e for q in per.values() for e in sorted(q, key=_noted_at)[-(cap or len(q)):]]


def _remove_each(entries: list, gone: list) -> list:
    """`entries` less one copy of each item of `gone` (a multiset difference)."""
    left = list(entries)
    for g in gone:
        if g in left:
            left.remove(g)
    return left


def _load_outbox(conn: sqlite3.Connection) -> list[dict]:
    try:
        raw = json.loads(db.get_state(conn, OUTBOX_KEY) or "[]")
    except ValueError:
        return []
    return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


# -- the dispatch lease -------------------------------------------------------
# One dispatch at a time across processes (the poller and /ops/poll, or two
# web workers): two overlapping dispatches could each hold a note for the
# same alert and deliver the later one first. The lease is a row in
# app_state, claimed in a short write and never held across a request, so
# it works wherever SQLite does (a file lock is unreliable on Docker
# Desktop and NAS mounts). It expires, so a crashed holder delays the next
# dispatch rather than blocking it forever.
LEASE_KEY = "alert_dispatch_lease"
LEASE_TTL_S = 120.0
# How long a dispatch waits for another to finish: the holder's budget, one
# request, and its outcome write. Past it, the waiter queues its notes for
# the next cycle rather than race the holder.
LEASE_WAIT_S = 40.0
# Tries at the outcome write, each waiting out the connection's busy
# timeout; past them, the outcome is kept in a file for the next cycle.
OUTCOME_TRIES = 2
DB_TIMEOUT_S = 30.0


def _claim(conn: sqlite3.Connection, owner: str) -> bool:
    conn.execute("BEGIN IMMEDIATE")
    try:
        try:
            held = json.loads(db.get_state(conn, LEASE_KEY) or "null")
        except ValueError:
            held = None
        now = time.time()
        if (isinstance(held, dict) and held.get("owner") != owner
                and isinstance(held.get("until"), (int, float)) and held["until"] > now):
            conn.rollback()
            return False
        db.set_state(conn, LEASE_KEY, json.dumps({"owner": owner, "until": now + LEASE_TTL_S}))
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise


def _release(conn: sqlite3.Connection, owner: str) -> None:
    try:
        if conn.in_transaction:
            conn.rollback()
        conn.execute("BEGIN IMMEDIATE")
        try:
            held = json.loads(db.get_state(conn, LEASE_KEY) or "null")
        except ValueError:
            held = None
        if isinstance(held, dict) and held.get("owner") == owner:
            conn.execute("DELETE FROM app_state WHERE key = ?", (LEASE_KEY,))
        conn.commit()
    except sqlite3.Error:
        pass   # the lease expires on its own


# -- outcomes the database would not take ------------------------------------
# A cycle's outcome (transport results, history rows, the outbox) is written
# after its requests. If the database stays locked past OUTCOME_TRIES, the
# outcome goes to a file beside it instead, and the next dispatch, under
# the lease, folds it in before anything else: no queued note and no
# delivery record is lost. Each writer names its own file, so a waiter that
# never held the lease cannot overwrite the holder's.
#
# Replay is exactly once. Each file carries an `id`, and an outcome lists
# the ids it `folded` in. Writing an outcome stores those ids in app_state
# in the same transaction, and a file that folds others in names them too,
# so a crash before the old files are unlinked never writes their history
# rows or failure counts twice: a file whose id is already stored, or
# named by another file, is removed unread.
FOLDED_KEY = "alert_dispatch_folded"
FOLDED_KEEP = 200
# a temp file this old was left by a crash mid-write, not a writer at work
TMP_STALE_S = 3600


def _pending_paths(db_path: str) -> list[str]:
    return sorted(glob.glob(glob.escape(db_path) + "-dispatch-*.json"))


def _read_pending(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("id"), str) else None


def _write_pending(db_path: str, outcome: dict) -> str:
    """Write `outcome` (with a fresh `id`) to its own file, readable by the
    owner only. O_EXCL: never write through a stale or planted file."""
    oid = uuid.uuid4().hex
    path = f"{db_path}-dispatch-{time.time_ns()}-{os.getpid()}-{oid[:8]}.json"
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({**outcome, "id": oid}, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return path


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _sweep_tmp(db_path: str, now: float) -> None:
    for tmp in glob.glob(glob.escape(db_path) + "-dispatch-*.json.tmp"):
        try:
            if now - os.path.getmtime(tmp) > TMP_STALE_S:
                os.unlink(tmp)
        except OSError:
            pass


def _folded(conn: sqlite3.Connection) -> list[str]:
    try:
        ids = json.loads(db.get_state(conn, FOLDED_KEY) or "[]")
    except ValueError:
        return []
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []


class WebhookDispatcher:
    """The real dispatcher: `alerting.dispatch()` hands it the cycle's
    notifications after commit. It takes the dispatch lease, reads its
    configuration, makes every request with no transaction open, then
    records the outcome in one short write. `lines` holds the cycle's
    report for the poll output; it never contains a URL."""

    def __init__(self, db_path: str, *, transport: httpx.BaseTransport | None = None,
                 now: float | None = None, clock: Callable[[], float] = time.monotonic,
                 budget: float = CYCLE_BUDGET_S):
        self.db_path = db_path
        self.transport = transport
        self.now = now
        self.clock = clock
        self.budget = budget
        self.lines: list[str] = []

    def send(self, notes: list[alerting.Notification]) -> None:
        conn = sqlite3.connect(self.db_path, timeout=DB_TIMEOUT_S)
        conn.row_factory = sqlite3.Row
        owner = uuid.uuid4().hex
        try:
            if not conn.execute("SELECT 1 FROM alert_transports LIMIT 1").fetchone():
                return
            got = self._lease(conn, owner)
            if got != "ok":
                # fail closed: whether another dispatch holds the lease or
                # the database never answered, sending now could race a
                # holder and reorder a receiver's notes. This cycle's notes
                # queue behind it (in a file, if the database stays locked).
                self._defer(conn, notes, got)
                return
            try:
                self._cycle(conn, notes)
            finally:
                _release(conn, owner)
        finally:
            conn.close()

    def _lease(self, conn: sqlite3.Connection, owner: str) -> str:
        """"ok" when this dispatch holds the lease, "held" when another
        dispatch kept it for LEASE_WAIT_S, "locked" when the database never
        answered (each try waits out the busy timeout)."""
        waited_until = time.monotonic() + LEASE_WAIT_S
        answered = False
        while True:
            try:
                if _claim(conn, owner):
                    return "ok"
                answered = True
            except sqlite3.OperationalError:
                pass   # locked past the busy timeout: keep waiting
            if time.monotonic() >= waited_until:
                return "held" if answered else "locked"
            time.sleep(0.25)

    @staticmethod
    def _routed(notes, transports, default) -> list[tuple[int, alerting.Notification]]:
        """This cycle's notes, each with the transport its route names."""
        out = []
        for n in notes:
            route = resolve(n.route, default)
            if route and route.transport_id in transports and meets(n.severity, route):
                out.append((route.transport_id, n))
        return out

    def _defer(self, conn: sqlite3.Connection, notes: list[alerting.Notification],
               why: str) -> None:
        """No lease within LEASE_WAIT_S: queue this cycle's generic notes for
        the next lease holder, in order, rather than race one. Nothing is
        sent, and the shared outbox and outcome files are only added to.
        Kuma needs nothing queued; its state is pushed every cycle."""
        transports = {r["id"]: dict(r) for r in conn.execute(
            "SELECT * FROM alert_transports ORDER BY id")}
        mine = [_entry(tid, n) for tid, n in
                self._routed(notes, transports, default_route(conn))
                if transports[tid]["kind"] == "generic" and transports[tid]["enabled"]]
        cause = ("another dispatch is still running" if why == "held"
                 else "the database is busy")
        self.lines.append(f"[warn] notify: {cause}; "
                          f"{len(mine)} notification{'' if len(mine) == 1 else 's'} "
                          "queued for the next cycle")
        if mine:
            self._write({"updates": [], "events": [], "outbox": mine, "consumed": []},
                        pending=[])

    def _cycle(self, conn: sqlite3.Connection, notes: list[alerting.Notification]) -> None:
        now = db.now() if self.now is None else self.now
        transports = {r["id"]: dict(r) for r in conn.execute(
            "SELECT * FROM alert_transports ORDER BY id")}
        default = default_route(conn)
        base = db.get_state(conn, LINK_BASE_KEY)
        # an earlier cycle's outcome the database would not take: its
        # history and results are written with ours, the outbox entries it
        # had already handled are not handled twice, and its queue is ours.
        # Only the lease holder runs a cycle, so no two dispatches do this.
        pending = self._pending(conn)
        handled = [e for _, d in pending for e in d.get("consumed") or []]
        outbox = _load_outbox(conn)
        consumed = outbox + handled
        backlog = _remove_each(outbox, handled) + [
            e for _, d in pending for e in d.get("outbox") or [] if isinstance(e, dict)]
        held_down = kuma_alerts(conn, default)

        alert_rows: dict[str, tuple] = {}

        def alert_row(key: str) -> tuple:
            if key not in alert_rows:
                row = conn.execute("SELECT id, category, state FROM alerts WHERE key = ? "
                                   "ORDER BY id DESC LIMIT 1", (key,)).fetchone()
                alert_rows[key] = ((row["id"], row["category"], row["state"]) if row
                                   else (None, "", None))
            return alert_rows[key]

        # every note due, per transport, in the order the polls decided them.
        # Order within one queue is not enough: dispatches waiting on the
        # lease take it in no set order, so a later poll's clear can go out
        # first. A raise, escalate, or remind whose alert has cleared since
        # is therefore dropped, here under the lease, wherever it came from;
        # its clear (sent already, or queued) is the receiver's last word.
        events: list[tuple] = []
        entries: list[dict] = []
        for entry in backlog:
            n = _note_from(entry.get("note") or {})
            if not (n and entry.get("t") in transports):
                continue
            # silenced since it was queued (#62): the operator asked not to
            # hear about it, so the retry is dropped rather than sent late.
            # A queued clear still goes: it closes a raise the receiver had.
            if n.kind != "clear" and alert_row(n.key)[2] == "silenced":
                events.append((n, "dropped", "silenced"))
                continue
            entries.append(_entry(entry["t"], n))
        entries += [_entry(tid, n) for tid, n in
                    self._routed(notes, transports, default)]
        queued: dict[int, list[alerting.Notification]] = {}
        for e in _in_order(entries):
            n = _note_from(e["note"])
            state = alert_row(n.key)[2]
            if n.kind in ("raise", "escalate", "remind") and state == "cleared":
                events.append((n, "dropped", "cleared since"))
                continue
            queued.setdefault(e["t"], []).append(n)

        started = self.clock()
        updates: list[list] = []
        new_outbox: list[dict] = []

        def spent() -> bool:
            return self.clock() - started >= self.budget

        def left() -> float:
            return self.budget - (self.clock() - started)

        with _client(self.transport) as client:
            for tid, t in transports.items():
                mine = queued.get(tid, [])
                if not t["enabled"]:
                    continue   # a disabled transport neither sends nor queues
                if spent():
                    self.lines.append(f"[warn] notify {t['name']}: deferred, "
                                      f"the cycle's time budget is spent")
                    new_outbox += [_entry(tid, n) for n in mine if t["kind"] == "generic"]
                    continue
                if t["kind"] == "kuma":
                    down = held_down.get(tid, [])
                    status = "down" if down else "up"
                    ok, result = _request(client, t, status=status,
                                          msg=kuma_message(down, base=base, now=now),
                                          max_s=left())
                    updates.append([tid, ok, result])
                    # the push is the delivery of every state change routed
                    # here; one-shot events cannot hold a monitor down, so
                    # Kuma does not carry them
                    for n in mine:
                        if n.kind != "event":
                            events.append((n, "notified" if ok else "delivery_failed",
                                           f"{t['name']}: pushed {status}" if ok
                                           else f"{t['name']}: {result}"))
                    self.lines.append(f"[ok]   notify {t['name']}: {status}"
                                      + (f" ({len(down)} alert{'' if len(down) == 1 else 's'})"
                                         if down else "") if ok
                                      else f"[warn] notify {t['name']}: {result}")
                    continue
                sent, failed = 0, None
                for i, n in enumerate(mine):
                    if failed is not None or spent():
                        # the receiver just failed or time ran out: keep the
                        # rest for next cycle rather than wait out more timeouts
                        new_outbox += [_entry(tid, m) for m in mine[i:]]
                        break
                    ok, result = _request(client, t, payload=generic_payload(
                        n, transport=t["name"], base=base, now=now,
                        category=alert_rows[n.key][1]), max_s=left())
                    updates.append([tid, ok, result])
                    if ok:
                        sent += 1
                        events.append((n, "notified", t["name"]))
                    else:
                        failed = result
                        events.append((n, "delivery_failed", f"{t['name']}: {result}"))
                        new_outbox.append(_entry(tid, n))
                if failed is not None:
                    self.lines.append(f"[warn] notify {t['name']}: {failed}; "
                                      f"retrying next cycle")
                elif sent:
                    self.lines.append(f"[ok]   notify {t['name']}: {sent} sent")

        outcome = {
            "updates": [u for _, d in pending for u in d.get("updates") or []]
                       + [[tid, ok, result, now] for tid, ok, result in updates],
            "events": [ev for _, d in pending for ev in d.get("events") or []] + [
                [alert_rows[n.key][0], now, event, n.key, n.rule, alert_rows[n.key][1] or "",
                 n.severity, n.text, n.href, detail] for n, event, detail in events],
            "outbox": new_outbox,
            "consumed": consumed,
            "folded": [i for _, d in pending for i in [d["id"], *(d.get("folded") or [])]],
        }
        self._write(outcome, pending=[p for p, _ in pending])

    def _pending(self, conn: sqlite3.Connection) -> list[tuple[str, dict]]:
        """The outcome files to fold in: unreadable ones are reported and
        removed, and one already written (its id stored, or folded into
        another file) is removed unread."""
        _sweep_tmp(self.db_path, time.time())
        read = []
        for p in _pending_paths(self.db_path):
            d = _read_pending(p)
            if d is None:
                self.lines.append(f"[warn] notify: unreadable held-over outcome "
                                  f"{os.path.basename(p)} removed")
                _unlink(p)
                continue
            read.append((p, d))
        done = set(_folded(conn)) | {i for _, d in read for i in d.get("folded") or []}
        out = []
        for p, d in read:
            if d["id"] in done:
                _unlink(p)
            else:
                out.append((p, d))
        return out

    def _write(self, outcome: dict, *, pending: list[str]) -> None:
        """Record one outcome in a short write, or keep it in a file for the
        next cycle when the database stays locked. Files it folded in are
        removed only once their content is stored, here or in the new file."""
        conn = sqlite3.connect(self.db_path, timeout=DB_TIMEOUT_S)
        conn.row_factory = sqlite3.Row
        try:
            for attempt in range(OUTCOME_TRIES):
                try:
                    self._record(conn, outcome)
                    break
                except sqlite3.OperationalError:
                    if conn.in_transaction:
                        conn.rollback()
                    if attempt + 1 < OUTCOME_TRIES:
                        time.sleep(1.0)
            else:
                _write_pending(self.db_path, outcome)
                self.lines.append("[warn] notify: the database stayed locked; "
                                  "delivery records and retries are kept for "
                                  "the next cycle")
        finally:
            conn.close()
        for p in pending:
            _unlink(p)

    def _record(self, conn: sqlite3.Connection, outcome: dict) -> None:
        # BEGIN IMMEDIATE first: a dispatch that queued notes behind the
        # lease (or an older peer) may have stored some while our requests
        # were in flight, and the outbox is merged under the write lock so
        # neither side's notes are lost (#80)
        conn.execute("BEGIN IMMEDIATE")
        for tid, ok, result, ts in outcome["updates"]:
            if ok:
                conn.execute("UPDATE alert_transports SET last_sent_at=?, last_result=?, "
                             "last_error=NULL, failures=0 WHERE id=?", (ts, result, tid))
            else:
                conn.execute("UPDATE alert_transports SET last_error=?, "
                             "failures=failures+1 WHERE id=?", (result, tid))
        conn.executemany(
            "INSERT INTO alert_events (alert_id, ts, event, key, rule, category, "
            "severity, text, href, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [tuple(ev) for ev in outcome["events"]])
        # a transport deleted while this cycle's requests were in flight
        # must not leave queued notes for a later transport to inherit
        alive = {r[0] for r in conn.execute("SELECT id FROM alert_transports")}
        ours = [e for e in outcome["outbox"] if e.get("t") in alive]
        # the entries this cycle read are handled (retried, dropped, or
        # re-queued above); whatever else is stored now was queued by
        # another dispatch meanwhile and stays
        others = [e for e in _remove_each(_load_outbox(conn), outcome["consumed"])
                  if e.get("t") in alive and e not in ours]
        db.set_state(conn, OUTBOX_KEY, json.dumps(_in_order(ours + others, OUTBOX_MAX)))
        if outcome.get("folded"):
            # in the same transaction: a file folded here is never replayed
            db.set_state(conn, FOLDED_KEY, json.dumps(
                (_folded(conn) + outcome["folded"])[-FOLDED_KEEP:]))
        conn.commit()


def send_test(conn: sqlite3.Connection, tid: int, *,
              transport: httpx.BaseTransport | None = None) -> tuple[bool, str]:
    """The Test button: one sample request, its result recorded on the
    transport. A Kuma test pushes the monitor's current state, so testing
    never flips a down monitor up."""
    t = conn.execute("SELECT * FROM alert_transports WHERE id = ?", (tid,)).fetchone()
    if t is None:
        return False, "no such transport"
    now = db.now()
    base = db.get_state(conn, LINK_BASE_KEY)
    with _client(transport) as client:
        if t["kind"] == "kuma":
            down = kuma_alerts(conn, default_route(conn)).get(tid, [])
            msg = kuma_message(down, base=base, now=now)
            ok, result = _request(client, t, status="down" if down else "up",
                                  msg=f"patchbay test: {msg}")
        else:
            sample = alerting.Notification(
                kind="test", key="test:patchbay", rule="test", severity="info",
                text="patchbay test notification", href="/alerts?tab=transports",
                raised_at=now, route=None)
            ok, result = _request(client, t, payload=generic_payload(
                sample, transport=t["name"], base=base, now=now))
    if ok:
        conn.execute("UPDATE alert_transports SET last_sent_at=?, last_result=?, "
                     "last_error=NULL, failures=0 WHERE id=?", (now, f"test: {result}", tid))
    else:
        conn.execute("UPDATE alert_transports SET last_error=? WHERE id=?",
                     (f"test: {result}", tid))
    return ok, result


def page_context(conn: sqlite3.Connection) -> dict:
    """What the Transports tab renders. URLs leave here masked only."""
    def when(ts):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else None

    transports = [{
        "id": r["id"], "name": r["name"], "kind": r["kind"], "enabled": bool(r["enabled"]),
        "masked": mask_url(r["url"]), "last_sent": when(r["last_sent_at"]),
        "last_result": r["last_result"], "last_error": r["last_error"],
        "failures": r["failures"],
    } for r in conn.execute("SELECT * FROM alert_transports ORDER BY id")]
    choices = [("none", "nowhere")] + [
        (f"{t['id']}:{s}", f"{t['name']}, {s}+") for t in transports for s in SEVERITIES]
    routes = rule_routes(conn)
    return {
        "transports": transports, "presets": PRESETS,
        "route_choices": choices,
        "default_route_raw": db.get_state(conn, DEFAULT_ROUTE_KEY) or "",
        "default_route_label": route_label(conn, None),
        "rule_routes": [{"name": name, "raw": raw or "", "label": route_label(conn, raw)}
                        for name, raw in sorted(routes.items())],
        "link_base": db.get_state(conn, LINK_BASE_KEY) or "",
    }
