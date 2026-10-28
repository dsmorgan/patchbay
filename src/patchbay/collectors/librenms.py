"""LibreNMS collector: devices, ports, and LLDP links.

Auth: X-Auth-Token header (create under gear menu -> API -> API Settings).
LibreNMS is the SNMP data plane; this collector never talks SNMP itself.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from typing import Any

import httpx

from ..config import Settings
from .. import db
from . import register

NAME = "librenms"


def _colon_mac(mac: str | None) -> str | None:
    """LibreNMS emits ifPhysAddress as bare hex ("288088734256"); every
    other MAC in the model is colon-delimited, so format at ingestion.
    Joins don't care (canon_mac strips separators) — readers do."""
    if not mac:
        return None
    raw = mac.strip().lower()
    if len(raw) == 12 and all(c in "0123456789abcdef" for c in raw):
        return ":".join(raw[i:i + 2] for i in range(0, 12, 2))
    return raw

OS_ROLE = {"ironware": "switch", "netgear": "switch", "freebsd": "firewall"}

# SNMP sysDescr values that don't match the real product name.  LibreNMS stores
# sysDescr as `hardware`; correct the known wrong values at ingestion.
HARDWARE_ALIASES: dict[str, str] = {
    "UAP-AC-Pro-Gen2": "UAP-AC-Pro",
}

# Netgear M4300 reports a factory string ("Unit: 1 Slot: 0 Port: 5 10G - Level")
# as ifAlias when a port has no comment set — noise, not documentation.
VENDOR_DEFAULT_DESCR = re.compile(r"^Unit: \d+ Slot: \d+ Port: \d+")

# Brocade/Ruckus FastIron echoes the long-form port name ("GigabitEthernet
# 1/1/10") as ifAlias when no comment is set: same port, different spelling
# ("ethernet1/1/10" is the ifName). A description that is just an interface
# word plus this port's own number path documents nothing.
IFNAME_ECHO = re.compile(r"^[A-Za-z0-9.]*[Ee]thernet\s*([\d/]+)$")


# LLDP's port-id TLV may carry a MAC address instead of a port name; as an
# interface name it's noise that blocks same-cable fusion with the far
# side's real name — treat it as unknown
MACISH_PORT = re.compile(r"^[0-9a-f]{2}([ :.\-][0-9a-f]{2}){5}", re.I)

# ifSpeed is a 32-bit gauge that wraps at ~4.29G; agents that compute
# ifHighSpeed from it instead of the real link rate (FreeBSD bsnmpd on
# vmxnet3, notably) report 10G as 10e9 % 2**32 = 1410065408 ("1.41G") —
# or that value rounded to whole Mbps, depending on which column LibreNMS
# kept. None of the wrapped values collide with a real link speed.
WRAPPED_SPEED = {}
for _s in (10_000_000_000, 20_000_000_000, 25_000_000_000,
           40_000_000_000, 100_000_000_000):
    WRAPPED_SPEED[_s % 2**32] = _s
    WRAPPED_SPEED[(_s % 2**32) // 1_000_000 * 1_000_000] = _s


# LibreNMS's per-second counter rates -> rate_history's columns (#64). Only
# the error rates live on the `ports` table; the discard rates are in
# `ports_statistics`, which the ports listing cannot select (it validates
# `columns` against `ports` and refuses the whole request on an unknown
# one). They come from one statistics call per port instead, only when
# PATCHBAY_LIBRENMS_DISCARDS is on (#83).
COUNTER_RATES = {"ifInErrors_rate": "in_errors", "ifOutErrors_rate": "out_errors",
                 "ifInDiscards_rate": "in_discards",
                 "ifOutDiscards_rate": "out_discards"}
PORTS_COLUMNS = ("port_id,ifName,ifIndex,ifAdminStatus,ifOperStatus,ifSpeed,"
                 "ifPhysAddress,ifAlias,ifInOctets_rate,ifOutOctets_rate,"
                 "ifInErrors_rate,ifOutErrors_rate")
# `poll_time` (epoch seconds of the port's last SNMP poll, a `ports` column)
# says which polling window a rate belongs to: a second patchbay poll inside
# the same window reads the same figures, and must not store them again
# (#117). Requested separately so a LibreNMS that refuses it (an unknown
# column fails the whole request) degrades to the old behavior.
POLL_TIME_COLUMN = "poll_time"


POLL_TIMES_STATE_KEY = "librenms_port_poll_times"

# The discard rates' per-port route (#83). One call per port, so it is
# spent only on ports whose discards matter: a stated cable (the link-down
# rule's sources) to another piece of infrastructure.
STATISTICS_PATH = "ports/{}?with=statistics"
DISCARD_RATES = ("ifInDiscards_rate", "ifOutDiscards_rate")
DISCARD_PEER_ROLES = frozenset({"switch", "ap", "firewall", "hypervisor"})
# what the last poll spent on statistics calls, for /ops and #86
STATISTICS_STATE_KEY = "librenms_statistics_calls"


def _poll_time(raw: Any) -> float | None:
    """A port's poll_time as epoch seconds, or None when it is absent or not
    a usable number. `ports.poll_time` is an unsigned integer column, so the
    API sends an int (or, through some JSON encoders, a numeric string); a
    datetime string is not a form it can return. None stores every poll."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        t = float(raw)
    except (TypeError, ValueError):
        return None
    return t if math.isfinite(t) and t > 0 else None


def _rate_sample(p: dict) -> tuple | None:
    """One port's rate_history values (in_bps, out_bps, then the counters
    in db.RATE_COUNTERS order), or None when the port reported no rate."""
    bits = [int(p[k] * 8) if p.get(k) is not None else None
            for k in ("ifInOctets_rate", "ifOutOctets_rate")]
    by_col = {col: p.get(k) for k, col in COUNTER_RATES.items()}
    counters = [float(by_col[c]) if by_col[c] is not None else None
                for c in db.RATE_COUNTERS]
    vals = (*bits, *counters)
    return vals if any(v is not None for v in vals) else None


def _discard_rates(body: Any) -> dict[str, float]:
    """The discard rates in a statistics answer, keyed like the ports
    listing's columns so _rate_sample maps them. The port comes back as a
    one-item list (or a bare object) with the statistics row nested under
    its relation name; a rate missing or not a number is left out."""
    port = body.get("port") if isinstance(body, dict) else None
    if isinstance(port, list):
        port = port[0] if port else None
    if not isinstance(port, dict):
        return {}
    stats = port.get("statistics") or port.get("ports_statistics")
    if isinstance(stats, list):
        stats = stats[0] if stats else None
    src = stats if isinstance(stats, dict) else port
    out = {}
    for k in DISCARD_RATES:
        v = src.get(k)
        if isinstance(v, (int, float, str)) and not isinstance(v, bool):
            try:
                f = float(v)
            except ValueError:
                continue
            if math.isfinite(f) and f >= 0:
                out[k] = f
    return out


def _discard_targets(conn: sqlite3.Connection, devices: list, ports: dict,
                     prev_times: dict) -> list[int]:
    """port_ids worth a statistics call: a port that is one end of a stated
    links row whose other end is a switch, AP, firewall, or hypervisor, and
    whose LibreNMS poll_time has moved since the last stored sample (an
    unmoved one stores no sample, so its call would be wasted). Reads only:
    it runs in the fetch phase, before the first write. The stored links
    and devices carry the normalized name, which for a device merged under
    another one (a declared alias, a serial, an identity merge with another
    source) is not its sysName, so every name resolves through the aliases
    table, chains collapsed as normalize does, before it compares."""
    from ..attention import STATED_LINK_SOURCES
    from ..normalize import canonical_name

    alias_map = dict(conn.execute("SELECT alias, canonical FROM aliases"))

    def resolve(name: str) -> str:
        seen: set[str] = set()
        for n in (name, canonical_name(name)):
            while n in alias_map and n not in seen:
                seen.add(n)
                n = alias_map[n]
            if seen:
                return canonical_name(n)
        return canonical_name(name)

    role = {resolve(r["name"]): (r["role"] or "").lower()
            for r in conn.execute("SELECT name, role FROM devices")}
    ends: set[tuple[str, str]] = set()
    for r in conn.execute(
            "SELECT a_device, a_interface, b_device, b_interface FROM links "
            f"WHERE source IN ({','.join('?' * len(STATED_LINK_SOURCES))})",
            STATED_LINK_SOURCES):
        a, b = resolve(r["a_device"]), resolve(r["b_device"])
        if role.get(b) in DISCARD_PEER_ROLES:
            ends.add((a, r["a_interface"]))
        if role.get(a) in DISCARD_PEER_ROLES:
            ends.add((b, r["b_interface"]))
    out = []
    for d in devices:
        name = resolve(d.get("sysName") or d.get("hostname") or "")
        for p in ports.get(d.get("device_id"), []):
            pid = p.get("port_id")
            if not pid or (name, p.get("ifName")) not in ends:
                continue
            src_ts = _poll_time(p.get("poll_time"))
            if src_ts is not None and src_ts == prev_times.get(str(pid)):
                continue
            out.append(pid)
    return out


def _prev_poll_times(conn: sqlite3.Connection) -> dict:
    try:
        prev = json.loads(db.get_state(conn, POLL_TIMES_STATE_KEY) or "{}")
    except ValueError:
        return {}
    return prev if isinstance(prev, dict) else {}


def _speed(raw: int | None) -> int | None:
    return WRAPPED_SPEED.get(raw, raw) if raw is not None else None


def _descr(raw: str | None, ifname: str = "") -> str:
    # "" rather than None: upsert_interface drops None fields ("no opinion"),
    # so only an empty string can clear a stale or vendor-default description
    if not raw or VENDOR_DEFAULT_DESCR.match(raw):
        return ""
    echo = IFNAME_ECHO.match(raw.strip())
    if echo and ifname.endswith(echo.group(1)):
        return ""
    return raw


class LibreNmsCollector:
    name = NAME

    def configured(self, settings: Settings) -> bool:
        return bool(settings.librenms_url and settings.librenms_token)

    def _get(self, client: httpx.Client, settings: Settings, path: str) -> Any:
        url = f"{settings.librenms_url.rstrip('/')}/api/v0/{path.lstrip('/')}"
        r = client.get(url, headers={"X-Auth-Token": settings.librenms_token})
        if r.status_code == 404:  # e.g. ports on a ping-only device
            return {}
        r.raise_for_status()
        return r.json()

    def _ports(self, client: httpx.Client, settings: Settings,
               device_id: int, with_poll_time: bool) -> tuple[list, bool]:
        """One device's ports, and whether poll_time was in the answer. A 400
        or 422 on the request that names poll_time means this LibreNMS
        refuses the column: retry without it, and report False so the caller
        stops asking."""
        path = f"devices/{device_id}/ports?columns={PORTS_COLUMNS}"
        if with_poll_time:
            try:
                return self._get(client, settings,
                                 f"{path},{POLL_TIME_COLUMN}").get("ports", []), True
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in (400, 422):
                    raise
        return self._get(client, settings, path).get("ports", []), False

    def _statistics(self, client: httpx.Client, settings: Settings,
                    targets: list[int]) -> tuple[dict[int, dict], int, str | None]:
        """Discard rates for each target port (#83): ({port_id: rates},
        calls made, error or None). A failed call stops the rest for this
        poll rather than failing the collection: discards are an extra, and
        a LibreNMS that refuses one port's statistics will refuse them all.
        A 200 whose body is not JSON (a login page, a proxy's error page, a
        PHP notice ahead of the JSON) counts as failed too; JSON of the
        wrong shape just carries no rates (_discard_rates)."""
        rates: dict[int, dict] = {}
        calls = 0
        for pid in targets:
            calls += 1
            try:
                body = self._get(client, settings, STATISTICS_PATH.format(pid))
            except (httpx.HTTPError, ValueError) as e:
                return rates, calls, type(e).__name__
            got = _discard_rates(body)
            if got:
                rates[pid] = got
        return rates, calls, None

    def _fetch(self, settings: Settings,
               conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Every API call the collection needs, before anything is written:
        the caller's transaction takes SQLite's write lock at the first
        write, and holding it across one ports request per device locked
        the UI out for the whole collection (#109). `conn` is read, never
        written, to pick the ports worth a statistics call."""
        with httpx.Client(verify=settings.tls_verify, timeout=30) as client:
            devices = self._get(client, settings, "devices").get("devices", [])
            ports = {}
            with_poll_time = True
            for d in devices:
                if d.get("sysName") or d.get("hostname"):
                    ports[d["device_id"]], with_poll_time = self._ports(
                        client, settings, d["device_id"], with_poll_time)
            discards: dict[int, dict] = {}
            calls, stats_error = 0, None
            if settings.librenms_discards and conn is not None:
                targets = _discard_targets(conn, devices, ports,
                                           _prev_poll_times(conn))
                discards, calls, stats_error = self._statistics(
                    client, settings, targets)
            return {
                "devices": devices, "ports": ports,
                "discards": discards, "statistics_calls": calls,
                "statistics_error": stats_error,
                "links": self._get(client, settings, "resources/links").get("links", []),
                "vlans": self._get(client, settings, "resources/vlans").get("vlans", []),
                "fdb": self._get(client, settings, "resources/fdb").get("ports_fdb", []),
            }

    def collect(self, settings: Settings, conn: sqlite3.Connection) -> str:
        fetched = self._fetch(settings, conn)
        # last poll_time seen per LibreNMS port_id (#117). Keyed by id, not
        # name: names are renamed by canonicalization and aliases. Rebuilt
        # from the ports seen this poll, so it stays bounded.
        prev_times = _prev_poll_times(conn)
        seen_times: dict[str, float] = {}
        discards = fetched["discards"]
        n_devices = n_ports = n_links = 0
        # the write phase: no network I/O from here on
        devices = fetched["devices"]
        db.save_raw(conn, source=NAME, endpoint="devices", payload=devices)
        id_to_name: dict[int, str] = {}
        port_map: dict[int, tuple[str, str]] = {}  # port_id -> (device, ifName)
        for d in devices:
            name = d.get("sysName") or d.get("hostname")
            if not name:
                continue
            status = "disabled" if d.get("disabled") else ("up" if d.get("status") else "down")
            dev_id = db.upsert_device(
                conn, name=name, source=NAME,
                mgmt_ip=d.get("ip") or d.get("overwrite_ip"),
                vendor=HARDWARE_ALIASES.get(d.get("hardware"), d.get("hardware")),
                os=d.get("os"),
                role=OS_ROLE.get(d.get("os")),
                serial=(d.get("serial") or None),
                status=status,
            )
            id_to_name[d["device_id"]] = name
            n_devices += 1

            for p in fetched["ports"][d["device_id"]]:
                if not p.get("ifName"):
                    continue
                if p.get("port_id"):
                    port_map[p["port_id"]] = (name, p["ifName"])
                db.upsert_interface(
                    conn, device_id=dev_id, name=p["ifName"],
                    ifindex=p.get("ifIndex"),
                    admin_status=p.get("ifAdminStatus"),
                    oper_status=p.get("ifOperStatus"),
                    speed_bps=_speed(p.get("ifSpeed")),
                    mac=_colon_mac(p.get("ifPhysAddress")),
                    description=_descr(p.get("ifAlias"), p["ifName"]),
                    # librenms rates are octets/sec from the 5-min poller
                    in_bps=(int(p["ifInOctets_rate"] * 8)
                            if p.get("ifInOctets_rate") is not None else None),
                    out_bps=(int(p["ifOutOctets_rate"] * 8)
                             if p.get("ifOutOctets_rate") is not None else None),
                )
                n_ports += 1
                # a poll_time equal to the last one seen for this port is the
                # same measurement again (#117); no poll_time (older LibreNMS,
                # or a port never polled) can't be told apart, so it is
                # stored. Unequal, even lower, is a new measurement (a reset
                # LibreNMS must not wedge the port).
                src_ts = _poll_time(p.get("poll_time"))
                pkey = str(p["port_id"]) if p.get("port_id") else None
                last = prev_times.get(pkey) if pkey else None
                if not isinstance(last, (int, float)):
                    last = None
                if pkey and (src_ts or last):
                    seen_times[pkey] = src_ts if src_ts is not None else last
                sample = _rate_sample({**p, **discards.get(p.get("port_id"), {})})
                if sample is None or (src_ts is not None and src_ts == last):
                    continue
                db.record_rates(conn, name, p["ifName"],
                                **dict(zip(("in_bps", "out_bps", *db.RATE_COUNTERS),
                                           sample)))

        db.set_state(conn, POLL_TIMES_STATE_KEY, json.dumps(seen_times))
        db.set_state(conn, STATISTICS_STATE_KEY, json.dumps({
            "ts": db.now(), "on": settings.librenms_discards,
            "calls": fetched["statistics_calls"],
            "error": fetched["statistics_error"]}))

        # Retire devices removed from LibreNMS — they stayed on every page
        # with frozen status forever (disabled devices still appear in the
        # listing, so this only fires on real deletion). Scoped to rows
        # this collector owns; guarded on a non-empty listing. Stored
        # names are post-normalize canonical, so match both forms.
        if devices:
            from ..normalize import canonical_name
            seen_names: set[str] = set()
            for d in devices:
                n = d.get("sysName") or d.get("hostname")
                if n:
                    seen_names |= {n, canonical_name(n)}
            if seen_names:
                conn.execute(
                    f"DELETE FROM devices WHERE source = ? "
                    f"AND name NOT IN ({','.join('?' * len(seen_names))})",
                    (NAME, *sorted(seen_names)))

        links = fetched["links"]
        db.save_raw(conn, source=NAME, endpoint="resources/links", payload=links)
        for l in links:
            local_dev = id_to_name.get(l.get("local_device_id"))
            remote = l.get("remote_hostname") or l.get("remote_device_id")
            if isinstance(remote, int):
                remote = id_to_name.get(remote, str(remote))
            local_if = (l.get("local_port_name")
                        or (port_map.get(l.get("local_port_id")) or (None, None))[1]
                        or (str(l["local_port_id"]) if l.get("local_port_id") else "?"))
            remote_if = (l.get("remote_port_name") or l.get("remote_port")
                         or l.get("remote_port_descr") or "?")
            if MACISH_PORT.match(remote_if):
                remote_if = "?"
            if not (local_dev and remote):
                continue
            if local_dev == remote and local_if == remote_if:
                continue  # LLDP self-loop artifact (seen on OPNsense vmx ports)
            db.upsert_link(
                conn, a_device=local_dev, a_interface=local_if,
                b_device=str(remote), b_interface=str(remote_if), source="lldp",
            )
            n_links += 1
        vlans = fetched["vlans"]
        db.save_raw(conn, source=NAME, endpoint="resources/vlans", payload=vlans)
        # refresh-by-replace, but never on an empty response: a transient
        # empty payload must not wipe the table until the next good poll
        if vlans:
            conn.execute("DELETE FROM device_vlans WHERE source = ?", (NAME,))
        n_vlans = 0
        for v in vlans:
            dev = id_to_name.get(v.get("device_id"))
            vid = int(v.get("vlan_vlan") or 0)
            if not dev or not vid:
                continue
            # phpIPAM names win; Q-BRIDGE only fills gaps and proves presence
            conn.execute(
                "INSERT INTO vlans (vid, name, source, last_seen) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(vid) DO UPDATE SET last_seen=excluded.last_seen, "
                "name=COALESCE(vlans.name, excluded.name)",
                (vid, v.get("vlan_name"), NAME, db.now()),
            )
            conn.execute(
                "INSERT OR IGNORE INTO device_vlans (device, vid, source) VALUES (?, ?, ?)",
                (dev, vid, NAME),
            )
            n_vlans += 1
        # prune VLANs Q-BRIDGE no longer reports anywhere: this source's
        # rows only, and never one something still claims (same
        # claim-aware guard as the phpipam prune — `source` records the
        # first creator, so a claimed row may belong to a live VLAN
        # another source still reports). Guarded on a non-empty listing.
        seen_vids = sorted({int(v.get("vlan_vlan") or 0) for v in vlans} - {0})
        if seen_vids:
            conn.execute(
                "DELETE FROM vlans WHERE source = ? "
                f"AND vid NOT IN ({','.join('?' * len(seen_vids))}) "
                "AND vid NOT IN (SELECT vid FROM device_vlans) "
                "AND vid NOT IN (SELECT vid FROM port_vlans) "
                "AND vid NOT IN (SELECT vid FROM vnic_vlans)",
                (NAME, *seen_vids))

        fdb = fetched["fdb"]
        if fdb:  # same empty-response guard as device_vlans above
            conn.execute("DELETE FROM fdb WHERE source = 'librenms'")
        # each MAC-table entry names the VLAN it was learned in by
        # LibreNMS's internal vlan id; the vlans listing maps that to the
        # VLAN number. Not every platform reports it (0 = unknown) — on
        # those the routed view falls back to the port's access VLAN.
        vlan_num = {v.get("vlan_id"): int(v.get("vlan_vlan") or 0)
                    for v in vlans if v.get("vlan_id") is not None}
        n_fdb = 0
        for e in fdb:
            where = port_map.get(e.get("port_id"))
            mac = e.get("mac_address") or ""
            if not where or len(mac) != 12:
                continue
            mac = ":".join(mac[i:i + 2] for i in range(0, 12, 2)).lower()
            conn.execute(
                "INSERT OR IGNORE INTO fdb (device, interface, mac, source, vlan) "
                "VALUES (?, ?, ?, 'librenms', ?)",
                (*where, mac, vlan_num.get(e.get("vlan_id"), 0)),
            )
            n_fdb += 1
        summary = (f"{n_devices} devices, {n_ports} ports, {n_links} lldp links, "
                   f"{n_vlans} vlan memberships, {n_fdb} fdb")
        if settings.librenms_discards:
            summary += f", {fetched['statistics_calls']} statistics calls"
            if fetched["statistics_error"]:
                summary += (f" (stopped after {fetched['statistics_error']}; "
                            "discards skipped for the rest)")
        return summary


register(LibreNmsCollector())
