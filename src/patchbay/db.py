"""SQLite persistence for the normalized network model.

Collectors upsert by natural keys (device name, interface name, MAC, CIDR)
so repeated polls converge instead of duplicating. Raw API payloads are kept
in raw_payloads for debugging and for the snapshot generator.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from . import redact

log = logging.getLogger(__name__)

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
    temperature REAL,          -- °C, device's own telemetry (liveness:
                               -- written only while up, omitted when stale)
    cpus INTEGER,              -- hardware mini-specs (#44): cores and RAM,
    mem_bytes INTEGER,         -- hypervisors only so far (vsphere reports them)
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
    source TEXT NOT NULL,      -- lldp | unifi | fdb-inference | vsphere-hint | …
    last_seen REAL,
    UNIQUE (a_device, a_interface, b_device, b_interface, source)
);
CREATE TABLE IF NOT EXISTS known_cables (
    a_device TEXT NOT NULL,    -- stated cables (lldp, unifi, declared) kept
    a_interface TEXT NOT NULL, -- past the evidence TTL for the link-down
    b_device TEXT NOT NULL,    -- rule (#79, known_cables.py); ends sorted
    b_interface TEXT NOT NULL, -- like links. The map never reads this.
    first_seen REAL NOT NULL,
    last_up REAL,              -- last normalize that saw it with no end down
    retired_at REAL,           -- operator said it is gone; NULL = live
    PRIMARY KEY (a_device, a_interface, b_device, b_interface)
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
    vlan INTEGER NOT NULL DEFAULT 0,  -- VLAN it was learned in; 0 = not reported
    -- source is part of the key: two collectors legitimately see the same
    -- MAC on the same port, and each must own (insert, refresh, delete) its
    -- own row — otherwise the first reporter owns the triple forever and
    -- its scoped delete kills a row the other source still reports. vlan is
    -- too: one MAC on a trunk is learned once per VLAN it talks in, and
    -- that per-VLAN fact is what places a trunked host on its networks.
    PRIMARY KEY (device, interface, mac, source, vlan)
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
CREATE TABLE IF NOT EXISTS config_revisions (
    id INTEGER PRIMARY KEY,    -- firewall config history pulled over the
    device TEXT NOT NULL,      -- device API (#23). `text` is REDACTED before
    fetched_at REAL NOT NULL,  -- it gets here — secret-bearing elements are
    sha TEXT NOT NULL,         -- replaced with keyed content stamps, so a change
    message TEXT,              -- stays diffable but no key/password is ever
    author TEXT,               -- stored. sha covers the noise-stripped text:
    text TEXT NOT NULL         -- a save that changed nothing real is no rev.
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
    out_bps INTEGER,
    in_errors REAL,            -- error and discard counters, per second, for
    out_errors REAL,           -- the port-canary rule (ADR-0003 Decision 5);
    in_discards REAL,          -- NULL when the source does not report them
    out_discards REAL
);
CREATE INDEX IF NOT EXISTS idx_rate_hist ON rate_history (device, interface, ts);
-- the newest samples across every port (the port canary's last 15 minutes,
-- retention) without reading the whole week through idx_rate_hist
CREATE INDEX IF NOT EXISTS idx_rate_hist_ts ON rate_history (ts);
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
CREATE TABLE IF NOT EXISTS tunnels (
    id INTEGER PRIMARY KEY,    -- firewall VPN tunnels (#42): first-class
    device TEXT NOT NULL,      -- objects, NOT interfaces — tunnel virtual
    type TEXT NOT NULL,        -- interfaces stay out of the port model.
    name TEXT NOT NULL,        -- type: wireguard | openvpn | ipsec | vpn.
    peer TEXT,                 -- remote endpoint (host:port). NO key
    interface TEXT,            -- material, ever — not even public keys.
    status TEXT,               -- up | down | idle (wireguard: stale
    last_handshake REAL,       -- handshake) | raw source phrases
    detail TEXT,               -- short human detail (allowed IPs, SA info)
    allowed_ips TEXT,          -- WireGuard peer allowed-ips, comma-separated
                               -- CIDRs: which peer a shared wg0 route reaches
    source TEXT NOT NULL,
    last_seen REAL,
    UNIQUE (device, type, name)
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
-- Alerting (ADR-0003). Configuration is database rows edited on /alerts,
-- never env: alert_rules is seeded from alerting.RULES on first run, and
-- alert_transports / alert_silences exist now so later parts add no
-- migration.
CREATE TABLE IF NOT EXISTS alert_rules (
    name TEXT PRIMARY KEY,     -- catalog rule id (alerting.RULES)
    enabled INTEGER NOT NULL DEFAULT 1,
    severity TEXT,             -- override; NULL = what the rule says per item
    params TEXT NOT NULL DEFAULT '{}',  -- JSON: `for`, `remind`, rule knobs
    route TEXT,                -- NULL = the site's default route; 'none' =
                               -- attention list only, never dispatched
    enabled_at REAL            -- when last switched on; NULL = always on.
);                             -- An event older than this is not replayed.
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,         -- the attention item's key
    rule TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    state TEXT NOT NULL,       -- pending | active | silenced | cleared
    polls INTEGER NOT NULL DEFAULT 1,  -- consecutive polls held, for `for`
    raised_at REAL NOT NULL,
    active_at REAL,
    cleared_at REAL,
    last_notified_at REAL,
    text TEXT,
    href TEXT,
    sample_ts REAL             -- the sample the last counted poll judged, for
);                             -- rules whose items carry one (port canary)
-- one open alert per key; a cleared one is history, and the same condition
-- returning raises a new row
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_open ON alerts (key)
    WHERE state != 'cleared';
CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY,    -- history: raised | active | cleared, and
    alert_id INTEGER,          -- later notified | silenced | delivery_failed.
    ts REAL NOT NULL,          -- key/text/severity are copied, not joined,
    event TEXT NOT NULL,       -- so pruning alerts never orphans a line
    key TEXT NOT NULL,
    rule TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    text TEXT,
    href TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_events_ts ON alert_events (ts);
CREATE TABLE IF NOT EXISTS alert_transports (
    id INTEGER PRIMARY KEY,    -- transports.py (#61). url is a credential:
    name TEXT NOT NULL UNIQUE, -- masked on the page, never logged, never
    kind TEXT NOT NULL,        -- in a snapshot. kind = webhook preset.
    url TEXT NOT NULL,
    extra TEXT NOT NULL DEFAULT '{}',   -- JSON, preset-specific fields
    enabled INTEGER NOT NULL DEFAULT 1,
    last_result TEXT,
    last_sent_at REAL,
    last_error TEXT,
    failures INTEGER NOT NULL DEFAULT 0  -- consecutive, for the 3-strike item
);
CREATE TABLE IF NOT EXISTS alert_silences (
    id INTEGER PRIMARY KEY,    -- silences.py (#62); PATCHBAY_EXPECT adds
    kind TEXT NOT NULL,        -- read-only rows of its own, never stored
                               -- here. kind: key | device | port | category
    scope TEXT NOT NULL,       -- the exact key, device, dev:port, category
    until REAL,                -- NULL = permanent
    reason TEXT,
    created_by TEXT,
    created_at REAL NOT NULL
);
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


def backup(src_path: str, dest_path: str, *, force: bool = False) -> int:
    """Copy the live database to dest_path with SQLite's backup API (#112).

    Consistent while the poller writes: `Connection.backup()` with the
    default `pages=-1` copies everything in one step under one read lock, so
    the copy is a single point-in-time snapshot and can't livelock against
    a writer that commits constantly (the writer waits briefly instead).
    Don't switch to a stepped copy (`pages=N`): SQLite restarts a stepped
    backup whenever another connection commits mid-copy. The file holds webhook tokens, the redaction key,
    and config history, so it is created mode 0600 before SQLite writes a
    byte, and it lands under its final name only once it passes an
    integrity check. Returns the size in bytes; raises FileExistsError
    (no force), FileNotFoundError (no source), or sqlite3.Error.
    """
    if not os.path.isfile(src_path):  # connect() would quietly create one
        raise FileNotFoundError(f"no database at {src_path}")
    if os.path.exists(dest_path) and not force:
        raise FileExistsError(dest_path)
    # `backup /data/backups/x.db` works on the first run; a directory made
    # here is private, like the file
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), mode=0o700, exist_ok=True)
    tmp = f"{dest_path}.partial"
    # O_EXCL: never write through a stale or planted file at the temp name
    try:
        os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        os.remove(tmp)  # a crashed earlier run; our own leftover
        os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        src = sqlite3.connect(src_path, timeout=30)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
            verdict = dst.execute("PRAGMA quick_check").fetchone()[0]
            if verdict != "ok":
                raise sqlite3.DatabaseError(f"backup failed its integrity check: {verdict}")
        finally:
            dst.close()
            src.close()
        os.replace(tmp, dest_path)
    except BaseException:
        for leftover in (tmp, f"{tmp}-journal"):
            if os.path.exists(leftover):
                os.remove(leftover)
        raise
    return os.path.getsize(dest_path)


# rate_history's per-second error and discard counters (#64), in the order
# the port-canary rule reports them
RATE_COUNTERS = ("in_errors", "out_errors", "in_discards", "out_discards")


def record_rates(conn: sqlite3.Connection, device: str, iface: str, *,
                 in_bps: int | None = None, out_bps: int | None = None,
                 in_errors: float | None = None, out_errors: float | None = None,
                 in_discards: float | None = None, out_discards: float | None = None,
                 ts: float | None = None) -> bool:
    """Append one rate_history sample for a port: the load view's peak and
    the port canaries read it (docs/collectors.md). Rates are per second,
    bits for `*_bps`; pass None for a figure the source did not report, and
    for all of them when the port is not live, so a stale reading is never
    written as current. Returns False, writing nothing, when every figure
    is None. `ts` defaults to now. Never commits: the caller owns the
    transaction, like every collector write."""
    vals = (in_bps, out_bps, in_errors, out_errors, in_discards, out_discards)
    if all(v is None for v in vals):
        return False
    conn.execute(
        "INSERT INTO rate_history (device, interface, ts, in_bps, out_bps, "
        "in_errors, out_errors, in_discards, out_discards) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (device, iface, now() if ts is None else ts, *vals))
    return True


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # lightweight migration: add columns that predate an existing db file
    cols = {r[1] for r in conn.execute("PRAGMA table_info(devices)")}
    if "serial" not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN serial TEXT")
    if "temperature" not in cols:  # #40: controller-reported thermals
        conn.execute("ALTER TABLE devices ADD COLUMN temperature REAL")
    for col in ("cpus", "mem_bytes"):  # #44: hypervisor mini-specs
        if col not in cols:
            conn.execute(f"ALTER TABLE devices ADD COLUMN {col} INTEGER")
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
    hcols = {r[1] for r in conn.execute("PRAGMA table_info(rate_history)")}
    for col in RATE_COUNTERS:  # #64: port-counter canaries
        if col not in hcols:
            conn.execute(f"ALTER TABLE rate_history ADD COLUMN {col} REAL")
    alcols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)")}
    if "sample_ts" not in alcols:  # #110: `for` counts distinct samples
        conn.execute("ALTER TABLE alerts ADD COLUMN sample_ts REAL")
    rucols = {r[1] for r in conn.execute("PRAGMA table_info(alert_rules)")}
    if "enabled_at" not in rucols:  # #110: re-enabling replays no events
        conn.execute("ALTER TABLE alert_rules ADD COLUMN enabled_at REAL")
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(tunnels)")}
    if tcols and "allowed_ips" not in tcols:  # #98: route-to-peer matching
        conn.execute("ALTER TABLE tunnels ADD COLUMN allowed_ips TEXT")
    rcols = {r[1] for r in conn.execute("PRAGMA table_info(routes)")}
    if rcols and "flags" not in rcols:  # discard routes must not read as reach
        conn.execute("ALTER TABLE routes ADD COLUMN flags TEXT")
    fdbpk = {r[1]: r[5] for r in conn.execute("PRAGMA table_info(fdb)")}
    if fdbpk and "source" not in fdbpk:
        conn.execute("ALTER TABLE fdb ADD COLUMN source TEXT NOT NULL DEFAULT 'librenms'")
        fdbpk["source"] = 0
    if fdbpk and not fdbpk["source"]:
        # source must be part of the key (see the schema comment); SQLite
        # can't alter a primary key, so rebuild the table in place
        conn.executescript(
            "CREATE TABLE fdb_migrate (device TEXT NOT NULL, "
            "interface TEXT NOT NULL, mac TEXT NOT NULL, "
            "source TEXT NOT NULL DEFAULT 'librenms', "
            "PRIMARY KEY (device, interface, mac, source));"
            "INSERT OR IGNORE INTO fdb_migrate "
            "  SELECT device, interface, mac, source FROM fdb;"
            "DROP TABLE fdb;"
            "ALTER TABLE fdb_migrate RENAME TO fdb;")
        fdbpk = {r[1]: r[5] for r in conn.execute("PRAGMA table_info(fdb)")}
    if fdbpk and "vlan" not in fdbpk:
        # the learned VLAN joins the key (a trunk learns one MAC per VLAN);
        # existing rows carry 0 = not reported until the next poll refreshes
        conn.executescript(
            "CREATE TABLE fdb_migrate (device TEXT NOT NULL, "
            "interface TEXT NOT NULL, mac TEXT NOT NULL, "
            "source TEXT NOT NULL DEFAULT 'librenms', "
            "vlan INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (device, interface, mac, source, vlan));"
            "INSERT OR IGNORE INTO fdb_migrate (device, interface, mac, source) "
            "  SELECT device, interface, mac, source FROM fdb;"
            "DROP TABLE fdb;"
            "ALTER TABLE fdb_migrate RENAME TO fdb;")
    _reredact_config_revisions(conn)


# the redact.REDACTION_VERSION the stored config history was last redacted
# under; app_state, so a fresh database records the current one
REDACTION_STATE = "config_redaction_version"
REDACTION_KEY = "redaction_key"


def redaction_key(conn: sqlite3.Connection) -> bytes:
    """The per-install key for config-history stamps (#93), generated on
    first use. It lives beside the history in app_state: it keeps anyone who
    can read the history (on /configs, say) but not the database from
    testing guessed secrets against a stamp."""
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO NOTHING", (REDACTION_KEY, secrets.token_hex(32)))
    return conn.execute("SELECT value FROM app_state WHERE key = ?",
                        (REDACTION_KEY,)).fetchone()[0].encode()


def _stored_redaction_version(conn: sqlite3.Connection) -> int | None:
    """The recorded redaction version: 0 when none was ever recorded (a
    database from before #93), None when the value doesn't parse."""
    # read by index: init also runs on connections without a Row factory
    row = conn.execute("SELECT value FROM app_state WHERE key = ?",
                       (REDACTION_STATE,)).fetchone()
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _reredact_config_revisions(conn: sqlite3.Connection) -> None:
    """Once per redaction-rule version (#93), re-apply the current element
    rules to every stored firewall config revision, so secrets the old rules
    missed leave the history, not only new revisions. Redaction is
    idempotent: stamps already in place stay put, so old and new revisions
    still diff cleanly, and the recomputed sha matches what
    save_config_revision derives from the same config next poll (no
    spurious revision).

    Version 2 also keys the stamps: a pre-#93 stamp is sha256(content)[:8],
    and redact.rekey turns it into exactly the keyed stamp a new poll writes
    for the same content, so history stays continuous across the upgrade.
    Rekeying is NOT idempotent (a stamp keyed twice no longer matches the
    next poll), so it runs only when the recorded version reads as below 2,
    and only under the write lock: BEGIN IMMEDIATE, then the version is
    read again, so of two processes starting init at once (every request
    runs it, and so does the poller) only the first rewrites. An unreadable
    version value is not trusted to mean "old": rows are re-redacted, never
    rekeyed. One transaction: the rewrite and the version mark land together
    or not at all. A row that can't be re-redacted is logged and left as
    stored rather than failing every init after it."""
    done = _stored_redaction_version(conn)
    if done is not None and done >= redact.REDACTION_VERSION:
        return
    if conn.in_transaction:   # as executescript would: never nest
        conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as e:   # another writer held the lock past
        log.warning("config history re-redaction deferred: %s", e)  # the timeout
        return
    try:
        done = _stored_redaction_version(conn)
        if done is not None and done >= redact.REDACTION_VERSION:
            conn.rollback()   # another process finished it first
            return
        key = redaction_key(conn)
        for rid, text in conn.execute(
                "SELECT id, text FROM config_revisions").fetchall():
            try:
                new = text
                if done is not None and done < 2:   # unkeyed stamps first
                    new = redact.STAMP_RE.sub(
                        lambda m: "redacted:" + redact.rekey(m.group(1), key), new)
                new, _ = redact.redact_xml(new, key)
            except Exception:
                log.exception("config revision %s could not be re-redacted; "
                              "left as stored", rid)
                continue
            if new != text:
                conn.execute(
                    "UPDATE config_revisions SET text = ?, sha = ? WHERE id = ?",
                    (new, hashlib.sha256(new.encode()).hexdigest(), rid))
        set_state(conn, REDACTION_STATE, str(redact.REDACTION_VERSION))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def now() -> float:
    return time.time()


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


# The poll lease (#109): one poll at a time across processes. The web app's
# Poll button and the scheduled poller are separate processes (separate
# containers, usually), so an in-process lock can't keep them apart, and a
# file lock would lean on how a bind mount implements locking. The database
# both already share holds the lease instead: an app_state row naming its
# holder and when it lapses. A holder renews it every POLL_LEASE_RENEW
# seconds from a heartbeat thread (lease_heartbeat); one that dies, even
# killed outright, leaves a lease that lapses within POLL_LEASE_TTL.
POLL_LEASE = "poll_lease"
POLL_LEASE_TTL = 180.0
POLL_LEASE_RENEW = 60.0


def lease_owner(kind: str) -> str:
    """A name for one poll: who took it (cli, web), its process, and a
    random part, since two containers can each run a pid 1."""
    import os

    return f"{kind}:{os.getpid()}:{secrets.token_hex(4)}"


def _lease_holder(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?",
                       (POLL_LEASE,)).fetchone()
    try:
        held = json.loads(row[0]) if row else None
    except ValueError:
        return None          # a damaged row holds nothing
    if (isinstance(held, dict) and isinstance(held.get("owner"), str)
            and isinstance(held.get("until"), (int, float))):
        return held
    return None


# claim_poll_lease's answers: the lease is ours; another poll holds it; or
# the database stayed locked past the busy timeout, so nobody can say
LEASE_CLAIMED, LEASE_HELD, LEASE_BUSY = "claimed", "held", "busy"


def claim_poll_lease(conn: sqlite3.Connection, owner: str, *,
                     ttl: float = POLL_LEASE_TTL) -> str:
    """Take the poll lease for owner, or renew it if owner holds it.
    Returns LEASE_CLAIMED, LEASE_HELD (another poll holds an unexpired
    lease), or LEASE_BUSY (the database stayed locked past the busy
    timeout: a busy database is not a running poll). The read and the
    write share one BEGIN IMMEDIATE, so two processes can't both see the
    lease free. Commits any open transaction first, as executescript
    would."""
    if conn.in_transaction:
        conn.commit()
    # a plain read first: a held lease is answered without waiting for the
    # write lock, which the running poll may be holding
    try:
        held = _lease_holder(conn)
    except sqlite3.OperationalError:
        return LEASE_BUSY
    if held and held["owner"] != owner and held["until"] > now():
        return LEASE_HELD
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return LEASE_BUSY
    try:
        t = now()
        held = _lease_holder(conn)
        if held and held["owner"] != owner and held["until"] > t:
            conn.rollback()
            return LEASE_HELD
        set_state(conn, POLL_LEASE, json.dumps({"owner": owner, "until": t + ttl}))
        conn.commit()
        return LEASE_CLAIMED
    except BaseException:
        conn.rollback()
        raise


def release_poll_lease(conn: sqlite3.Connection, owner: str) -> None:
    """Give the lease back if owner still holds it. Discards uncommitted
    work, as closing the connection would: commit before releasing. Best
    effort: a lease that can't be released lapses at its expiry."""
    if conn.in_transaction:
        conn.rollback()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as e:
        log.warning("poll lease not released (it lapses on its own): %s", e)
        return
    try:
        held = _lease_holder(conn)
        if held and held["owner"] == owner:
            conn.execute("DELETE FROM app_state WHERE key = ?", (POLL_LEASE,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


@contextmanager
def lease_heartbeat(path: str, owner: str, *,
                    every: float = POLL_LEASE_RENEW) -> Iterator[None]:
    """Renew owner's poll lease every `every` seconds while the with body
    runs, on a thread with its own connection: a collector's fetch can
    outlast the TTL, and renewing only between collectors would let the
    lease lapse mid-poll. A renewal that finds the database locked (the
    poll itself writing) just tries again next beat."""
    import threading

    stop = threading.Event()

    def beat() -> None:
        conn = sqlite3.connect(path)
        try:
            while not stop.wait(every):
                try:
                    claim_poll_lease(conn, owner)
                except sqlite3.Error as e:
                    log.warning("poll lease renewal failed: %s", e)
        finally:
            conn.close()

    thread = threading.Thread(target=beat, name="poll-lease", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=10)


def save_last_poll(conn: sqlite3.Connection, lines: list[str]) -> None:
    """The most recent poll's per-source results. /ops renders these so a
    headless deployment's overnight failures are visible without a shell."""
    set_state(conn, "last_poll", json.dumps({"ts": now(), "lines": lines}))


# Snapshot failures, newest last, for the snapshot-failed rule. A snapshot
# runs after the poll commits, so the rule reads this record on the next
# poll. The cap only bounds the list: the rule reads the last day.
SNAPSHOT_FAILURES = "snapshot_failures"
SNAPSHOT_FAILURES_KEEP = 20


def record_snapshot_failure(conn: sqlite3.Connection, error: str, *,
                            kind: str = "failed", trigger: str = "daily") -> None:
    """Note one snapshot that failed (`failed`) or was written but not
    delivered off-host (`undelivered`). `trigger` names the path: daily,
    manual, or alert."""
    entries = snapshot_failures(conn)
    # a sequence number beside the time: two failures can share a
    # millisecond, and each is its own event
    n = (entries[-1].get("n") or 0) + 1 if entries else 1
    entries.append({"ts": now(), "n": n, "kind": kind, "trigger": trigger,
                    "error": str(error)[:500]})
    set_state(conn, SNAPSHOT_FAILURES, json.dumps(entries[-SNAPSHOT_FAILURES_KEEP:]))


def snapshot_failures(conn: sqlite3.Connection) -> list[dict]:
    raw = get_state(conn, SNAPSHOT_FAILURES)
    try:
        entries = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [e for e in entries if isinstance(e, dict)
            and isinstance(e.get("ts"), (int, float))] if isinstance(entries, list) else []


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


def save_tunnels(conn: sqlite3.Connection, *, device: str, source: str,
                 type_: str, rows: list[dict]) -> None:
    """One tunnel type's refresh-by-replace: the caller only invokes this
    when the type's status endpoint actually answered, so an empty rows
    list means 'no tunnels of this type', not a failed poll (a 403 or
    error never reaches here — last good data stands). Shared by the
    firewall collectors."""
    conn.execute("DELETE FROM tunnels WHERE device = ? AND source = ? AND type = ?",
                 (device, source, type_))
    for t in rows:
        conn.execute(
            "INSERT INTO tunnels (device, type, name, peer, interface, status, "
            "last_handshake, detail, allowed_ips, source, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(device, type, name) DO UPDATE SET peer=excluded.peer, "
            "interface=excluded.interface, status=excluded.status, "
            "last_handshake=excluded.last_handshake, detail=excluded.detail, "
            "allowed_ips=excluded.allowed_ips, "
            "source=excluded.source, last_seen=excluded.last_seen",
            (device, type_, t["name"], t.get("peer"), t.get("interface"),
             t.get("status"), t.get("last_handshake"), t.get("detail"),
             t.get("allowed_ips"), source, now()))



# raw_payloads is a debugging aid, but source APIs return credential fields
# in otherwise-useful payloads (LibreNMS device rows carry the SNMPv3
# authpass/cryptopass; UniFi stat/device carries controller keys). Any key
# whose name contains one of these fragments has its VALUE dropped before
# storage — overzealous by design, the payload stays useful without it.
_RAW_SECRET_KEY = re.compile(
    r"pass|secret|token|community|credential|private|psk|(?:^|[-_])key(?:$|[-_])|"
    r"x_.*key|authalgo|cryptoalgo", re.I)


def _scrub_payload(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("<stripped>" if _RAW_SECRET_KEY.search(str(k)) and v
                    else _scrub_payload(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_payload(v) for v in obj]
    return obj


def save_raw(conn: sqlite3.Connection, *, source: str, endpoint: str, payload: Any) -> None:
    conn.execute(
        "INSERT INTO raw_payloads (source, endpoint, fetched_at, payload) VALUES (?, ?, ?, ?)",
        (source, endpoint, now(), json.dumps(_scrub_payload(payload))),
    )
