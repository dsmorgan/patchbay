"""Expected tunnels (issue #63, ADR-0003 Decision 4): the VPN tunnels a site
declares it needs up. Stored as one JSON list in `app_state` under
`expected_tunnels` rather than a table: it is a small, rarely edited
declaration (like the /ops declarations, which also live in app_state), it
needs no joins, and it adds no schema for the alerting branches to collide
on. Each entry is {device, type, name}. Never key material, not even public
keys: the validators below accept only those three fields.

A declaration matches the device name literally. It does not follow
renames or aliases: re-declare the tunnel after a device is renamed.
"""

from __future__ import annotations

import json
import sqlite3

from . import db

STATE_KEY = "expected_tunnels"
TYPES = ("wireguard", "openvpn", "ipsec")
MAX_FIELD = 128


def load(conn: sqlite3.Connection) -> list[dict]:
    """The declared tunnels, sorted. Unreadable state means none declared."""
    try:
        data = json.loads(db.get_state(conn, STATE_KEY) or "[]")
    except ValueError:
        return []
    out = []
    for e in data if isinstance(data, list) else []:
        if isinstance(e, dict) and all(isinstance(e.get(k), str) for k in ("device", "type", "name")):
            out.append({k: e[k] for k in ("device", "type", "name")})
    return sorted(out, key=lambda e: (e["device"], e["type"], e["name"]))


def validate(device: str, type_: str, name: str) -> tuple[str, str, str]:
    """Normalize one declaration or raise ValueError, before any write."""
    device, type_, name = (device or "").strip(), (type_ or "").strip().lower(), (name or "").strip()
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
    device, type_, name = validate(device, type_, name)
    entries = load(conn)
    entry = {"device": device, "type": type_, "name": name}
    if entry not in entries:
        _save(conn, entries + [entry])


def remove(conn: sqlite3.Connection, device: str, type_: str, name: str) -> None:
    entry = {"device": device, "type": type_, "name": name}
    _save(conn, [e for e in load(conn) if e != entry])
