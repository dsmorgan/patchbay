"""UniFi collector for the self-hosted Network application.

Auth: local admin + cookie login (POST /api/login). Self-hosted controllers
do not support API keys — that is a UniFi OS feature. APs become devices
(role=ap); wireless clients become endpoints attached to their AP.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx

from ..config import Settings
from .. import db
from . import register

NAME = "unifi"
DEVICE_STATES = {0: "down", 1: "up", 4: "upgrading", 5: "provisioning", 6: "heartbeat_missed"}

# UniFi API model codes → human-readable names.  The controller returns short
# codes (e.g. "U7PG2") that don't map cleanly to the product names shown in
# the UI; correct the ones we know about so patchbay displays useful labels.
MODEL_NAMES: dict[str, str] = {
    "U7PG2":   "AC Pro",       # UAP-AC-Pro / UAP-AC-Pro-Gen2 (same code)
    "U7PIW":   "AC In-Wall Pro",  # UAP-AC-IW-Pro
    "UAP6MP":  "U6 Pro",       # U6-Pro
    "U7PROXG": "U7 Pro XGS",   # U7-Pro-XGS (alternate code)
    "UAPA6A4": "U7 Pro XGS",   # U7-Pro-XGS (actual API code)
    "USWED77": "USW-Pro-10-PoE",
    "USPM16P": "USW-Pro-Max-16-PoE",
    "US8P150": "US-8-150W",
    "US8P60":  "US-8-60W",
}


class UnifiCollector:
    name = NAME

    def configured(self, settings: Settings) -> bool:
        return bool(settings.unifi_url and settings.unifi_user and settings.unifi_pass)

    def collect(self, settings: Settings, conn: sqlite3.Connection) -> str:
        base = settings.unifi_url.rstrip("/")
        site = "default"
        n_aps = n_clients = 0
        with httpx.Client(verify=settings.tls_verify, timeout=20) as client:
            r = client.post(f"{base}/api/login",
                            json={"username": settings.unifi_user, "password": settings.unifi_pass})
            r.raise_for_status()

            # raise_for_status: a controller error envelope (LoginRequired…)
            # must fail loudly, not read as "0 APs"
            dr = client.get(f"{base}/api/s/{site}/stat/device")
            dr.raise_for_status()
            devices = dr.json().get("data", [])
            db.save_raw(conn, source=NAME, endpoint="stat/device", payload=devices)
            ap_by_mac: dict[str, str] = {}
            sw_mac_to_name: dict[str, str] = {}        # switch chassis MAC → device name
            sw_port_names: dict[str, dict[int, str]] = {}  # device name → {port_idx: port_name}
            # uplinks buffered until every switch's port names are known:
            # (device, local port name, upstream device, remote port index)
            pending_links: list[tuple[str, str, str, int]] = []
            n_switches = 0
            for d in devices:
                dev_type = d.get("type")
                dev_name = d.get("name") or d.get("mac")
                status = DEVICE_STATES.get(d.get("state"), "unknown")

                if dev_type == "usw":
                    sw_id = db.upsert_device(
                        conn, name=dev_name, source=NAME,
                        mgmt_ip=d.get("ip"), vendor="Ubiquiti",
                        model=MODEL_NAMES.get(d.get("model"), d.get("model")),
                        os=f"unifi {d.get('version', '')}".strip(), role="switch",
                        status=status,
                    )
                    if d.get("mac"):
                        sw_mac_to_name[d["mac"].lower()] = dev_name
                    port_idx_map: dict[int, str] = {}
                    for port in (d.get("port_table") or []):
                        idx = port.get("port_idx")
                        port_name = port.get("name") or (f"Port {idx}" if idx else None)
                        if not port_name:
                            continue
                        if idx is not None:
                            port_idx_map[idx] = port_name
                        speed = port.get("speed") or 0
                        db.upsert_interface(
                            conn, device_id=sw_id, name=port_name,
                            oper_status="up" if port.get("up") else "down",
                            admin_status="up" if port.get("enable") else "down",
                            speed_bps=speed * 1_000_000 or None,
                        )
                    sw_port_names[dev_name] = port_idx_map
                    # Switch-to-switch uplink (same LLDP data as APs). The
                    # remote port name isn't knowable yet — the upstream
                    # switch may come later in the device list — so buffer
                    # the link and resolve names after the loop.
                    uplink = d.get("uplink") or {}
                    upstream = uplink.get("uplink_device_name")
                    upstream_port = uplink.get("uplink_remote_port")
                    local_idx = uplink.get("port_idx")
                    if upstream and upstream_port is not None and local_idx is not None:
                        local_port = port_idx_map.get(local_idx, f"Port {local_idx}")
                        pending_links.append(
                            (dev_name, local_port, upstream, upstream_port))
                    n_switches += 1
                    continue

                if dev_type != "uap":
                    continue
                ap_by_mac[d["mac"].lower()] = dev_name
                dev_id = db.upsert_device(
                    conn, name=dev_name, source=NAME,
                    mgmt_ip=d.get("ip"), vendor="Ubiquiti",
                    model=MODEL_NAMES.get(d.get("model"), d.get("model")),
                    os=f"unifi {d.get('version', '')}".strip(), role="ap",
                    status=status,
                )
                uplink = d.get("uplink") or {}
                iface_name = uplink.get("name") or uplink.get("ifname") or "eth0"
                db.upsert_interface(
                    conn, device_id=dev_id,
                    name=iface_name,
                    oper_status="up" if uplink.get("up") else
                                ("down" if uplink else None),
                    speed_bps=(uplink.get("speed") or 0) * 1_000_000 or None,
                    mac=d.get("mac"),
                    description="uplink",
                )
                # Physical link to the upstream switch via LLDP-discovered
                # port. Buffered like the switch uplinks so a renamed port
                # on the upstream switch resolves to its stored name.
                upstream = uplink.get("uplink_device_name")
                upstream_port = uplink.get("uplink_remote_port")
                if upstream and upstream_port is not None:
                    pending_links.append(
                        (dev_name, iface_name, upstream, upstream_port))
                n_aps += 1

            # uplink_remote_port is a port index; resolve it through the
            # upstream switch's port_table so a renamed port still matches
            # the stored interface. "Port N" is the unmodified-USW fallback.
            for a_dev, a_if, upstream, remote_idx in pending_links:
                remote_port = sw_port_names.get(upstream, {}).get(
                    remote_idx, f"Port {remote_idx}")
                db.upsert_link(conn, a_device=a_dev, a_interface=a_if,
                               b_device=upstream, b_interface=remote_port,
                               source=NAME)

            cr = client.get(f"{base}/api/s/{site}/stat/sta")
            cr.raise_for_status()
            clients = cr.json().get("data", [])
            db.save_raw(conn, source=NAME, endpoint="stat/sta", payload=clients)
            fdb_rows: list[tuple[str, str, str]] = []
            for c in clients:
                mac = (c.get("mac") or "").lower()
                if not mac:
                    continue
                ap_name = ap_by_mac.get((c.get("ap_mac") or "").lower())
                db.upsert_endpoint(
                    conn, mac=mac, source=NAME,
                    ip=c.get("ip") or None,
                    hostname=c.get("name") or c.get("hostname") or None,
                    device=ap_name,
                    interface=c.get("essid") or None,   # SSID as the "port" for wireless
                    vlan=c.get("vlan"),
                )
                n_clients += 1
                # wired clients carry sw_mac + sw_port — write into fdb so
                # normalize's fdb-uplink inference can place them on switch ports
                if c.get("is_wired"):
                    sw_mac = (c.get("sw_mac") or "").lower()
                    sw_port_idx = c.get("sw_port")
                    if sw_mac and sw_port_idx is not None:
                        sw_name = sw_mac_to_name.get(sw_mac)
                        port_name = sw_port_names.get(sw_name or "", {}).get(sw_port_idx)
                        if sw_name and port_name:
                            fdb_rows.append((sw_name, port_name, mac))
            if clients:  # same empty-response guard as librenms fdb
                conn.execute("DELETE FROM fdb WHERE source = 'unifi'")
                conn.executemany(
                    "INSERT OR IGNORE INTO fdb (device, interface, mac, source) VALUES (?, ?, ?, 'unifi')",
                    fdb_rows,
                )
        parts = []
        if n_switches:
            parts.append(f"{n_switches} switches")
        parts.append(f"{n_aps} APs, {n_clients} clients, {len(fdb_rows)} fdb")
        return ", ".join(parts)


register(UnifiCollector())
