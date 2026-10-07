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
raise a `source` item (attention.py). Every request has a timeout, and the
whole cycle has a time budget, so a slow receiver delays the poll's exit
by a bounded amount and never its data.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
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
        text = text.replace(p, "<redacted>")
    return text


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


def _request(client: httpx.Client, t, *, status: str | None = None, msg: str = "",
             payload: dict | None = None, max_s: float = REQUEST_S) -> tuple[bool, str]:
    """One delivery. Returns (ok, result); the result is safe to show.

    httpx's timeout bounds each phase (connect, each read), not the whole
    request, so a receiver that trickles its body a byte at a time would
    hold the poll forever. The request streams instead: every phase gets
    at most `max_s`, the body read stops at a monotonic deadline of `max_s`
    from the start, and at most BODY_MAX bytes are read."""
    max_s = max(0.1, min(REQUEST_S, max_s))
    deadline = time.monotonic() + max_s
    if t["kind"] == "kuma":
        req = client.build_request("GET", kuma_url(t["url"], status or "up", msg),
                                   timeout=httpx.Timeout(max_s))
    else:
        req = client.build_request("POST", t["url"], json=payload,
                                   timeout=httpx.Timeout(max_s))
    sent = str(req.url)
    try:
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
        return False, _error_text(e, t["url"], kind=t["kind"], sent=sent)
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


def kuma_alerts(conn: sqlite3.Connection, default: Route | None) -> dict[int, list]:
    """Active alerts per transport they hold down. Only `active` counts:
    pending has not held long enough, and a silenced alert (#62) is not
    news."""
    routes = rule_routes(conn)
    out: dict[int, list] = {}
    for a in conn.execute("SELECT * FROM alerts WHERE state = 'active' ORDER BY raised_at"):
        route = resolve(routes.get(a["rule"]), default)
        if route and meets(a["severity"], route):
            out.setdefault(route.transport_id, []).append(dict(a))
    return out


def _note_dict(n: alerting.Notification) -> dict:
    return dataclasses.asdict(n)


def _note_from(d: dict) -> alerting.Notification | None:
    names = {f.name for f in dataclasses.fields(alerting.Notification)}
    try:
        return alerting.Notification(**{k: v for k, v in d.items() if k in names})
    except TypeError:
        return None


class WebhookDispatcher:
    """The real dispatcher: `alerting.dispatch()` hands it the cycle's
    notifications after commit. It reads its configuration, makes every
    request with no transaction open, then records the outcome in one
    short write. `lines` holds the cycle's report for the poll output; it
    never contains a URL."""

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
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            self._cycle(conn, notes)
        finally:
            conn.close()

    def _cycle(self, conn: sqlite3.Connection, notes: list[alerting.Notification]) -> None:
        now = db.now() if self.now is None else self.now
        transports = {r["id"]: dict(r) for r in conn.execute(
            "SELECT * FROM alert_transports ORDER BY id")}
        if not transports:
            return
        default = default_route(conn)
        base = db.get_state(conn, LINK_BASE_KEY)
        try:
            outbox = json.loads(db.get_state(conn, OUTBOX_KEY) or "[]")
        except ValueError:
            outbox = []
        held_down = kuma_alerts(conn, default)

        # this cycle's notes, per transport, in the order they arose
        queued: dict[int, list[tuple[alerting.Notification, int]]] = {}
        for entry in outbox if isinstance(outbox, list) else []:
            n = _note_from(entry.get("note") or {})
            if n and entry.get("t") in transports:
                queued.setdefault(entry["t"], []).append((n, int(entry.get("tries") or 0)))
        for n in notes:
            route = resolve(n.route, default)
            if route and route.transport_id in transports and meets(n.severity, route):
                queued.setdefault(route.transport_id, []).append((n, 0))
        alert_rows = {}
        for n, _ in (x for q in queued.values() for x in q):
            row = conn.execute("SELECT id, category FROM alerts WHERE key = ? "
                               "ORDER BY id DESC LIMIT 1", (n.key,)).fetchone()
            alert_rows[n.key] = (row["id"], row["category"]) if row else (None, "")

        started = self.clock()
        updates: dict[int, dict] = {}
        events: list[tuple] = []
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
                    new_outbox += [{"t": tid, "note": _note_dict(n), "tries": k}
                                   for n, k in mine if t["kind"] == "generic"]
                    continue
                if t["kind"] == "kuma":
                    down = held_down.get(tid, [])
                    status = "down" if down else "up"
                    ok, result = _request(client, t, status=status,
                                          msg=kuma_message(down, base=base, now=now),
                                          max_s=left())
                    updates[tid] = {"ok": ok, "result": result}
                    # the push is the delivery of every state change routed
                    # here; one-shot events cannot hold a monitor down, so
                    # Kuma does not carry them
                    for n, _ in mine:
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
                for i, (n, tries) in enumerate(mine):
                    if failed is not None or spent():
                        # the receiver just failed or time ran out: keep the
                        # rest for next cycle rather than wait out more timeouts
                        new_outbox += [{"t": tid, "note": _note_dict(m), "tries": k}
                                       for m, k in mine[i:]]
                        break
                    ok, result = _request(client, t, payload=generic_payload(
                        n, transport=t["name"], base=base, now=now,
                        category=alert_rows[n.key][1]), max_s=left())
                    if ok:
                        sent += 1
                        events.append((n, "notified", t["name"]))
                        updates[tid] = {"ok": True, "result": result}
                    else:
                        failed = result
                        events.append((n, "delivery_failed", f"{t['name']}: {result}"))
                        updates[tid] = {"ok": False, "result": result}
                        new_outbox.append({"t": tid, "note": _note_dict(n), "tries": tries + 1})
                if failed is not None:
                    self.lines.append(f"[warn] notify {t['name']}: {failed}; "
                                      f"retrying next cycle")
                elif sent:
                    self.lines.append(f"[ok]   notify {t['name']}: {sent} sent")

        # the outcome lands in one short write, after every request is done
        for tid, u in updates.items():
            if u["ok"]:
                conn.execute("UPDATE alert_transports SET last_sent_at=?, last_result=?, "
                             "last_error=NULL, failures=0 WHERE id=?", (now, u["result"], tid))
            else:
                conn.execute("UPDATE alert_transports SET last_error=?, "
                             "failures=failures+1 WHERE id=?", (u["result"], tid))
        for n, event, detail in events:
            alert_id, category = alert_rows[n.key]
            conn.execute(
                "INSERT INTO alert_events (alert_id, ts, event, key, rule, category, "
                "severity, text, href, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (alert_id, now, event, n.key, n.rule, category or "", n.severity,
                 n.text, n.href, detail))
        # a transport deleted while this cycle's requests were in flight
        # must not leave queued notes for a later transport to inherit
        alive = {r[0] for r in conn.execute("SELECT id FROM alert_transports")}
        per: dict[int, list] = {}
        for e in (e for e in new_outbox if e["t"] in alive):
            per.setdefault(e["t"], []).append(e)
        kept = [e for q in per.values() for e in q[-OUTBOX_MAX:]]
        db.set_state(conn, OUTBOX_KEY, json.dumps(kept))
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
