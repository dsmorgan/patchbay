"""The alert engine (ADR-0003, Decision 1): who hears about what is wrong.

`attention.py` stays the single source of what is wrong; this module turns
its items into stateful alerts. Each poll, `run()` diffs the current item
set against the `alerts` table and walks each key through the lifecycle:

    raise    key present now, absent before -> row inserted as `pending`
    active   held for the rule's `for` polls (default 1, so the same cycle)
    clear    key absent now, present before -> row marked cleared, kept
    event    a one-shot rule: raised, active and cleared in one cycle

Every transition lands in `alert_events`, which is the History tab. What
the engine decides to send comes back as `Notification`s; the caller hands
them to `dispatch()` after the poll transaction commits, so an unreachable
receiver never holds the database. No transport exists yet (#61), so the
default dispatcher drops them.

Like `attention.py`, this imports no web framework: the poller calls it.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Protocol

from . import db
from .attention import DEVICE_STALE_S, DOWN_STATES, attention_items

# History retention: a quarter covers "has this happened before?", and the
# count cap bounds a flapping rule that would otherwise fill 90 days.
EVENT_KEEP_DAYS = 90
EVENT_KEEP_MAX = 10_000


@dataclass(frozen=True)
class Rule:
    category: str
    event: bool = False        # one-shot: notify and clear in the same cycle
    params: dict = field(default_factory=lambda: {"for": 1})
    route: str | None = None   # None = the default route; 'none' = nowhere


# The catalog: the rules that exist in attention_items() today. A new rule
# is one attention function and one row here, with tests. Severity is the
# item's own (slow link is crit or warn by speed); alert_rules.severity
# overrides it per site.
RULES: dict[str, Rule] = {
    # informs on the attention list but routes nowhere (ADR-0003 Decision
    # 4): a legitimately slow port is a silence, not a page
    "slow-link": Rule(category="link", route="none"),
    "ipam-drift": Rule(category="ipam"),
    "stale-source": Rule(category="source"),
}

# A rule that is not in the catalog (a third-party or synthetic item) gets
# the plainest behavior rather than being dropped.
_DEFAULT_RULE = Rule(category="")

# Rules whose items an unreachable device would multiply: one unplugged
# switch is one alert, not one per cable (ADR-0003, fixed inhibition).
_INHIBITED_BY_DOWN_DEVICE = {"link-down"}

# where record_first_seen kept its timestamps before the alerts table; read
# once so an upgrade does not reset every item's "for" to new
_LEGACY_FIRST_SEEN = "alert_first_seen"


@dataclass(frozen=True)
class Notification:
    kind: str                  # raise | remind | clear | event
    key: str
    rule: str
    severity: str
    text: str
    href: str
    raised_at: float
    route: str | None


class Dispatcher(Protocol):
    def send(self, notes: list[Notification]) -> None: ...


class NullDispatcher:
    """Drops every notification: what a site with no transport gets.
    Transports (#61) implement the same `send`."""

    def send(self, notes: list[Notification]) -> None:
        return None


def seed_rules(conn: sqlite3.Connection, catalog: dict[str, Rule] = RULES) -> None:
    """Insert catalog defaults for rules the table lacks. Existing rows are
    the site's edits and are never overwritten; a rule added in a later
    release appears on the next poll."""
    for name, rule in catalog.items():
        conn.execute(
            "INSERT OR IGNORE INTO alert_rules (name, params, route) VALUES (?, ?, ?)",
            (name, json.dumps(rule.params), rule.route))


def _config(conn: sqlite3.Connection, catalog: dict[str, Rule]) -> dict[str, dict]:
    """Effective per-rule settings: catalog defaults under the stored row."""
    out = {}
    for r in conn.execute("SELECT * FROM alert_rules"):
        try:
            params = json.loads(r["params"] or "{}")
        except ValueError:
            params = {}
        out[r["name"]] = {"enabled": bool(r["enabled"]), "severity": r["severity"],
                          "params": params, "route": r["route"]}
    return out


def _rule_for(name: str, catalog: dict[str, Rule], config: dict[str, dict]) -> dict:
    rule = catalog.get(name, _DEFAULT_RULE)
    cfg = config.get(name, {})
    params = {**rule.params, **cfg.get("params", {})}
    try:
        for_polls = max(1, int(params.get("for") or 1))
    except (TypeError, ValueError):
        for_polls = 1
    return {
        "enabled": cfg.get("enabled", True),
        "severity": cfg.get("severity"),
        "for": for_polls,
        "remind": params.get("remind"),
        "event": rule.event,
        "route": cfg.get("route", rule.route),
    }


def down_devices(conn: sqlite3.Connection, now: float) -> set[str]:
    """Devices that are not on the network: a down state, or stale. The same
    definition the rail's device totals use, so inhibition and the counts
    agree about which boxes are gone."""
    cutoff = now - DEVICE_STALE_S
    return {r["name"] for r in conn.execute("SELECT name, status, last_seen FROM devices")
            if (r["last_seen"] or 0) < cutoff
            or (r["status"] or "").lower() in DOWN_STATES}


def inhibited(item: dict, down: set[str]) -> bool:
    """Fixed rule: a link-down item at a port of a down device says nothing
    the device's own alert does not. Items name their ports as
    `ports: [(device, interface), ...]`."""
    return (item.get("rule") in _INHIBITED_BY_DOWN_DEVICE
            and any(dev in down for dev, _ in item.get("ports") or ()))


def _event(conn, alert_id, ts, event, a, detail=None):
    conn.execute(
        "INSERT INTO alert_events (alert_id, ts, event, key, rule, category, "
        "severity, text, href, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (alert_id, ts, event, a["key"], a["rule"], a["category"], a["severity"],
         a["text"], a["href"], detail))


def _note(kind: str, a, raised_at: float, route) -> Notification:
    return Notification(kind=kind, key=a["key"], rule=a["rule"], severity=a["severity"],
                        text=a["text"] or "", href=a["href"] or "",
                        raised_at=raised_at, route=route)


def evaluate(conn: sqlite3.Connection, items: list[dict], *,
             now: float | None = None,
             catalog: dict[str, Rule] = RULES) -> list[Notification]:
    """Diff `items` against the open alerts and apply the lifecycle. Returns
    the notifications due this cycle; nothing is sent here. The caller owns
    the transaction, the same contract as the collectors."""
    now = db.now() if now is None else now
    seed_rules(conn, catalog)
    config = _config(conn, catalog)
    down = down_devices(conn, now)
    legacy = _legacy_first_seen(conn)
    notes: list[Notification] = []

    open_ = {r["key"]: r for r in conn.execute(
        "SELECT * FROM alerts WHERE state != 'cleared'")}
    seen: set[str] = set()
    held: set[str] = set()

    for it in items:
        key = it["key"]
        name = it.get("rule") or it["category"]
        rule = _rule_for(name, catalog, config)
        if not rule["enabled"]:
            continue
        if inhibited(it, down):
            # neither raised nor cleared: when the device returns, a port
            # that is still down picks up where it was
            held.add(key)
            continue
        seen.add(key)
        a = {"key": key, "rule": name, "category": it["category"],
             "severity": rule["severity"] or it["severity"],
             "text": it.get("text"), "href": it.get("href")}

        if rule["event"]:
            # a one-shot key fires once; the rule keys each occurrence
            # uniquely, and a repeat of a fired key is the same occurrence
            if conn.execute("SELECT 1 FROM alerts WHERE key = ?", (key,)).fetchone():
                continue
            cur = conn.execute(
                "INSERT INTO alerts (key, rule, category, severity, state, polls, "
                "raised_at, active_at, cleared_at, last_notified_at, text, href) "
                "VALUES (?,?,?,?, 'cleared', 1, ?,?,?,?,?,?)",
                (key, name, a["category"], a["severity"], now, now, now, now,
                 a["text"], a["href"]))
            for ev in ("raised", "active", "cleared"):
                _event(conn, cur.lastrowid, now, ev, a)
            notes.append(_note("event", a, now, rule["route"]))
            continue

        row = open_.get(key)
        if row is None:
            raised = legacy.get(key, now)
            state = "active" if rule["for"] <= 1 else "pending"
            cur = conn.execute(
                "INSERT INTO alerts (key, rule, category, severity, state, polls, "
                "raised_at, active_at, last_notified_at, text, href) "
                "VALUES (?,?,?,?,?, 1, ?,?,?,?,?)",
                (key, name, a["category"], a["severity"], state, raised,
                 now if state == "active" else None,
                 now if state == "active" else None, a["text"], a["href"]))
            _event(conn, cur.lastrowid, now, "raised", a)
            if state == "active":
                _event(conn, cur.lastrowid, now, "active", a)
                notes.append(_note("raise", a, raised, rule["route"]))
            continue

        # still firing: refresh what the item says now (a stale source's
        # age, a slow link's tier) and count the poll toward `for`
        polls = row["polls"] + 1
        state, active_at, notified = row["state"], row["active_at"], row["last_notified_at"]
        if state == "pending" and polls >= rule["for"]:
            state, active_at, notified = "active", now, now
            _event(conn, row["id"], now, "active", a)
            notes.append(_note("raise", a, row["raised_at"], rule["route"]))
        elif (state == "active" and rule["remind"]
              and now - (notified or active_at or now) >= float(rule["remind"])):
            notified = now
            notes.append(_note("remind", a, row["raised_at"], rule["route"]))
        conn.execute(
            "UPDATE alerts SET severity=?, text=?, href=?, polls=?, state=?, "
            "active_at=?, last_notified_at=? WHERE id=?",
            (a["severity"], a["text"], a["href"], polls, state, active_at,
             notified, row["id"]))

    for key, row in open_.items():
        if key in seen or key in held:
            continue
        conn.execute("UPDATE alerts SET state='cleared', cleared_at=? WHERE id=?",
                     (now, row["id"]))
        # a pending alert that never went active was never announced, so
        # its clear is history only, not news
        _event(conn, row["id"], now, "cleared", row,
               None if row["state"] == "active" else "never active")
        if row["state"] == "active":
            rule = _rule_for(row["rule"], catalog, config)
            notes.append(_note("clear", row, row["raised_at"], rule["route"]))

    if legacy:
        conn.execute("DELETE FROM app_state WHERE key = ?", (_LEGACY_FIRST_SEEN,))
    return notes


def _legacy_first_seen(conn: sqlite3.Connection) -> dict[str, float]:
    raw = db.get_state(conn, _LEGACY_FIRST_SEEN)
    try:
        seen = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return seen if isinstance(seen, dict) else {}


def run(conn: sqlite3.Connection, settings, *, now: float | None = None) -> list[Notification]:
    """The poll-path entry: evaluate the attention rules and apply the
    lifecycle. A savepoint keeps a bug here from committing half a diff or
    discarding the poll's data; the caller commits and then dispatches."""
    conn.execute("SAVEPOINT alerting")
    try:
        items, _ = attention_items(conn, settings)
        notes = evaluate(conn, items, now=now)
        conn.execute("RELEASE alerting")
    except Exception:
        conn.execute("ROLLBACK TO alerting")
        conn.execute("RELEASE alerting")
        raise
    return notes


def dispatch(notes: list[Notification], dispatcher: Dispatcher | None = None) -> None:
    """Hand the cycle's notifications to the transports. Called after the
    poll transaction commits; route `none` never leaves the process."""
    notes = [n for n in notes if n.route != "none"]
    if notes:
        (dispatcher or NullDispatcher()).send(notes)


def prune_history(conn: sqlite3.Connection, now: float | None = None) -> None:
    """Retention for the history: events older than EVENT_KEEP_DAYS go, then
    all but the newest EVENT_KEEP_MAX. Cleared alerts age out on the same
    window; an open alert is never pruned, however old."""
    cutoff = (db.now() if now is None else now) - EVENT_KEEP_DAYS * 86400
    conn.execute("DELETE FROM alert_events WHERE ts < ?", (cutoff,))
    conn.execute(
        "DELETE FROM alert_events WHERE id NOT IN "
        "(SELECT id FROM alert_events ORDER BY ts DESC, id DESC LIMIT ?)",
        (EVENT_KEEP_MAX,))
    conn.execute("DELETE FROM alerts WHERE state = 'cleared' AND cleared_at < ?",
                 (cutoff,))
