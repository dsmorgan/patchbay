"""SQLite persistence for the normalized network model.

Collectors upsert by natural keys (device name, interface name, MAC, CIDR)
so repeated polls converge instead of duplicating. Raw API payloads are kept
in raw_payloads for debugging and for the snapshot generator.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    mgmt_ip TEXT,
    vendor TEXT,
    model TEXT,
    os TEXT,
    role TEXT,
    status TEXT,               -- up | down | disabled | unknown
    parent TEXT,               -- e.g. the ESXi host a VM runs on
    serial TEXT,               -- chassis serial (LLDP alias evidence)
    source TEXT NOT NULL,      -- collector that owns this record
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS aliases (
    alias TEXT PRIMARY KEY,    -- name some source used (fqdn, serial, ...)
    canonical TEXT NOT NULL    -- devices.name it resolves to
);
CREATE TABLE IF NOT EXISTS interfaces (
    id INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    ifindex INTEGER,
    admin_status TEXT,
    oper_status TEXT,
    speed_bps INTEGER,
    mac TEXT,
    description TEXT,
    last_seen REAL,
    UNIQUE (device_id, name)
);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY,
    a_device TEXT NOT NULL,
    a_interface TEXT NOT NULL,
    b_device TEXT NOT NULL,
    b_interface TEXT NOT NULL,
    source TEXT NOT NULL,      -- lldp | fdb-inference | vsphere-hint
    last_seen REAL,
    UNIQUE (a_device, a_interface, b_device, b_interface, source)
);
CREATE TABLE IF NOT EXISTS endpoints (
    id INTEGER PRIMARY KEY,
    mac TEXT NOT NULL UNIQUE,
    ip TEXT,
    hostname TEXT,
    device TEXT,               -- switch it was learned on
    interface TEXT,            -- port it was learned on
    vlan INTEGER,
    source TEXT NOT NULL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS subnets (
    id INTEGER PRIMARY KEY,
    cidr TEXT NOT NULL UNIQUE,
    description TEXT,
    vlan INTEGER,
    source TEXT NOT NULL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS vlans (
    id INTEGER PRIMARY KEY,
    vid INTEGER NOT NULL UNIQUE,
    name TEXT,
    source TEXT NOT NULL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS positions (
    name TEXT PRIMARY KEY,     -- topology node pinned by the user
    x REAL NOT NULL,
    y REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fdb (
    device TEXT NOT NULL,      -- switch that learned the MAC
    interface TEXT NOT NULL,   -- port it was learned on
    mac TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'librenms',
    PRIMARY KEY (device, interface, mac)
);
CREATE TABLE IF NOT EXISTS device_vlans (
    device TEXT NOT NULL,      -- device carrying the VLAN (Q-BRIDGE evidence)
    vid INTEGER NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (device, vid)
);
CREATE TABLE IF NOT EXISTS ipam_addresses (
    ip TEXT PRIMARY KEY,       -- full IPAM address book, incl. MAC-less
    hostname TEXT,             -- rows (endpoints only keeps MAC-keyed ones);
    mac TEXT,                  -- basis for the drift report
    subnet_cidr TEXT,
    description TEXT,
    state TEXT,                -- IPAM tag: offline | used | reserved | dhcp
    last_seen REAL,
    ipam_id INTEGER,           -- the IPAM's own object ids, for deep links
    ipam_subnet_id INTEGER,    -- back into its UI; null where the IPAM
    ipam_section_id INTEGER    -- doesn't provide them (link simply absent)
);
CREATE TABLE IF NOT EXISTS port_vlans (
    device TEXT NOT NULL,      -- per-port membership parsed from backed-up
    interface TEXT NOT NULL,   -- configs (oxidized collector); tagged=0 means
    vid INTEGER NOT NULL,      -- access/untagged on this port
    tagged INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    PRIMARY KEY (device, interface, vid)
);
CREATE TABLE IF NOT EXISTS port_roles (
    device TEXT NOT NULL,      -- special-purpose ports parsed from configs:
    interface TEXT NOT NULL,   -- 'monitor-dst' (SPAN/mirror destination) and
    role TEXT NOT NULL,        -- 'monitor-src'. A mirror destination carries
    detail TEXT,               -- copied traffic, so neither its MAC table nor
    source TEXT NOT NULL,      -- its neighbor protocols describe what it's
    PRIMARY KEY (device, interface, role)   -- cabled to.
);
CREATE TABLE IF NOT EXISTS vnic_vlans (
    mac TEXT PRIMARY KEY,      -- VM/vmkernel NIC MAC as the hypervisor reports
    vid INTEGER NOT NULL,      -- it; vid is its port group's VLAN (4095 = the
    portgroup TEXT,            -- guest tags its own frames). Resolved to a
    source TEXT NOT NULL       -- (device, interface) by the normalizer.
);
CREATE TABLE IF NOT EXISTS rate_history (
    device TEXT NOT NULL,      -- one sample per port per poll; feeds the
    interface TEXT NOT NULL,   -- topology load view's peak metric
    ts REAL NOT NULL,
    in_bps INTEGER,
    out_bps INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rate_hist ON rate_history (device, interface, ts);
CREATE TABLE IF NOT EXISTS gateways (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    address TEXT,
    status TEXT,
    loss TEXT,
    delay TEXT,
    source TEXT NOT NULL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS routes (
    device TEXT NOT NULL,      -- router that holds this route
    destination TEXT NOT NULL, -- CIDR ('default' normalized to 0.0.0.0/0, ::/0)
    gateway TEXT,
    interface TEXT,
    proto TEXT,                -- ipv4 | ipv6
    flags TEXT,                -- BSD route flags; B/R mean discard, not reach
    source TEXT NOT NULL,
    last_seen REAL,
    PRIMARY KEY (device, destination, proto)
);
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,      -- internal persisted state (e.g. the generated
    value TEXT NOT NULL        -- session-signing secret when none is configured)
);
CREATE TABLE IF NOT EXISTS raw_payloads (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_source_time ON raw_payloads (source, fetched_at);
"""


@contextmanager
def connect(path: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # lightweight migration: add columns that predate an existing db file
    cols = {r[1] for r in conn.execute("PRAGMA table_info(devices)")}
    if "serial" not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN serial TEXT")
    icols = {r[1] for r in conn.execute("PRAGMA table_info(interfaces)")}
    for col in ("in_bps", "out_bps"):
        if col not in icols:
            conn.execute(f"ALTER TABLE interfaces ADD COLUMN {col} INTEGER")
    acols = {r[1] for r in conn.execute("PRAGMA table_info(ipam_addresses)")}
    if "state" not in acols:
        conn.execute("ALTER TABLE ipam_addresses ADD COLUMN state TEXT")
    for col in ("ipam_id", "ipam_subnet_id", "ipam_section_id"):
        if col not in acols:
            conn.execute(f"ALTER TABLE ipam_addresses ADD COLUMN {col} INTEGER")
    if "ip" not in icols:  # firewall interfaces know their address (L3 view)
        conn.execute("ALTER TABLE interfaces ADD COLUMN ip TEXT")
    if "ip6" not in icols:  # ...and their IPv6 one (PD subnets are routed too)
        conn.execute("ALTER TABLE interfaces ADD COLUMN ip6 TEXT")
    rcols = {r[1] for r in conn.execute("PRAGMA table_info(routes)")}
    if rcols and "flags" not in rcols:  # discard routes must not read as reach
        conn.execute("ALTER TABLE routes ADD COLUMN flags TEXT")
    fdbcols = {r[1] for r in conn.execute("PRAGMA table_info(fdb)")}
    if fdbcols and "source" not in fdbcols:
        conn.execute("ALTER TABLE fdb ADD COLUMN source TEXT NOT NULL DEFAULT 'librenms'")


def now() -> float:
    return time.time()


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def save_last_poll(conn: sqlite3.Connection, lines: list[str]) -> None:
    """The most recent poll's per-source results. /ops renders these so a
    headless deployment's overnight failures are visible without a shell."""
    set_state(conn, "last_poll", json.dumps({"ts": now(), "lines": lines}))


def upsert_device(conn: sqlite3.Connection, *, name: str, source: str, **fields: Any) -> int:
    fields = {k: v for k, v in fields.items() if v is not None}
    cols = ["name", "source", "last_seen", *fields]
    vals = [name, source, now(), *fields.values()]
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "name")
    conn.execute(
        f"INSERT INTO devices ({', '.join(cols)}) VALUES ({', '.join('?' * len(vals))}) "
        f"ON CONFLICT(name) DO UPDATE SET {updates}",
        vals,
    )
    return conn.execute("SELECT id FROM devices WHERE name = ?", (name,)).fetchone()[0]


def upsert_interface(conn: sqlite3.Connection, *, device_id: int, name: str, **fields: Any) -> None:
    fields = {k: v for k, v in fields.items() if v is not None}
    cols = ["device_id", "name", "last_seen", *fields]
    vals = [device_id, name, now(), *fields.values()]
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("device_id", "name"))
    conn.execute(
        f"INSERT INTO interfaces ({', '.join(cols)}) VALUES ({', '.join('?' * len(vals))}) "
        f"ON CONFLICT(device_id, name) DO UPDATE SET {updates}",
        vals,
    )


def upsert_link(conn: sqlite3.Connection, *, a_device: str, a_interface: str,
                b_device: str, b_interface: str, source: str) -> None:
    # store links direction-normalized so A<>B and B<>A are one row
    a, b = sorted([(a_device, a_interface), (b_device, b_interface)])
    conn.execute(
        "INSERT INTO links (a_device, a_interface, b_device, b_interface, source, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(a_device, a_interface, b_device, b_interface, source) "
        "DO UPDATE SET last_seen=excluded.last_seen",
        (*a, *b, source, now()),
    )


def upsert_subnet(conn: sqlite3.Connection, *, cidr: str, source: str, **fields: Any) -> None:
    fields = {k: v for k, v in fields.items() if v is not None}
    cols = ["cidr", "source", "last_seen", *fields]
    vals = [cidr, source, now(), *fields.values()]
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "cidr")
    conn.execute(
        f"INSERT INTO subnets ({', '.join(cols)}) VALUES ({', '.join('?' * len(vals))}) "
        f"ON CONFLICT(cidr) DO UPDATE SET {updates}",
        vals,
    )


def upsert_endpoint(conn: sqlite3.Connection, *, mac: str, source: str, **fields: Any) -> None:
    fields = {k: v for k, v in fields.items() if v is not None}
    cols = ["mac", "source", "last_seen", *fields]
    vals = [mac.lower(), source, now(), *fields.values()]
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "mac")
    conn.execute(
        f"INSERT INTO endpoints ({', '.join(cols)}) VALUES ({', '.join('?' * len(vals))}) "
        f"ON CONFLICT(mac) DO UPDATE SET {updates}",
        vals,
    )


def save_raw(conn: sqlite3.Connection, *, source: str, endpoint: str, payload: Any) -> None:
    conn.execute(
        "INSERT INTO raw_payloads (source, endpoint, fetched_at, payload) VALUES (?, ?, ?, ?)",
        (source, endpoint, now(), json.dumps(payload)),
    )
