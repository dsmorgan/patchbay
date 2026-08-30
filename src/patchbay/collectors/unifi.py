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
            n_switches = 0
            for d in devices:
                dev_type = d.get("type")
                dev_name = d.get("name") or d.get("mac")
                status = DEVICE_STATES.get(d.get("state"), "unknown")

                if dev_type == "usw":
                    sw_id = db.upsert_device(
                        conn, name=dev_name, source=NAME,
                        mgmt_ip=d.get("ip"), vendor="Ubiquiti", model=d.get("model"),
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
                    # Switch-to-switch uplink link (same LLDP data as APs).
                    # port_idx is the local uplink port; look up its name from
                    # port_table so the link matches the stored interface name.
                    uplink = d.get("uplink") or {}
                    upstream = uplink.get("uplink_device_name")
                    upstream_port = uplink.get("uplink_remote_port")
                    local_idx = uplink.get("port_idx")
                    if upstream and upstream_port is not None and local_idx is not None:
                        local_port = next(
                            (p.get("name") or f"Port {local_idx}"
                             for p in (d.get("port_table") or [])
                             if p.get("port_idx") == local_idx),
                            f"Port {local_idx}",
                        )
                        db.upsert_link(
                            conn,
                            a_device=dev_name, a_interface=local_port,
                            b_device=upstream, b_interface=f"Port {upstream_port}",
                            source=NAME,
                        )
                    n_switches += 1
                    continue

                if dev_type != "uap":
                    continue
                ap_by_mac[d["mac"].lower()] = dev_name
                dev_id = db.upsert_device(
                    conn, name=dev_name, source=NAME,
                    mgmt_ip=d.get("ip"), vendor="Ubiquiti", model=d.get("model"),
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
                # Physical link to upstream switch via LLDP-discovered port.
                # uplink_remote_port is the port index; "Port N" matches the
                # switch's port_table name field for an unmodified USW.
                upstream = uplink.get("uplink_device_name")
                upstream_port = uplink.get("uplink_remote_port")
                if upstream and upstream_port is not None:
                    db.upsert_link(
                        conn,
                        a_device=dev_name, a_interface=iface_name,
                        b_device=upstream, b_interface=f"Port {upstream_port}",
                        source=NAME,
                    )
                n_aps += 1

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
