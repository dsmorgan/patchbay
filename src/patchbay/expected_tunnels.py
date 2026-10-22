"""Expected tunnels (issue #63, ADR-0003 Decision 4): the VPN tunnels a site
declares it needs up. Stored as one JSON list in `app_state` under
`expected_tunnels` rather than a table: it is a small, rarely edited
declaration (like the /ops declarations, which also live in app_state), it
needs no joins, and it adds no schema for the alerting branches to collide
on. Each entry is {device, type, name}. Never key material, not even public
keys: the validators below accept only those three fields.

A declaration names its device the way every stored row does, by
`normalize.canonical_name` (lowercased, DNS domain dropped), so "FW1" and
"fw1.example.net" both declare a tunnel on "fw1" (#110). A name that is
an alias when the tunnel is declared is stored as the device it names.
Later renames and aliases are not followed: re-declare the tunnel after a
device is renamed. The one exception is a name patchbay itself retires
(#98: a firewall once named after its address's first octet), which
rename() carries over.
"""

from __future__ import annotations

import json
import sqlite3

from . import db

STATE_KEY = "expected_tunnels"
TYPES = ("wireguard", "openvpn", "ipsec")
MAX_FIELD = 128


def _canonical(device: str) -> str:
    from .normalize import canonical_name
    return canonical_name(device)


def load(conn: sqlite3.Connection) -> list[dict]:
    """The declared tunnels, sorted. Unreadable state means none declared.
    A device stored before #110 as typed reads as its canonical name."""
    try:
        data = json.loads(db.get_state(conn, STATE_KEY) or "[]")
    except ValueError:
        return []
    out = []
    for e in data if isinstance(data, list) else []:
        if isinstance(e, dict) and all(isinstance(e.get(k), str) for k in ("device", "type", "name")):
            entry = {"device": _canonical(e["device"]), "type": e["type"], "name": e["name"]}
            if entry not in out:
                out.append(entry)
    return sorted(out, key=lambda e: (e["device"], e["type"], e["name"]))


def validate(device: str, type_: str, name: str) -> tuple[str, str, str]:
    """Normalize one declaration or raise ValueError, before any write."""
    device, type_, name = (device or "").strip(), (type_ or "").strip().lower(), (name or "").strip()
    device = _canonical(device)
    if not device or len(device) > MAX_FIELD:
        raise ValueError("a device name of at most 128 characters is required")
    if type_ not in TYPES:
        raise ValueError(f"type must be one of: {', '.join(TYPES)}")
    if not name or len(name) > MAX_FIELD:
        raise ValueError("a tunnel name of at most 128 characters is required")
    return device, type_, name


def _save(conn: sqlite3.Connection, entries: list[dict]) -> None:
    db.set_state(conn, STATE_KEY, json.dumps(entries))


def add(conn: sqlite3.Connection, device: str, type_: str, name: str) -> None:
    raw = (device or "").strip()
    device, type_, name = validate(device, type_, name)
    alias = conn.execute("SELECT canonical FROM aliases WHERE alias IN (?, ?) "
                         "ORDER BY alias = ? DESC LIMIT 1", (raw, device, raw)).fetchone()
    if alias and alias[0]:
        device = alias[0]
    entries = load(conn)
    entry = {"device": device, "type": type_, "name": name}
    if entry not in entries:
        _save(conn, entries + [entry])


def remove(conn: sqlite3.Connection, device: str, type_: str, name: str) -> None:
    entry = {"device": _canonical(device), "type": type_, "name": name}
    _save(conn, [e for e in load(conn) if e != entry])


def rename(conn: sqlite3.Connection, old: str, new: str) -> None:
    """Move declarations from a device name patchbay retired to its heir
    (normalize.retire_name); one already declared under the new name wins."""
    entries = load(conn)
    if not any(e["device"] == old for e in entries):
        return
    moved = []
    for e in entries:
        e = {**e, "device": new} if e["device"] == old else e
        if e not in moved:
            moved.append(e)
    _save(conn, moved)
