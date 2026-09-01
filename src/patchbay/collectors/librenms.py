"""LibreNMS collector: devices, ports, and LLDP links.

Auth: X-Auth-Token header (create under gear menu -> API -> API Settings).
LibreNMS is the SNMP data plane; this collector never talks SNMP itself.
"""

from __future__ import annotations

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


def _speed(raw: int | None) -> int | None:
    return WRAPPED_SPEED.get(raw, raw) if raw is not None else None


def _descr(raw: str | None) -> str:
    # "" rather than None: upsert_interface drops None fields ("no opinion"),
    # so only an empty string can clear a stale or vendor-default description
    if not raw or VENDOR_DEFAULT_DESCR.match(raw):
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

    def collect(self, settings: Settings, conn: sqlite3.Connection) -> str:
        n_devices = n_ports = n_links = 0
        with httpx.Client(verify=settings.tls_verify, timeout=30) as client:
            devices = self._get(client, settings, "devices").get("devices", [])
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

                ports = self._get(
                    client, settings,
                    f"devices/{d['device_id']}/ports?columns="
                    "port_id,ifName,ifIndex,ifAdminStatus,ifOperStatus,ifSpeed,"
                    "ifPhysAddress,ifAlias,ifInOctets_rate,ifOutOctets_rate",
                ).get("ports", [])
                for p in ports:
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
                        description=_descr(p.get("ifAlias")),
                        # librenms rates are octets/sec from the 5-min poller
                        in_bps=(int(p["ifInOctets_rate"] * 8)
                                if p.get("ifInOctets_rate") is not None else None),
                        out_bps=(int(p["ifOutOctets_rate"] * 8)
                                 if p.get("ifOutOctets_rate") is not None else None),
                    )
                    n_ports += 1
                    if p.get("ifInOctets_rate") is not None or p.get("ifOutOctets_rate") is not None:
                        conn.execute(
                            "INSERT INTO rate_history (device, interface, ts, in_bps, out_bps) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (name, p["ifName"], db.now(),
                             int(p["ifInOctets_rate"] * 8) if p.get("ifInOctets_rate") is not None else None,
                             int(p["ifOutOctets_rate"] * 8) if p.get("ifOutOctets_rate") is not None else None),
                        )
            conn.execute("DELETE FROM rate_history WHERE ts < ?", (db.now() - 7 * 86400,))

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

            links = self._get(client, settings, "resources/links").get("links", [])
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
            vlans = self._get(client, settings, "resources/vlans").get("vlans", [])
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

            fdb = self._get(client, settings, "resources/fdb").get("ports_fdb", [])
            if fdb:  # same empty-response guard as device_vlans above
                conn.execute("DELETE FROM fdb WHERE source = 'librenms'")
            n_fdb = 0
            for e in fdb:
                where = port_map.get(e.get("port_id"))
                mac = e.get("mac_address") or ""
                if not where or len(mac) != 12:
                    continue
                mac = ":".join(mac[i:i + 2] for i in range(0, 12, 2)).lower()
                conn.execute(
                    "INSERT OR IGNORE INTO fdb (device, interface, mac, source) VALUES (?, ?, ?, 'librenms')",
                    (*where, mac),
                )
                n_fdb += 1
        return (f"{n_devices} devices, {n_ports} ports, {n_links} lldp links, "
                f"{n_vlans} vlan memberships, {n_fdb} fdb")


register(LibreNmsCollector())
