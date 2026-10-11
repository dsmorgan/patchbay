"""Known cables (#79): stated cables remembered past the evidence TTL.

A pulled cable goes quiet: LLDP and UniFi stop reporting it, and its
`links` row expires at `normalize.EVIDENCE_TTL`. That is right for the map,
which should not draw a cable nobody reports, and wrong for the link-down
rule, which would clear while the cable is still out. This table keeps the
alerting half of the memory, so the map's lifecycle stays as it is.

Lifecycle, maintained by `record()` on every normalize:

    record    a stated link row (lldp, unifi, declared) upserts its cable;
              `last_up` refreshes while no end reports oper down
    hold      the row outlives its link row; link-down keeps evaluating it
    supersede another stated cable claims one of its ports: a port has
              one cable, so the old record goes
    retire    the operator says the cable is gone (`retire()`): the row is
              kept as a tombstone, so a link row the source still reports
              cannot bring the alert back; the cable is live again (and the
              tombstone lifted) once it is reported with no end down
    age out   not up for `retain_days` (link-down's parameter, default 30)
              and no longer reported: a decommissioned switch does not
              alert forever

Like `attention.py`, this imports no web framework: the poller calls it.
"""
from __future__ import annotations

import sqlite3

from .attention import (CABLE_RETAIN_DAYS, PORT_DOWN_STATES, STATED_LINK_SOURCES,
                        rule_params)

Ends = tuple[tuple[str, str], tuple[str, str]]


def ends_of(a_device: str, a_interface: str, b_device: str, b_interface: str) -> Ends:
    """One orientation per cable, the one upsert_link stores and the
    link-down key reads."""
    a, b = sorted([(a_device, a_interface), (b_device, b_interface)])
    return a, b


def retain_days(conn: sqlite3.Connection) -> float:
    try:
        days = float(rule_params(conn, "link-down",
                                 {"retain_days": CABLE_RETAIN_DAYS})["retain_days"])
    except (TypeError, ValueError):
        return CABLE_RETAIN_DAYS
    return days if days > 0 else CABLE_RETAIN_DAYS


def stated(conn: sqlite3.Connection) -> dict[Ends, set[str]]:
    """Every cable a current links row states, with the sources stating it."""
    out: dict[Ends, set[str]] = {}
    for r in conn.execute(
            "SELECT * FROM links WHERE source IN (%s)"
            % ",".join("?" * len(STATED_LINK_SOURCES)), STATED_LINK_SOURCES):
        ends = ends_of(r["a_device"], r["a_interface"], r["b_device"], r["b_interface"])
        out.setdefault(ends, set()).add(r["source"])
    return out


def _down_ports(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    return {(r["dev"], r["iface"]) for r in conn.execute(
        "SELECT d.name AS dev, i.name AS iface, i.oper_status FROM interfaces i "
        "JOIN devices d ON d.id = i.device_id")
        if (r["oper_status"] or "").lower() in PORT_DOWN_STATES}


def record(conn: sqlite3.Connection, now: float) -> None:
    """The normalize pass: record what is stated now, retire what another
    cable superseded, and age out what has been gone for retain_days."""
    current = stated(conn)
    down = _down_ports(conn)
    for (a, b) in current:
        up = None if a in down or b in down else now
        # a cable reported with no end down is live: last_up refreshes and
        # a retirement is lifted, because the cable is plainly back
        conn.execute(
            "INSERT INTO known_cables (a_device, a_interface, b_device, b_interface, "
            "first_seen, last_up) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (a_device, a_interface, b_device, b_interface) DO UPDATE SET "
            "last_up = COALESCE(excluded.last_up, last_up), "
            "retired_at = CASE WHEN excluded.last_up IS NULL THEN retired_at END",
            (*a, *b, now, up))
    # one cable per port: a record whose port a current cable claims (the
    # cable moved, or a '?' end was since resolved) describes nothing
    claimed = {e for ends in current for e in ends if e[1] != "?"}
    cutoff = now - retain_days(conn) * 86400
    for r in conn.execute("SELECT * FROM known_cables").fetchall():
        ends = ends_of(r["a_device"], r["a_interface"], r["b_device"], r["b_interface"])
        if ends in current:
            continue
        if any(e in claimed for e in ends if e[1] != "?") \
                or (r["last_up"] or r["first_seen"]) < cutoff:
            _delete(conn, ends)


def _delete(conn: sqlite3.Connection, ends: Ends) -> None:
    conn.execute("DELETE FROM known_cables WHERE a_device = ? AND a_interface = ? "
                 "AND b_device = ? AND b_interface = ?", (*ends[0], *ends[1]))


def rename(conn: sqlite3.Connection, resolve) -> None:
    """Follow device renames (normalize._rename_refs): rewrite each end
    through `resolve`, restore the sorted orientation, and fold a record
    into a twin that already holds the new name. The fold keeps the oldest
    first sighting, the newest up time, and a retirement only if both
    halves were retired."""
    for r in conn.execute("SELECT * FROM known_cables").fetchall():
        old = ends_of(r["a_device"], r["a_interface"], r["b_device"], r["b_interface"])
        new = ends_of(resolve(r["a_device"]), r["a_interface"],
                      resolve(r["b_device"]), r["b_interface"])
        if new == old:
            continue
        _delete(conn, old)
        twin = conn.execute(
            "SELECT * FROM known_cables WHERE a_device = ? AND a_interface = ? "
            "AND b_device = ? AND b_interface = ?", (*new[0], *new[1])).fetchone()
        first, up, retired = r["first_seen"], r["last_up"], r["retired_at"]
        if twin is not None:
            first = min(first, twin["first_seen"])
            up = max((x for x in (up, twin["last_up"]) if x is not None), default=None)
            retired = (max(retired, twin["retired_at"])
                       if retired is not None and twin["retired_at"] is not None else None)
            _delete(conn, new)
        conn.execute(
            "INSERT INTO known_cables (a_device, a_interface, b_device, b_interface, "
            "first_seen, last_up, retired_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (*new[0], *new[1], first, up, retired))


def retire(conn: sqlite3.Connection, ends: Ends, now: float) -> bool:
    """Mark a cable as removed on purpose. False when nothing states or
    remembers it. A declared cable is the declaration's to retire, so the
    caller refuses it before calling this."""
    known = conn.execute(
        "SELECT 1 FROM known_cables WHERE a_device = ? AND a_interface = ? "
        "AND b_device = ? AND b_interface = ?", (*ends[0], *ends[1])).fetchone()
    if known is None and ends not in stated(conn):
        return False
    conn.execute(
        "INSERT INTO known_cables (a_device, a_interface, b_device, b_interface, "
        "first_seen, retired_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (a_device, a_interface, b_device, b_interface) DO UPDATE SET "
        "retired_at = excluded.retired_at", (*ends[0], *ends[1], now, now))
    return True


def link_down_key(ends: Ends) -> str:
    return "link-down:" + ":".join(f"{d}:{i}" for d, i in ends)
