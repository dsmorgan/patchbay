"""The alert engine (ADR-0003, Decision 1): who hears about what is wrong.

`attention.py` stays the single source of what is wrong; this module turns
its items into stateful alerts. Each poll, `run()` diffs the current item
set against the `alerts` table and walks each key through the lifecycle:

    raise    key present now, absent before -> row inserted as `pending`
    active   held for the rule's `for` polls (default 1, so the same cycle)
    clear    key absent now, present before -> row marked cleared, kept
    event    a one-shot rule: raised, active and cleared in one cycle

Silences (#62, silences.py) add one state. An item a silence covers is
still stored, as `silenced`, and nothing is sent about it while it is:

    silence  pending or active -> silenced; no clear is sent, because the
             condition did not end. Kuma reads only `active`, so the
             monitor stops being held down on the same poll.
    hold     silenced and still covered -> stays silenced, text refreshed
    expire   silenced and no longer covered, condition still holds -> the
             same row raises as new: raised_at and the `for` count restart
             and the one notification is a `raise`, never a clear first
    end      silenced and the condition ends -> cleared; the clear is sent
             only if a raise was sent before the silence, so a receiver
             is never left firing and never hears of an unannounced alert

Disabling a rule closes its open alerts on the next poll, by the same
rule: one a receiver heard about gets a clear whose detail says "rule
disabled" (the condition may still hold), and Kuma, which follows state,
is released on the same push. Re-enabling it replays no event that
happened while it was off.

An item may carry `sample_ts`, the time of the sample it judged (the port
canary). Then a poll counts toward `for` only when the sample is newer than
the last one counted: one bad sample read on two polls is one look.

Every transition lands in `alert_events`, which is the History tab. What
the engine decides to send comes back as `Notification`s; the caller hands
them to `dispatch()` after the poll transaction commits, so an unreachable
receiver never holds the database. `transports.WebhookDispatcher` sends
them (#61); with no dispatcher given, they are dropped.

Like `attention.py`, this imports no web framework: the poller calls it.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from typing import Protocol

from . import db
from .attention import (CABLE_RETAIN_DAYS, CANARY_CLEAR, CANARY_FLOOR, CANARY_FOR,
                        CANARY_MULTIPLIER, CANARY_WARMUP_H, DEVICE_DOWN_ROLES,
                        DEVICE_STALE_S, DOWN_STATES, EVENT_WINDOW_S,
                        GATEWAY_LOSS_PCT, attention_items)
from .silences import describe

# History retention: a quarter covers "has this happened before?", and the
# count cap bounds a flapping rule that would otherwise fill 90 days.
EVENT_KEEP_DAYS = 90
EVENT_KEEP_MAX = 10_000


@dataclass(frozen=True)
class Rule:
    category: str
    event: bool = False        # one-shot: notify and clear in the same cycle
    params: dict = field(default_factory=lambda: {"for": 1, "remind": 0})
    route: str | None = None   # None = the default route; 'none' = nowhere
    summary: str = ""          # when it fires, for the Rules tab


# The catalog (ADR-0003 Decision 4): every rule attention_items() runs. A
# new rule is one attention function and one row here, with tests. Severity
# is the item's own (slow link is crit or warn by speed); alert_rules.severity
# overrides it per site. Parameter defaults beyond `for` are attention.py's
# constants, so the rule and its catalog row cannot disagree.
RULES: dict[str, Rule] = {
    # hypervisor is watched by default (#60): a host powered off on purpose
    # is a silence, and a host that died unannounced takes its guests with it
    "device-down": Rule(
        category="device", params={"for": 1, "remind": 0, "roles": list(DEVICE_DOWN_ROLES)},
        summary="A device in a watched role reports a down state, or has not "
                "been reported for two hours."),
    # a pulled cable keeps alerting after its link row expires (#79): the
    # known_cables record holds it until the port is up, the cable is
    # retired, or retain_days pass without it being seen up
    "link-down": Rule(
        category="link", params={"for": 1, "remind": 0, "retain_days": CABLE_RETAIN_DAYS},
        summary="A port on a stated cable (LLDP, UniFi, or declared) is down "
                "while its device is up. A cable between two infrastructure "
                "devices (switch, ap, firewall, router, hypervisor) still counts "
                "after nobody reports it, until its port is up again, it is "
                "retired from its alert, or retain_days pass without it being "
                "seen up. A cable to any other device clears when its report "
                "expires."),
    "gateway-degraded": Rule(
        category="gateway", params={"for": 1, "remind": 0, "loss": GATEWAY_LOSS_PCT},
        summary="Critical when the firewall reports a gateway down; warning "
                "when its packet loss exceeds the loss percentage."),
    # informs on the attention list but routes nowhere (ADR-0003 Decision
    # 4): a legitimately slow port is a silence, not a page, and a config
    # change is news, not an incident
    "slow-link": Rule(
        category="link", route="none",
        summary="A link runs at 100M or slower."),
    "config-changed": Rule(
        category="config", event=True, params={}, route="none",
        summary="A device's stored configuration changed."),
    "ipam-drift": Rule(
        category="ipam", summary="IPAM records and the network disagree."),
    "expected-tunnel-missing": Rule(
        category="tunnel", params={"for": 1, "remind": 0, "idle_is_down": True},
        summary="A tunnel declared on the Tunnels tab is absent, not up, or "
                "has not been reported for two hours. A WireGuard tunnel with a stale "
                "handshake (idle) counts as down unless idle_is_down is false."),
    "stale-source": Rule(
        category="source",
        summary="A data source has not reported for 15 minutes."),
    "snapshot-failed": Rule(
        category="source", event=True, params={},
        summary="The daily, a manual, or an alert snapshot failed, or was not "
                "delivered off-host."),
    # a port's error or discard rate orders of magnitude above its own
    # baseline (#64, ADR-0003 Decision 5); `for` 2 so one bad sample is quiet
    "port-canary": Rule(
        category="port",
        params={"for": CANARY_FOR, "remind": 0, "floor": CANARY_FLOOR,
                "multiplier": CANARY_MULTIPLIER, "clear": CANARY_CLEAR,
                "warmup_h": CANARY_WARMUP_H},
        summary="A port's error rate (discards: see #86) is at least multiplier × its "
                "own baseline (p95 of the past week, less the last hour) and "
                "at least floor per second; it clears below clear × baseline. "
                "A port with under warmup_h hours of samples is judged against "
                "the floor alone."),
    # an alert transport that failed three polls running (#61); routed by
    # default like any rule, which reaches the other transports when the
    # default is not the one that is failing
    "transport-failing": Rule(
        category="source",
        summary="An alert transport failed to deliver on three polls in a row."),
}

# A rule that is not in the catalog (a third-party or synthetic item) gets
# the plainest behavior rather than being dropped.
_DEFAULT_RULE = Rule(category="")

# Rules whose items an unreachable device would multiply: one unplugged
# switch is one alert, not one per cable (ADR-0003, fixed inhibition). The
# link-down rule names both cable ends in `ports` for this. A tunnel item
# is held while the firewall that terminates it is in down_devices (down or
# stale): the device alert already says so. A port canary names its one
# port, so a flood on a box that then dies holds, not clears.
_INHIBITED_BY_DOWN_DEVICE = {"link-down", "expected-tunnel-missing", "port-canary"}

# where record_first_seen kept its timestamps before the alerts table; read
# once so an upgrade does not reset every item's "for" to new
_LEGACY_FIRST_SEEN = "alert_first_seen"


@dataclass(frozen=True)
class Notification:
    kind: str                  # raise | escalate | remind | clear | event
    key: str
    rule: str
    severity: str
    text: str
    href: str
    raised_at: float
    route: str | None
    detail: str = ""           # why, when the kind alone does not say
    noted_at: float = 0.0      # the poll that decided it: delivery order


class Dispatcher(Protocol):
    def send(self, notes: list[Notification]) -> None: ...


class NullDispatcher:
    """Drops every notification. `transports.WebhookDispatcher` implements
    the same `send`."""

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


# Re-notification interval, in minutes, kept in a rule's params as `remind`
# (0 = never). A year is the ceiling the form accepts; the engine reads
# whatever is stored without trusting it (see `remind_minutes`).
REMIND_MAX_MIN = 525_600


def remind_minutes(raw) -> float | None:
    """The stored `remind` as minutes, or None for never. A missing, zero,
    negative, non-numeric, or non-finite value means never: a damaged row
    must not stop the engine (the `attention.rule_params` doctrine)."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        minutes = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return minutes if math.isfinite(minutes) and minutes > 0 else None


def _remind_shown(raw) -> int | float:
    """What the Rules tab shows for a stored value: whole minutes, or 0."""
    minutes = remind_minutes(raw)
    if minutes is None:
        return 0
    return int(minutes) if minutes == int(minutes) else minutes


SEVERITIES = ("crit", "warn", "info")
_RANK = {"info": 1, "warn": 2, "crit": 3}


def rule_settings(conn: sqlite3.Connection,
                  catalog: dict[str, Rule] = RULES) -> list[dict]:
    """What the Rules tab shows: each catalog rule with its stored enable,
    severity override, and route, and the parameters a site can edit (the
    catalog's own keys) at their effective values. Read-only: a rule with
    no stored row shows its catalog defaults (the poll and every save seed
    the rows), so a GET never needs SQLite's write lock (#109)."""
    config = _config(conn, catalog)
    out = []
    for name, rule in catalog.items():
        cfg = config.get(name, {})
        stored = cfg.get("params", {})
        out.append({
            "name": name, "category": rule.category, "event": rule.event,
            "summary": rule.summary,
            "enabled": cfg.get("enabled", True), "severity": cfg.get("severity"),
            "route": cfg.get("route", rule.route),
            "params": {k: (_remind_shown(stored.get(k, v)) if k == "remind"
                           else stored.get(k, v))
                       for k, v in rule.params.items()},
        })
    return out


def _parse_param(key: str, default, raw: str):
    """One form value, read as the type of the catalog default. The checks
    are the ones a rule needs to keep running: a role list that watches
    nothing is a disabled rule, which the enable box already says."""
    raw = raw.strip()
    if isinstance(default, list):
        roles = [r.strip().lower() for r in raw.split(",") if r.strip()]
        if not roles:
            raise ValueError(f"{key}: name at least one, or disable the rule")
        return roles
    if isinstance(default, bool):   # before int: bool subclasses int
        v = raw.lower()
        if v in ("1", "true", "on", "yes"):
            return True
        if v in ("0", "false", "off", "no"):
            return False
        raise ValueError(f"{key}: true or false")
    if key == "remind":
        try:
            n = int(raw)
        except ValueError:
            raise ValueError(f"{key}: whole minutes, 0 for never") from None
        if not 0 <= n <= REMIND_MAX_MIN:
            raise ValueError(f"{key}: 0 (never) to {REMIND_MAX_MIN} minutes")
        return n
    if isinstance(default, int):
        try:
            n = int(raw)
        except ValueError:
            raise ValueError(f"{key}: a whole number") from None
        if n < 1:
            raise ValueError(f"{key}: at least 1")
        return n
    if isinstance(default, float):
        try:
            x = float(raw.rstrip("% "))
        except ValueError:
            raise ValueError(f"{key}: a number") from None
        if not 0 <= x <= 100:
            raise ValueError(f"{key}: a percentage from 0 to 100")
        return x
    return raw


def update_rule(conn: sqlite3.Connection, name: str, form: dict[str, str],
                catalog: dict[str, Rule] = RULES) -> None:
    """Apply one Rules-tab form: enabled, severity override, and the rule's
    parameters. The route is not edited here (the Transports tab owns it),
    and stored keys the form does not carry are kept.
    Raises KeyError for an unknown rule and ValueError for a bad value,
    before writing anything."""
    rule = catalog[name]
    severity = (form.get("severity") or "").strip() or None
    if severity is not None and severity not in SEVERITIES:
        raise ValueError(f"severity: one of {', '.join(SEVERITIES)}, or the rule's own")
    parsed = {k: _parse_param(k, v, form[f"param_{k}"])
              for k, v in rule.params.items() if f"param_{k}" in form}
    seed_rules(conn, catalog)
    row = conn.execute("SELECT params FROM alert_rules WHERE name = ?", (name,)).fetchone()
    try:
        params = json.loads(row["params"] or "{}")
    except ValueError:
        params = {}
    if not isinstance(params, dict):
        params = {}
    params.update(parsed)
    # switching a rule on stamps the moment: an event that happened while it
    # was off is not replayed as news (attention._apply_rule_settings)
    enabled = 1 if form.get("enabled") else 0
    conn.execute("UPDATE alert_rules SET enabled_at = CASE WHEN ? = 1 AND enabled = 0 "
                 "THEN ? ELSE enabled_at END, enabled = ?, severity = ?, params = ? "
                 "WHERE name = ?",
                 (enabled, db.now(), enabled, severity, json.dumps(params), name))


def _config(conn: sqlite3.Connection, catalog: dict[str, Rule]) -> dict[str, dict]:
    """Effective per-rule settings: catalog defaults under the stored row."""
    out = {}
    for r in conn.execute("SELECT * FROM alert_rules"):
        try:
            params = json.loads(r["params"] or "{}")
        except ValueError:
            params = {}
        if not isinstance(params, dict):   # `null` or `[]`: damaged, not fatal
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
        "remind": remind_minutes(params.get("remind")),
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


def held_by(item: dict, down: set[str]) -> list[str]:
    """The down devices that hold `item` back, sorted: empty unless the
    rule is one an unreachable device would multiply. Items name their
    ports as `ports: [(device, interface), ...]`."""
    if item.get("rule") not in _INHIBITED_BY_DOWN_DEVICE:
        return []
    return sorted({dev for dev, _ in item.get("ports") or () if dev in down})


def inhibited(item: dict, down: set[str]) -> bool:
    """Fixed rule: a link-down item at a port of a down device says nothing
    the device's own alert does not."""
    return bool(held_by(item, down))


def held_text(item: dict, down: set[str]) -> str:
    """"held: sw1 is down" for an item the engine holds, else "": the tag the
    Active tab and Overview show, so the gap between their count and the
    rail badge (the engine's count) reads as a rule, not a mismatch."""
    devs = held_by(item, down)
    if not devs:
        return ""
    return f"held: {devs[0]} is down" if len(devs) == 1 else f"held: {', '.join(devs)} are down"


def _event(conn, alert_id, ts, event, a, detail=None):
    conn.execute(
        "INSERT INTO alert_events (alert_id, ts, event, key, rule, category, "
        "severity, text, href, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (alert_id, ts, event, a["key"], a["rule"], a["category"], a["severity"],
         a["text"], a["href"], detail))


def _note(kind: str, a, raised_at: float, route, now: float,
          detail: str = "") -> Notification:
    return Notification(kind=kind, key=a["key"], rule=a["rule"], severity=a["severity"],
                        text=a["text"] or "", href=a["href"] or "",
                        raised_at=raised_at, route=route, detail=detail,
                        noted_at=now)


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
        # two rows can yield one key (a link reported by two sources); the
        # second must not insert a second open alert and abort the cycle
        if key in seen or key in held:
            continue
        name = it.get("rule") or it["category"]
        rule = _rule_for(name, catalog, config)
        if not rule["enabled"]:
            continue
        silence = it.get("silenced")
        if silence:
            # checked before inhibition: an operator's silence must release
            # a Kuma monitor even while a down device holds the item
            seen.add(key)
            _silenced(conn, it, name, rule, open_.get(key), silence, now)
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
            # uniquely, and a repeat of a fired key is the same occurrence.
            # Dedupe reads `alerts`, whose cleared rows prune at 90 days, so
            # a key reused across occurrences would fire again after that.
            fired = conn.execute("SELECT id, active_at FROM alerts WHERE key = ?",
                                 (key,)).fetchone()
            if fired:
                if fired["active_at"] is None:
                    # silenced when it fired, since unsilenced: visible again,
                    # and never replayed as news. `active_at` is what marks it
                    # visible, so the rail badge follows the silence both ways.
                    conn.execute("UPDATE alerts SET active_at = ? WHERE id = ?",
                                 (now, fired["id"]))
                continue
            # it happened when it happened, not when this poll read it
            at = it.get("at") or now
            cur = conn.execute(
                "INSERT INTO alerts (key, rule, category, severity, state, polls, "
                "raised_at, active_at, cleared_at, last_notified_at, text, href) "
                "VALUES (?,?,?,?, 'cleared', 1, ?,?,?,?,?,?)",
                (key, name, a["category"], a["severity"], at, now, now, now,
                 a["text"], a["href"]))
            for ev in ("raised", "active", "cleared"):
                _event(conn, cur.lastrowid, now, ev, a)
            notes.append(_note("event", a, at, rule["route"], now))
            continue

        row = open_.get(key)
        if row is not None and row["state"] == "silenced":
            # the silence expired or was removed while the condition held:
            # it raises as new on the same row, so the receiver hears one
            # raise and no clear for a condition that never ended. A pending
            # re-raise keeps last_notified_at: a receiver that heard the
            # first raise must still hear the clear if it ends before `for`.
            state = "active" if rule["for"] <= 1 else "pending"
            conn.execute(
                "UPDATE alerts SET severity=?, text=?, href=?, polls=1, state=?, "
                "raised_at=?, active_at=?, last_notified_at=?, sample_ts=? WHERE id=?",
                (a["severity"], a["text"], a["href"], state, now,
                 now if state == "active" else None,
                 now if state == "active" else row["last_notified_at"],
                 it.get("sample_ts"), row["id"]))
            _event(conn, row["id"], now, "raised", a, "silence ended")
            if state == "active":
                _event(conn, row["id"], now, "active", a)
                notes.append(_note("raise", a, now, rule["route"], now))
            continue
        if row is None:
            raised = legacy.get(key, now)
            state = "active" if rule["for"] <= 1 else "pending"
            cur = conn.execute(
                "INSERT INTO alerts (key, rule, category, severity, state, polls, "
                "raised_at, active_at, last_notified_at, text, href, sample_ts) "
                "VALUES (?,?,?,?,?, 1, ?,?,?,?,?,?)",
                (key, name, a["category"], a["severity"], state, raised,
                 now if state == "active" else None,
                 now if state == "active" else None, a["text"], a["href"],
                 it.get("sample_ts")))
            _event(conn, cur.lastrowid, now, "raised", a)
            if state == "active":
                _event(conn, cur.lastrowid, now, "active", a)
                notes.append(_note("raise", a, raised, rule["route"], now))
            continue

        # still firing: refresh what the item says now (a stale source's
        # age, a slow link's tier) and count the poll toward `for`. An item
        # that names its sample counts only a sample newer than the last
        # one counted: the same sample read again is not a second poll.
        sample = it.get("sample_ts")
        fresh = sample is None or row["sample_ts"] is None or sample > row["sample_ts"]
        sample = sample if fresh else row["sample_ts"]
        polls = row["polls"] + (1 if fresh else 0)
        state, active_at, notified = row["state"], row["active_at"], row["last_notified_at"]
        if state == "pending" and polls >= rule["for"]:
            state, active_at, notified = "active", now, now
            _event(conn, row["id"], now, "active", a)
            notes.append(_note("raise", a, row["raised_at"], rule["route"], now))
        elif (state == "active"
              and _RANK.get(a["severity"], 0) > _RANK.get(row["severity"], 0)):
            # a rise (a lossy gateway going down) is news the receiver has
            # not had; a drop is not, and waits for the clear
            notified = now
            _event(conn, row["id"], now, "escalated", a,
                   f"{row['severity']} -> {a['severity']}")
            notes.append(_note("escalate", a, row["raised_at"], rule["route"], now))
        elif (state == "active" and rule["remind"]
              and now - (notified or active_at or now) >= rule["remind"] * 60):
            notified = now
            notes.append(_note("remind", a, row["raised_at"], rule["route"], now))
        conn.execute(
            "UPDATE alerts SET severity=?, text=?, href=?, polls=?, state=?, "
            "active_at=?, last_notified_at=?, sample_ts=? WHERE id=?",
            (a["severity"], a["text"], a["href"], polls, state, active_at,
             notified, sample, row["id"]))

    for key, row in open_.items():
        if key in seen or key in held:
            continue
        conn.execute("UPDATE alerts SET state='cleared', cleared_at=? WHERE id=?",
                     (now, row["id"]))
        rule = _rule_for(row["rule"], catalog, config)
        # An alert no receiver heard about (silenced or pending from the
        # start) clears as history, not news. One that was announced before
        # a silence, or before a pending re-raise, gets its clear: otherwise
        # a receiver that heard the raise would be left firing forever. A
        # disabled rule says nothing about the condition, which may still
        # hold, so its clear says why: "rule disabled", not fixed.
        announced = row["last_notified_at"] is not None
        if not announced:
            detail = ("rule disabled" if not rule["enabled"]
                      else "silenced" if row["state"] == "silenced" else "never active")
            _event(conn, row["id"], now, "cleared", row, detail)
            continue
        detail = "" if rule["enabled"] else "rule disabled"
        _event(conn, row["id"], now, "cleared", row, detail or None)
        notes.append(_note("clear", row, row["raised_at"], rule["route"], now, detail))

    if legacy:
        conn.execute("DELETE FROM app_state WHERE key = ?", (_LEGACY_FIRST_SEEN,))
    return notes


def _silenced(conn, it: dict, name: str, rule: dict, row, silence: dict,
              now: float) -> None:
    """One silenced item: stored and marked, never notified."""
    a = {"key": it["key"], "rule": name, "category": it["category"],
         "severity": rule["severity"] or it["severity"],
         "text": it.get("text"), "href": it.get("href")}
    why = describe(silence) + (f": {silence['reason']}" if silence.get("reason") else "")
    if rule["event"]:
        # a one-shot fires once whether or not anyone hears it, so a later
        # unsilence does not replay an occurrence that is already past
        fired = conn.execute("SELECT id, active_at FROM alerts WHERE key = ?",
                             (it["key"],)).fetchone()
        if fired:
            if fired["active_at"] is not None:   # silenced after it fired
                conn.execute("UPDATE alerts SET active_at = NULL WHERE id = ?",
                             (fired["id"],))
            return
        cur = conn.execute(
            "INSERT INTO alerts (key, rule, category, severity, state, polls, "
            "raised_at, cleared_at, text, href) VALUES (?,?,?,?, 'cleared', 1, ?,?,?,?)",
            (it["key"], name, a["category"], a["severity"], now, now, a["text"], a["href"]))
        _event(conn, cur.lastrowid, now, "silenced", a, why)
        _event(conn, cur.lastrowid, now, "cleared", a, "silenced")
        return
    if row is None:
        cur = conn.execute(
            "INSERT INTO alerts (key, rule, category, severity, state, polls, "
            "raised_at, text, href) VALUES (?,?,?,?, 'silenced', 1, ?,?,?)",
            (it["key"], name, a["category"], a["severity"], now, a["text"], a["href"]))
        _event(conn, cur.lastrowid, now, "silenced", a, why)
        return
    conn.execute("UPDATE alerts SET severity=?, text=?, href=?, polls=?, state='silenced' "
                 "WHERE id=?", (a["severity"], a["text"], a["href"], row["polls"] + 1,
                                row["id"]))
    if row["state"] != "silenced":
        _event(conn, row["id"], now, "silenced", a, why)


def clear_settled(conn: sqlite3.Connection, key: str, detail: str,
                  now: float | None = None,
                  catalog: dict[str, Rule] = RULES) -> list[Notification]:
    """Clear one open alert because the operator settled it (a retired
    cable, #79), not because the condition ended. The silence rule applies:
    an alert a receiver heard about (last_notified_at set) gets its clear,
    or a generic receiver would stay firing; one never announced clears as
    history only. Returns the notification due, for the caller to dispatch
    after commit; empty when nothing is open under `key` or nothing is due."""
    now = db.now() if now is None else now
    row = conn.execute("SELECT * FROM alerts WHERE key = ? AND state != 'cleared'",
                       (key,)).fetchone()
    if row is None:
        return []
    conn.execute("UPDATE alerts SET state='cleared', cleared_at=? WHERE id=?",
                 (now, row["id"]))
    _event(conn, row["id"], now, "cleared", row, detail)
    if row["last_notified_at"] is None:
        return []
    rule = _rule_for(row["rule"], catalog, _config(conn, catalog))
    return [_note("clear", row, row["raised_at"], rule["route"], now, detail)]


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
        items, _ = attention_items(conn, settings, now=now)
        notes = evaluate(conn, items, now=now)
        conn.execute("RELEASE alerting")
    except Exception:
        conn.execute("ROLLBACK TO alerting")
        conn.execute("RELEASE alerting")
        raise
    return notes


def dispatch(notes: list[Notification], dispatcher: Dispatcher | None = None) -> None:
    """Hand the cycle's notifications to the transports. Called after the
    poll transaction commits; route `none` never leaves the process. The
    dispatcher runs even when nothing is due: a Kuma transport pushes its
    state every cycle, which is what makes it a dead-man switch."""
    (dispatcher or NullDispatcher()).send([n for n in notes if n.route != "none"])


def open_count(conn: sqlite3.Connection, now: float | None = None) -> dict[str, int]:
    """The rail badge's number (#118): the engine's own rows, one cheap read
    of the `alerts` table — never an `attention_items` run, since the rail
    renders on every page. Returns {severity: n}, empty when nothing counts.
    It counts what the engine would page about, and lists what the Active
    tab does:

    - open alerts, pending and active;
    - one-shot events (config-changed, snapshot-failed) inside the tab's
      window, which the engine stores as already cleared. `evaluate` keeps
      `active_at` current with the silence state (NULL while silenced), and
      an event older than its rule's last switch-on is hidden, as
      `_apply_rule_settings` hides it.

    Silenced rows and rows of a rule switched off since the last poll are
    left out at once, and the severity override applies, as on the tab.

    What remains different, by design or by timing: items the engine holds
    back while their device is down (`held_by`) are on the tab, tagged
    "held", and never in the table; the table is as of the last poll, so a
    new item, a clear, or a silence shows on the tab first (and an event's
    silence state follows on the next poll); and an event whose source row
    was deleted stays counted until its 24-hour window ends.

    Two selects joined by UNION ALL: open rows come through the partial
    index on `state != 'cleared'`, events through the index on `raised_at`,
    so cleared history never costs a scan."""
    now = db.now() if now is None else now
    events = tuple(n for n, r in RULES.items() if r.event)
    marks = ",".join("?" * len(events))
    sev = "COALESCE(NULLIF(r.severity, ''), a.severity)"
    join = "FROM alerts a LEFT JOIN alert_rules r ON r.name = a.rule "
    return {r["sev"]: r["n"] for r in conn.execute(
        "SELECT sev, SUM(n) AS n FROM ("
        f" SELECT {sev} AS sev, COUNT(*) AS n {join}"
        "  WHERE a.state != 'cleared' AND a.state IN ('pending', 'active')"
        "  AND COALESCE(r.enabled, 1) = 1 GROUP BY sev"
        " UNION ALL"
        f" SELECT {sev} AS sev, COUNT(*) AS n {join}"
        f"  WHERE a.state = 'cleared' AND a.raised_at >= ? AND a.rule IN ({marks})"
        "  AND a.active_at IS NOT NULL AND a.raised_at >= COALESCE(r.enabled_at, 0)"
        "  AND COALESCE(r.enabled, 1) = 1 GROUP BY sev"
        ") GROUP BY sev", (now - EVENT_WINDOW_S, *events))}


def prune_history(conn: sqlite3.Connection, now: float | None = None) -> None:
    """Retention for the history: events older than EVENT_KEEP_DAYS go, then
    all but the newest EVENT_KEEP_MAX. Cleared alerts age out on the same
    window; an open alert is never pruned, however old. A silence that
    expired that long ago goes too: it no longer explains any history."""
    cutoff = (db.now() if now is None else now) - EVENT_KEEP_DAYS * 86400
    conn.execute("DELETE FROM alert_events WHERE ts < ?", (cutoff,))
    conn.execute(
        "DELETE FROM alert_events WHERE id NOT IN "
        "(SELECT id FROM alert_events ORDER BY ts DESC, id DESC LIMIT ?)",
        (EVENT_KEEP_MAX,))
    conn.execute("DELETE FROM alerts WHERE state = 'cleared' AND cleared_at < ?",
                 (cutoff,))
    conn.execute("DELETE FROM alert_silences WHERE until IS NOT NULL AND until < ?",
                 (cutoff,))
