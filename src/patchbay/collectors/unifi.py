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
AP_STATES = {0: "down", 1: "up", 4: "upgrading", 5: "provisioning", 6: "heartbeat_missed"}


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
            for d in devices:
                if d.get("type") != "uap":
                    continue
                name = d.get("name") or d.get("mac")
                ap_by_mac[d["mac"].lower()] = name
                dev_id = db.upsert_device(
                    conn, name=name, source=NAME,
                    mgmt_ip=d.get("ip"), vendor="Ubiquiti", model=d.get("model"),
                    os=f"unifi {d.get('version', '')}".strip(), role="ap",
                    status=AP_STATES.get(d.get("state"), "unknown"),
                )
                uplink = d.get("uplink") or {}
                db.upsert_interface(
                    conn, device_id=dev_id,
                    name=uplink.get("ifname") or "eth0",
                    oper_status="up" if uplink.get("up") else
                                ("down" if uplink else None),
                    speed_bps=(uplink.get("speed") or 0) * 1_000_000 or None,
                    mac=d.get("mac"),
                    description="uplink",
                )
                n_aps += 1

            cr = client.get(f"{base}/api/s/{site}/stat/sta")
            cr.raise_for_status()
            clients = cr.json().get("data", [])
            db.save_raw(conn, source=NAME, endpoint="stat/sta", payload=clients)
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
        return f"{n_aps} APs, {n_clients} clients"


register(UnifiCollector())
