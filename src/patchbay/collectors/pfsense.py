"""pfSense collector: interfaces, gateway health, DHCP static mappings.

Auth: x-api-key header (the API key from the pfSense REST API package).
A 403 on any endpoint is logged and skipped; partial grants degrade
gracefully rather than failing the whole poll.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx

from ..config import Settings
from .. import db
from . import register

NAME = "pfsense"

# Interface name prefixes that indicate VPN tunnels — not physical connections
# and not useful for topology. Skipped for both interfaces and gateways.
_TUNNEL_PREFIXES = ("tun", "ovpn", "gif", "gre")

# Interface types reported by pfSense REST API that are VPN/virtual only
_TUNNEL_TYPES = {"wireguard", "openvpn", "ipsec", "gif", "gre"}


_MEDIA_SPEED: list[tuple[str, int]] = [
    ("100gbase", 100_000_000_000),
    ("40gbase",   40_000_000_000),
    ("25gbase",   25_000_000_000),
    ("10gbase",   10_000_000_000),
    ("10g",       10_000_000_000),
    ("2500base",   2_500_000_000),
    ("2.5gbase",   2_500_000_000),
    ("1000base",   1_000_000_000),
    ("100base",      100_000_000),
    ("10base",        10_000_000),
]


def _parse_media_speed(media: str | None) -> int | None:
    if not media:
        return None
    m = media.lower()
    for prefix, bps in _MEDIA_SPEED:
        if prefix in m:
            return bps
    return None


def _is_tunnel(iface_id: str, iface_type: str | None = None) -> bool:
    name = iface_id.lower()
    return (
        name.startswith(_TUNNEL_PREFIXES)
        or (iface_type or "").lower() in _TUNNEL_TYPES
    )


class PfsenseCollector:
    name = NAME

    def configured(self, settings: Settings) -> bool:
        return bool(settings.pfsense_host and settings.pfsense_api_key)

    def collect(self, settings: Settings, conn: sqlite3.Connection) -> str:
        host = settings.pfsense_host
        base = host.rstrip("/") if "://" in host else f"https://{host}"
        headers = {
            "x-api-key": settings.pfsense_api_key,
            "Content-Type": "application/json",
        }
        notes: list[str] = []

        def get(path: str) -> Any | None:
            r = client.get(f"{base}/{path}", headers=headers)
            if r.status_code == 403:
                notes.append(f"{path}: 403")
                return None
            if r.status_code == 404:
                # absent endpoint usually means the pfrest package is missing
                # or too old — say so instead of a healthy-looking zero
                notes.append(f"{path}: 404 (pfrest package missing or outdated?)")
                return None
            r.raise_for_status()
            data = r.json()
            # pfSense REST API v2 wraps responses in {"data": ..., "code": 200}
            if isinstance(data, dict) and "data" in data:
                return data["data"]
            return data

        with httpx.Client(verify=settings.tls_verify, timeout=20) as client:
            _hostname = base.split("://")[-1].split(":")[0]
            dev_name = _hostname.split(".")[0]

            sysinfo = get("api/v2/status/system") or {}
            version = None
            if isinstance(sysinfo, dict):
                version = (sysinfo.get("pfsense_version")
                           or sysinfo.get("base_firmware_version"))

            dev_id = db.upsert_device(
                conn, name=dev_name, source=NAME, role="firewall", status="up",
                vendor="pfSense",
                os=(f"pfsense {version}" if version else None),
                mgmt_ip=_hostname,
            )

            # Build live-status index keyed by BSD interface name.
            # Status endpoint uses "if" (BSD name); index by all name variants so
            # the config-endpoint lookup (also by "if") reliably hits.
            iface_status_raw = get("api/v2/status/interfaces") or []
            if iface_status_raw:
                db.save_raw(conn, source=NAME, endpoint="status/interfaces",
                            payload=iface_status_raw)
            iface_status: dict[str, dict] = {}
            for i in (iface_status_raw if isinstance(iface_status_raw, list) else []):
                # Status endpoint uses "hwif" for BSD name (igc4), "name" for logical
                # name (wan). Index by both so config-side lookup by BSD name hits.
                for key_field in ("hwif", "if", "name"):
                    key = i.get(key_field)
                    if key and key not in iface_status:
                        iface_status[key] = i

            # Collect tunnel interface ids so we can skip their gateways too
            tunnel_iface_ids: set[str] = set()
            vlan_rows: list[tuple] = []

            iface_cfg = get("api/v2/interfaces") or []
            n_ifaces = 0
            for iface in (iface_cfg if isinstance(iface_cfg, list) else []):
                iface_id = iface.get("if") or iface.get("id")
                if not iface_id:
                    continue
                if _is_tunnel(iface_id, iface.get("type")):
                    tunnel_iface_ids.add(iface_id)
                    continue
                live = iface_status.get(iface_id, {})

                # MAC: status endpoint may use "macaddr" or "mac"
                mac = (live.get("macaddr") or live.get("mac")
                       or iface.get("mac") or "").lower() or None

                # Oper status: "up" / "no carrier" / "down" → up/down/None
                raw_oper = (live.get("status") or "").lower()
                if raw_oper == "up":
                    oper_status = "up"
                elif raw_oper in ("no carrier", "down"):
                    oper_status = "down"
                else:
                    oper_status = raw_oper or None

                # Admin status from config enable flag
                enabled = iface.get("enable")
                admin_status = ("up" if enabled else "down") if enabled is not None else None

                # Speed from media string ("1000baseT <full-duplex>")
                speed_bps = _parse_media_speed(live.get("media"))

                # IP — resolve DHCP/PPPoE from live status; grab IPv6 too
                ip = iface.get("ipaddr") or None
                if ip in ("dhcp", "pppoe", ""):
                    ip = live.get("ipaddr") or None
                ip6 = live.get("ipaddrv6") or None

                db.upsert_interface(
                    conn, device_id=dev_id, name=iface_id,
                    oper_status=oper_status,
                    admin_status=admin_status,
                    mac=mac,
                    description=iface.get("descr") or None,
                    ip=ip,
                    ip6=ip6,
                    speed_bps=speed_bps,
                )

                # VLAN sub-interfaces: write port_vlans so 802.1Q column populates.
                # igc0.20 → vid=20 untagged (the firewall strips the tag outbound).
                if "." in iface_id:
                    try:
                        vlan_rows.append((dev_name, iface_id, int(iface_id.rsplit(".", 1)[-1]), NAME))
                    except ValueError:
                        pass
                n_ifaces += 1

            # This collector owns its port_vlans rows: delete-then-insert, so a
            # VLAN sub-interface removed from pfSense leaves the model too
            # (upsert-only stores rot — see CONTRIBUTING). Guarded on a
            # non-empty interfaces listing: a 403 or empty body must keep
            # last good data, never wipe it.
            if iface_cfg:
                conn.execute("DELETE FROM port_vlans WHERE source = ? AND device = ?",
                             (NAME, dev_name))
                conn.executemany(
                    "INSERT INTO port_vlans (device, interface, vid, tagged, source) "
                    "VALUES (?, ?, ?, 0, ?) ON CONFLICT(device, interface, vid) DO NOTHING",
                    vlan_rows)

            # Purge tunnel interfaces and their port_vlans rows written before the
            # filter was in place. interfaces has no source column, so target by prefix.
            conn.execute(
                "DELETE FROM interfaces WHERE device_id = ? AND ("
                "name LIKE 'tun%' OR name LIKE 'ovpn%' "
                "OR name LIKE 'gif%' OR name LIKE 'gre%')",
                (dev_id,),
            )
            conn.execute(
                "DELETE FROM port_vlans WHERE source = ? AND device = ? AND ("
                "interface LIKE 'tun%' OR interface LIKE 'ovpn%' "
                "OR interface LIKE 'gif%' OR interface LIKE 'gre%')",
                (NAME, dev_name),
            )

            # Gateway health — fetch first, prune only on a real listing:
            # deleting before the GET meant a 403 or empty body emptied the
            # gateways table for a full poll cycle.
            gw_status = get("api/v2/status/gateways") or []
            if gw_status:
                conn.execute("DELETE FROM gateways WHERE source = ?", (NAME,))
                db.save_raw(conn, source=NAME, endpoint="status/gateways", payload=gw_status)
            # VPN gateways don't belong in the WAN gateway table, but their
            # monitor status is tunnel health (#42). WireGuard has no status
            # endpoint in pfrest v2, so its gateway is the only liveness
            # signal; OpenVPN/IPsec have real status endpoints below, so
            # their gateways are just skipped (no duplicate tunnel rows).
            wg_rows: list[dict] = []
            for gw in (gw_status if isinstance(gw_status, list) else []):
                gw_name = gw.get("name")
                if not gw_name:
                    continue
                gw_iface = gw.get("interface") or gw.get("friendlyiface") or ""
                is_vpn_gw = (gw_iface in tunnel_iface_ids or _is_tunnel(gw_iface)
                             or (gw_name.upper().endswith(("_VPNV4", "_VPNV6", "_VPN", "_GW"))
                                 and any(kw in gw_name.upper()
                                         for kw in ("VPN", "WG", "TUN", "OVPN"))))
                if is_vpn_gw:
                    if "WG" in gw_name.upper() or gw_iface.lower().startswith(("wg", "tun_wg")):
                        raw_status = (gw.get("status") or "").lower()
                        wg_rows.append({
                            "name": gw_name,
                            "peer": gw.get("monitorip") or None,
                            "interface": gw_iface or None,
                            "status": ("up" if raw_status == "online" else
                                       "down" if raw_status in ("down", "loss")
                                       else raw_status or None),
                            "detail": (f"loss {gw['loss']}"
                                       if gw.get("loss") not in (None, "", "0.0%", "0%")
                                       else None),
                        })
                    continue
                # Map pfSense status string → patchbay status.
                # API returns "status": "online"|"down"|"unknown"|"loss" (string, not bool).
                # "unknown" means pfSense has no RTT data yet (e.g. IPv6 gateway pending).
                raw_status = (gw.get("status") or "").lower()
                if raw_status == "online":
                    status = "up"
                elif raw_status in ("down", "loss"):
                    status = "down"
                else:
                    status = raw_status or None  # "unknown", "pending", etc. passed through
                conn.execute(
                    "INSERT INTO gateways (name, address, status, loss, delay, source, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET address=excluded.address, "
                    "status=excluded.status, loss=excluded.loss, delay=excluded.delay, "
                    "last_seen=excluded.last_seen",
                    (gw_name,
                     gw.get("monitorip") or gw.get("srcip"),
                     status,
                     gw.get("loss"), gw.get("stddev"), NAME, db.now()),
                )

            # VPN tunnels (#42): each type's rows land only when its own
            # signal actually answered — a 403/404 (get() returns None)
            # keeps last good data. Tunnel *interfaces* stay excluded above.
            if gw_status:
                db.save_tunnels(conn, device=dev_name, source=NAME,
                                type_="wireguard", rows=wg_rows)

            ovpn = get("api/v2/status/openvpn")
            if ovpn is not None:
                rows = []
                for i, s in enumerate(ovpn if isinstance(ovpn, list) else []):
                    nm = s.get("name") or f"openvpn {i}"
                    conns = s.get("conns") or []
                    raw = str(s.get("status") or "").lower()
                    rows.append({
                        "name": nm,
                        "peer": s.get("remote_host") or None,
                        "interface": None,
                        # servers report no status word — being listed means
                        # running; clients carry an explicit one
                        "status": ("up" if raw in ("up", "connected", "ok", "")
                                   else raw),
                        "detail": (f"{len(conns)} clients" if conns else None),
                    })
                db.save_tunnels(conn, device=dev_name, source=NAME,
                                type_="openvpn", rows=rows)

            ipsec = get("api/v2/status/ipsec")
            if ipsec is not None:
                rows = []
                for i, sa in enumerate(ipsec if isinstance(ipsec, list) else []):
                    nm = (sa.get("con_id") or sa.get("con-id")
                          or sa.get("name") or f"ipsec {i}")
                    state = str(sa.get("state") or "").lower()
                    rows.append({
                        "name": str(nm),
                        "peer": sa.get("remote_host") or None,
                        "interface": None,
                        "status": "up" if state == "established" else (state or None),
                        "detail": (f"IKEv{sa['version']}"
                                   if sa.get("version") else None),
                    })
                db.save_tunnels(conn, device=dev_name, source=NAME,
                                type_="ipsec", rows=rows)

            # DHCP static mappings as endpoints (hostname + IP + MAC)
            dhcp_servers = get("api/v2/services/dhcp_servers") or []
            n_endpoints = 0
            for srv in (dhcp_servers if isinstance(dhcp_servers, list) else []):
                for sm in (srv.get("staticmap") or []):
                    mac = (sm.get("mac") or "").lower()
                    if mac:
                        db.upsert_endpoint(
                            conn, mac=mac, source=NAME,
                            ip=sm.get("ipaddr") or None,
                            hostname=sm.get("hostname") or None,
                        )
                        n_endpoints += 1

        parts = [f"{n_ifaces} interfaces", "gateways"]
        if n_endpoints:
            parts.append(f"{n_endpoints} DHCP static mappings")
        summary = "/".join(parts) + " polled"
        if notes:
            summary += f" ({'; '.join(notes)})"
        return summary


register(PfsenseCollector())
