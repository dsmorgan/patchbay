"""OPNsense collector: interfaces, gateway health, ARP, and DHCP leases.

Auth: API key/secret (HTTP basic). ACLs are the GUI page privileges of the
API user; a 403 here names the missing privilege rather than failing the
whole poll, so partial grants degrade gracefully.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any

import httpx

from ..config import Settings
from .. import db
from . import register

# --- firewall config history (#23) ------------------------------------------
# config.xml is pulled over the same API key as everything else (grant the
# firewall's backup page privilege). It carries live private keys, so the raw
# document is redacted BEFORE anything touches the database: the content of
# every secret-bearing element is replaced with a short hash of itself —
# a rotated key still shows as a change, but no secret is ever stored.
# Overzealous by design, same stance as the snapshot scrubber.
_SECRET_TAGS = ("prv", "crt", "password", "pass", "secret", "psk",
                "pre-shared-key", "private-key", "privatekey", "privkey",
                "sharedkey", "authorizedkeys")
_SECRET_RE = re.compile(
    r"<(" + "|".join(re.escape(t) for t in _SECRET_TAGS) + r")>([^<]+)</\1>")
# the <revision> block updates on every save — its description/username are
# the change's metadata (lifted onto the revision row); its timestamp churn
# must not read as a config change or clutter the diffs
_REVISION_RE = re.compile(r"\s*<revision>.*?</revision>", re.S)

CONFIG_REVISIONS_KEEP = 50   # per device; history beyond this is trimmed


def _digest(m: re.Match) -> str:
    tag, content = m.group(1), m.group(2)
    stamp = hashlib.sha256(content.encode()).hexdigest()[:8]
    return f"<{tag}>redacted:{stamp}</{tag}>"


def prepare_config(raw: str) -> tuple[str, str | None, str | None]:
    """Redact secrets and strip revision noise from a raw config.xml.
    Returns (stored text, change description, change author)."""
    msg = auth = None
    rev = _REVISION_RE.search(raw)
    if rev:  # only trust description/username found inside the revision block
        msg_m = re.search(r"<description>([^<]*)</description>", rev.group(0))
        auth_m = re.search(r"<username>([^<]*)</username>", rev.group(0))
        msg = (msg_m.group(1).strip() or None) if msg_m else None
        auth = (auth_m.group(1).strip() or None) if auth_m else None
    text = _REVISION_RE.sub("", raw)
    text = _SECRET_RE.sub(_digest, text)
    return text, msg, auth


def save_config_revision(conn: sqlite3.Connection, device: str, raw: str) -> bool:
    """Store a new redacted revision if the config actually changed.
    Returns True when a new revision was written."""
    text, msg, auth = prepare_config(raw)
    sha = hashlib.sha256(text.encode()).hexdigest()
    last = conn.execute(
        "SELECT sha FROM config_revisions WHERE device = ? "
        "ORDER BY fetched_at DESC, id DESC LIMIT 1", (device,)).fetchone()
    if last and last["sha"] == sha:
        return False
    conn.execute(
        "INSERT INTO config_revisions (device, fetched_at, sha, message, author, text) "
        "VALUES (?, ?, ?, ?, ?, ?)", (device, db.now(), sha, msg, auth, text))
    conn.execute(
        "DELETE FROM config_revisions WHERE device = ? AND id NOT IN "
        "(SELECT id FROM config_revisions WHERE device = ? "
        " ORDER BY fetched_at DESC, id DESC LIMIT ?)",
        (device, device, CONFIG_REVISIONS_KEEP))
    return True

NAME = "opnsense"

_TUNNEL_PREFIXES = ("tun", "ovpn", "gif", "gre", "ipsec", "wg")


def _parse_line_rate(stats: dict) -> int | None:
    raw = stats.get("line rate", "")  # "1000000000 bit/s"
    try:
        return int(raw.split()[0]) if raw else None
    except (ValueError, IndexError):
        return None


def base_and_name(host: str) -> tuple[str, str]:
    """API base and short device name from OPNSENSE_HOST, which is either a
    bare hostname (https assumed) or a scheme-prefixed URL for installations
    that don't terminate TLS on the management interface."""
    base = f"{host.rstrip('/')}/api" if "://" in host else f"https://{host}/api"
    name = host.split("://")[-1].split(":")[0].split(".")[0]
    return base, name


class OpnsenseCollector:
    name = NAME

    def configured(self, settings: Settings) -> bool:
        return bool(settings.opnsense_host and settings.opnsense_api_key
                    and settings.opnsense_api_secret)

    def collect(self, settings: Settings, conn: sqlite3.Connection) -> str:
        base, short_name = base_and_name(settings.opnsense_host)
        auth = (settings.opnsense_api_key, settings.opnsense_api_secret)
        notes: list[str] = []

        def get(path: str) -> Any | None:
            r = client.get(f"{base}/{path}", auth=auth)
            if r.status_code == 403:
                notes.append(f"{path}: 403 (grant the matching page privilege)")
                return None
            r.raise_for_status()
            return r.json()

        def get_text(path: str) -> str | None:
            r = client.get(f"{base}/{path}", auth=auth)
            if r.status_code == 403:
                notes.append(f"{path}: 403 (grant the matching page privilege)")
                return None
            r.raise_for_status()
            return r.text

        with httpx.Client(verify=settings.tls_verify, timeout=20) as client:
            # the firewall knows what it is — SNMP only sees "FreeBSD/amd64"
            fw = get("core/firmware/info") or {}
            version = fw.get("product_version")
            mgmt_hostname = settings.opnsense_host.split("://")[-1].split(":")[0]
            dev_id = db.upsert_device(conn, name=short_name,
                                      source=NAME, role="firewall", status="up",
                                      vendor="OPNsense",
                                      os=(f"opnsense {version}" if version else None),
                                      mgmt_ip=mgmt_hostname)

            ifaces = get("interfaces/overview/export")
            vlan_rows: list[tuple] = []
            n_ifaces = 0
            # truthiness, not `is not None`: a 200 with an empty body must
            # not run the delete-then-insert or the tunnel purge below —
            # same empty-response stance as the librenms/unifi fdb paths
            if ifaces:
                db.save_raw(conn, source=NAME, endpoint="interfaces/overview/export", payload=ifaces)
                for i in ifaces if isinstance(ifaces, list) else []:
                    name = i.get("device") or i.get("identifier")
                    if not name:
                        continue
                    if name.lower().startswith(_TUNNEL_PREFIXES):
                        continue
                    enabled = i.get("enabled")
                    admin_status = ("up" if enabled else "down") if enabled is not None else None
                    vlan_tag = i.get("vlan_tag")
                    if vlan_tag is not None:
                        try:
                            vlan_rows.append((short_name, name, int(vlan_tag), NAME))
                        except (ValueError, TypeError):
                            pass
                    db.upsert_interface(
                        conn, device_id=dev_id, name=name,
                        oper_status="up" if i.get("status") == "up" else i.get("status"),
                        admin_status=admin_status,
                        mac=(i.get("macaddr") or None),
                        description=i.get("description"),
                        ip=(i.get("addr4") or None),
                        ip6=(i.get("addr6") or None),
                        speed_bps=_parse_line_rate(i.get("statistics") or {}),
                    )
                    n_ifaces += 1
                # This collector owns its port_vlans rows: delete-then-insert,
                # so a VLAN sub-interface removed from OPNsense leaves the
                # model too. Only when the export succeeded — a 403 above must
                # not wipe rows that are still true.
                conn.execute("DELETE FROM port_vlans WHERE source = ? AND device = ?",
                             (NAME, short_name))
                conn.executemany(
                    "INSERT INTO port_vlans (device, interface, vid, tagged, source) "
                    "VALUES (?, ?, ?, 0, ?) ON CONFLICT(device, interface, vid) DO NOTHING",
                    vlan_rows)
                # Purge tunnel interfaces written before the filter was in
                # place. interfaces has no source column, so target by prefix.
                conn.execute(
                    "DELETE FROM interfaces WHERE device_id = ? AND ("
                    "name LIKE 'tun%' OR name LIKE 'ovpn%' OR name LIKE 'gif%' "
                    "OR name LIKE 'gre%' OR name LIKE 'ipsec%' OR name LIKE 'wg%')",
                    (dev_id,),
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
                fw_name = short_name
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

            # firewall config history (#23): the raw XML is redacted and
            # noise-stripped by save_config_revision before storage; the raw
            # document itself is discarded here and never saved to raw_payloads
            cfg = get_text("core/backup/download/this")
            new_rev = False
            if cfg and cfg.lstrip().startswith("<"):
                new_rev = save_config_revision(conn, short_name, cfg)

        summary = f"{n_ifaces} interfaces/gateways/arp/leases polled"
        if new_rev:
            summary += ", new config revision"
        if notes:
            summary += f" ({'; '.join(notes)})"
        return summary


register(OpnsenseCollector())
