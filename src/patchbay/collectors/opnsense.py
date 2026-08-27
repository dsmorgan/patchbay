"""OPNsense collector: interfaces, gateway health, ARP, and DHCP leases.

Auth: API key/secret (HTTP basic). ACLs are the GUI page privileges of the
API user; a 403 here names the missing privilege rather than failing the
whole poll, so partial grants degrade gracefully.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx

from ..config import Settings
from .. import db
from . import register

NAME = "opnsense"


class OpnsenseCollector:
    name = NAME

    def configured(self, settings: Settings) -> bool:
        return bool(settings.opnsense_host and settings.opnsense_api_key
                    and settings.opnsense_api_secret)

    def collect(self, settings: Settings, conn: sqlite3.Connection) -> str:
        host = settings.opnsense_host
        base = f"{host.rstrip('/')}/api" if "://" in host else f"https://{host}/api"
        auth = (settings.opnsense_api_key, settings.opnsense_api_secret)
        notes: list[str] = []

        def get(path: str) -> Any | None:
            r = client.get(f"{base}/{path}", auth=auth)
            if r.status_code == 403:
                notes.append(f"{path}: 403 (grant the matching page privilege)")
                return None
            r.raise_for_status()
            return r.json()

        with httpx.Client(verify=settings.tls_verify, timeout=20) as client:
            # the firewall knows what it is — SNMP only sees "FreeBSD/amd64"
            fw = get("core/firmware/info") or {}
            version = fw.get("product_version")
            _hostname = host.split("://")[-1].split(":")[0]
            dev_id = db.upsert_device(conn, name=_hostname.split(".")[0],
                                      source=NAME, role="firewall", status="up",
                                      vendor="OPNsense",
                                      os=(f"opnsense {version}" if version else None))

            ifaces = get("interfaces/overview/export")
            if ifaces is not None:
                db.save_raw(conn, source=NAME, endpoint="interfaces/overview/export", payload=ifaces)
                for i in ifaces if isinstance(ifaces, list) else []:
                    name = i.get("device") or i.get("identifier")
                    if not name:
                        continue
                    addrs = i.get("addresses") or []
                    db.upsert_interface(
                        conn, device_id=dev_id, name=name,
                        oper_status="up" if i.get("status") == "up" else i.get("status"),
                        mac=(i.get("macaddr") or None),
                        description=i.get("description"),
                        ip=(i.get("addr4") or None),
                        ip6=(i.get("addr6") or None),
                        speed_bps=None,
                    )

            gws = get("routes/gateway/status")
            if gws is not None:
                db.save_raw(conn, source=NAME, endpoint="routes/gateway/status", payload=gws)
                for g in gws.get("items", []):
                    conn.execute(
                        "INSERT INTO gateways (name, address, status, loss, delay, source, last_seen) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(name) DO UPDATE SET address=excluded.address, "
                        "status=excluded.status, loss=excluded.loss, delay=excluded.delay, "
                        "last_seen=excluded.last_seen",
                        (g.get("name"), g.get("address"), g.get("status_translated") or g.get("status"),
                         g.get("loss"), g.get("delay"), NAME, db.now()),
                    )

            arp = get("diagnostics/interface/get_arp") or get("diagnostics/interface/getArp")
            if arp is not None:
                db.save_raw(conn, source=NAME, endpoint="get_arp", payload=arp)
                rows = arp if isinstance(arp, list) else arp.get("rows", [])
                for e in rows:
                    mac = (e.get("mac") or "").lower()
                    if mac and mac != "(incomplete)":
                        db.upsert_endpoint(conn, mac=mac, source=NAME,
                                           ip=e.get("ip"), hostname=(e.get("hostname") or None))

            # the routing table proves reachability that addresses can't: a
            # delegated IPv6 prefix (or any downstream network) is routed via
            # a next-hop the firewall doesn't have an address in, so without
            # this it looks isolated. Needs "Diagnostics: Routing Tables".
            routes = get("diagnostics/interface/get_routes")
            n_routes = 0
            if routes:
                db.save_raw(conn, source=NAME, endpoint="routes", payload=routes)
                fw_name = _hostname.split(".")[0]
                conn.execute("DELETE FROM routes WHERE source = ?", (NAME,))
                for rt in routes if isinstance(routes, list) else []:
                    dest, proto = rt.get("destination"), rt.get("proto")
                    if not dest or not proto:
                        continue
                    if dest == "default":  # a default route covers everything;
                        continue           # it can't say a subnet is *reachable*
                    conn.execute(
                        "INSERT OR REPLACE INTO routes (device, destination, gateway, "
                        "interface, proto, flags, source, last_seen) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (fw_name, dest, rt.get("gateway"), rt.get("netif"),
                         proto, rt.get("flags"), NAME, db.now()))
                    n_routes += 1
                notes.append(f"{n_routes} routes")

            # searchLease paginates: walk every page rather than trusting one
            # oversized rowCount (leases beyond it would be silently dropped)
            page, all_rows = 1, []
            while True:
                leases = get(f"dhcpv4/leases/searchLease?current={page}&rowCount=500")
                if leases is None:
                    break
                rows = leases.get("rows", [])
                all_rows += rows
                if len(rows) < 500:
                    break
                page += 1
            if all_rows:
                db.save_raw(conn, source=NAME, endpoint="dhcpv4/leases",
                            payload={"rows": all_rows})
                for l in all_rows:
                    mac = (l.get("mac") or "").lower()
                    if mac:
                        db.upsert_endpoint(conn, mac=mac, source=NAME,
                                           ip=l.get("address"), hostname=(l.get("hostname") or None))

        summary = "interfaces/gateways/arp/leases polled"
        if notes:
            summary += f" ({'; '.join(notes)})"
        return summary


register(OpnsenseCollector())
