"""Silences (ADR-0003 Decision 6, issue #62): what is known and fine, and
until when.

A silence has a scope, an expiry, a reason, and who set it. The scope is
one of four kinds:

    device     every item about the device: a device-down item, a gateway
               by name, and every item with a port on the device
    port       `device:interface`: every item with that port among its ends
    category   every item in the category (`link`, `source`, ...)
    key        exactly one item, by its key

Items say what they are about in two optional fields: `devices` (names)
and `ports` (`(device, interface)` pairs). An item with neither, such as
the stale-source summary, matches only a category or key silence.

`apply()` marks items once, at the end of `attention_items()`, so every
surface reads the same answer: the Overview hides a silenced item, the
alert engine stores it as `silenced` and dispatches nothing for it, and
the /alerts page lists it on its own tab rather than not at all.

`PATCHBAY_EXPECT` is legacy input: each entry is a permanent, read-only
silence (a bare name is a device, `dev:iface` a port), shown in the same
list. It is edited where it always was, on /ops.

Like `attention.py`, this imports no web framework: the poller calls it.
"""
from __future__ import annotations

import sqlite3
import time

KINDS = {"device": "device", "port": "port", "category": "category", "key": "this alert"}

# The choices a silence form offers, in hours. 24 is the default: "I know"
# is a day's quiet with a reason (ADR-0003 Decision 9), not forever.
DURATIONS = [("1", "1 hour"), ("4", "4 hours"), ("24", "24 hours"),
             ("168", "7 days"), ("720", "30 days"), ("permanent", "permanent")]
DEFAULT_HOURS = "24"
# A free-form duration is accepted up to this many hours (five years), so a
# typo cannot overflow a timestamp; longer than that is "permanent".
MAX_HOURS = 5 * 366 * 24
SCOPE_MAX = 300
REASON_MAX = 500

ENV_SOURCE = "env"
ENV_REASON = "declared expected (PATCHBAY_EXPECT)"


def env_silences(settings) -> list[dict]:
    """PATCHBAY_EXPECT as silences. The id is a string, so no form can
    address one: an env-sourced row is read-only here, as env-won values
    are everywhere else."""
    out = []
    for spec in sorted(getattr(settings, "expected", None) or ()):
        out.append({
            "id": f"env:{spec}", "kind": "port" if ":" in spec else "device",
            "scope": spec, "until": None, "reason": ENV_REASON,
            "created_by": "PATCHBAY_EXPECT", "created_at": None,
            "source": ENV_SOURCE,
        })
    return out


def db_silences(conn: sqlite3.Connection) -> list[dict]:
    return [{**dict(r), "source": "db"} for r in conn.execute(
        "SELECT * FROM alert_silences ORDER BY created_at DESC, id DESC")]


def all_silences(conn: sqlite3.Connection, settings) -> list[dict]:
    """Every silence, live or expired, env-sourced first."""
    return env_silences(settings) + db_silences(conn)


def is_live(s: dict, now: float) -> bool:
    return s["until"] is None or s["until"] > now


def item_devices(item: dict) -> set[str]:
    return set(item.get("devices") or ()) | {d for d, _ in item.get("ports") or ()}


def covers(s: dict, item: dict) -> bool:
    kind, scope = s["kind"], s["scope"]
    if kind == "device":
        return scope in item_devices(item)
    if kind == "port":
        return scope in {f"{d}:{i}" for d, i in item.get("ports") or ()}
    if kind == "category":
        return item.get("category") == scope
    if kind == "key":
        return item.get("key") == scope
    return False


def _lasting(s: dict) -> float:
    return float("inf") if s["until"] is None else s["until"]


def apply(conn: sqlite3.Connection, items: list[dict], settings,
          now: float) -> None:
    """Mark each item `silenced` with the live silence that covers it, or
    None. When several cover one item, the longest-lasting one is shown,
    because that is the one that decides when the item returns."""
    live = [s for s in all_silences(conn, settings) if is_live(s, now)]
    for it in items:
        hits = [s for s in live if covers(s, it)]
        it["silenced"] = max(hits, key=_lasting) if hits else None


def scopes_for(item: dict) -> list[tuple[str, str]]:
    """The scopes a silence button offers for one item, narrowest first:
    the item itself, each port, each device, then its category."""
    out = [("key", item["key"])]
    # an empty interface marks a device-level hold (an expected tunnel's
    # inhibition), not a port anyone can silence
    out += [("port", f"{d}:{i}") for d, i in item.get("ports") or () if i]
    out += [("device", d) for d in sorted(item_devices(item))]
    out.append(("category", item["category"]))
    return list(dict.fromkeys(out))


def until_text(s: dict) -> str:
    if s["until"] is None:
        return "permanent"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(s["until"]))


def describe(s: dict) -> str:
    """One line for the scope, as the lists show it."""
    if s["kind"] == "key":
        return f"alert {s['scope']}"
    return f"{s['kind']} {s['scope']}"


def parse(form: dict[str, str], *, now: float, created_by: str,
          categories: set[str]) -> dict:
    """Validate one silence form into a row. Raises ValueError naming the
    field, before anything is written. An Active row's button posts one
    `target` of `kind:scope` (a select of the item's own scopes) in place
    of the two fields."""
    if form.get("target"):
        kind, _, scope = form["target"].partition(":")
        form = {**form, "kind": kind, "scope": scope}
    kind = (form.get("kind") or "").strip()
    if kind not in KINDS:
        raise ValueError(f"kind: one of {', '.join(KINDS)}")
    scope = (form.get("scope") or "").strip()
    if not scope:
        raise ValueError("scope: name what to silence")
    if len(scope) > SCOPE_MAX or not scope.isprintable():
        raise ValueError(f"scope: at most {SCOPE_MAX} printable characters")
    if kind == "port":
        dev, sep, iface = scope.partition(":")
        if not (sep and dev.strip() and iface.strip()):
            raise ValueError("scope: a port is device:interface")
        # the same normalization PATCHBAY_EXPECT gets, so `sw1 : 1/0/2`
        # matches the port the items name
        scope = f"{dev.strip()}:{iface.strip()}"
    elif kind == "device" and ":" in scope:
        raise ValueError("scope: a device is a bare name; use the port kind "
                         "for device:interface")
    elif kind == "category" and scope not in categories:
        raise ValueError(f"scope: a category, one of {', '.join(sorted(categories))}")

    raw = (form.get("hours") or DEFAULT_HOURS).strip().lower()
    if raw == "permanent":
        until = None
    else:
        try:
            hours = float(raw)
        except ValueError:
            raise ValueError("hours: a number of hours, or permanent") from None
        if not 0 < hours <= MAX_HOURS:
            raise ValueError(f"hours: more than 0 and at most {MAX_HOURS}, or permanent")
        until = now + hours * 3600

    reason = (form.get("reason") or "").strip()
    if len(reason) > REASON_MAX or not reason.replace("\t", " ").isprintable():
        raise ValueError(f"reason: at most {REASON_MAX} printable characters")
    return {"kind": kind, "scope": scope, "until": until, "reason": reason or None,
            "created_by": created_by, "created_at": now}


def add(conn: sqlite3.Connection, row: dict) -> int:
    cur = conn.execute(
        "INSERT INTO alert_silences (kind, scope, until, reason, created_by, created_at) "
        "VALUES (:kind, :scope, :until, :reason, :created_by, :created_at)", row)
    return cur.lastrowid


def delete(conn: sqlite3.Connection, sid: int) -> bool:
    return conn.execute("DELETE FROM alert_silences WHERE id = ?", (sid,)).rowcount > 0


def page_context(conn: sqlite3.Connection, settings, *, now: float | None = None) -> dict:
    """What the Silences tab renders: every silence, live first, expired
    after and marked so the template can mute them."""
    now = time.time() if now is None else now

    rows = []
    for s in all_silences(conn, settings):
        rows.append({**s, "live": is_live(s, now), "what": describe(s),
                     "until_text": until_text(s),
                     "created_text": time.strftime("%Y-%m-%d %H:%M",
                                                   time.localtime(s["created_at"]))
                                     if s["created_at"] else None})
    rows.sort(key=lambda s: not s["live"])     # stable: env first within each
    return {"silences": rows, "silence_kinds": KINDS, "durations": DURATIONS,
            "default_hours": DEFAULT_HOURS}
