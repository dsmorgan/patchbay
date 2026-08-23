"""phpIPAM collector: subnets, VLANs, and curated address records.

Auth: static app token ("SSL with app token" app), sent as a `token` header.
phpIPAM is the naming layer — its addresses become endpoint hostname/IP
records keyed by MAC where one is recorded.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx

from ..config import Settings
from .. import db
from . import register

NAME = "phpipam"


class PhpIpamCollector:
    name = NAME

    def configured(self, settings: Settings) -> bool:
        return bool(settings.ipam_url and settings.ipam_app_id and settings.ipam_token)

    def _get(self, client: httpx.Client, settings: Settings, path: str) -> Any:
        url = f"{settings.ipam_url.rstrip('/')}/{settings.ipam_app_id}/{path.lstrip('/')}"
        r = client.get(url, headers={"token": settings.ipam_token})
        if r.status_code == 404:  # phpIPAM's "no results", not an error
            return []
        r.raise_for_status()
        body = r.json()
        if not body.get("success"):
            raise RuntimeError(f"phpIPAM {path}: {body.get('message', 'unknown error')}")
        return body.get("data") or []

    def collect(self, settings: Settings, conn: sqlite3.Connection) -> str:
        n_subnets = n_vlans = n_addrs = 0
        with httpx.Client(verify=settings.tls_verify, timeout=15) as client:
            vlans = self._get(client, settings, "vlan/")
            db.save_raw(conn, source=NAME, endpoint="vlan/", payload=vlans)
            for v in vlans:
                vid = int(v.get("number") or 0)
                if vid:
                    conn.execute(
                        "INSERT INTO vlans (vid, name, source, last_seen) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(vid) DO UPDATE SET name=excluded.name, last_seen=excluded.last_seen",
                        (vid, v.get("name"), NAME, db.now()),
                    )
                    n_vlans += 1

            subnets = self._get(client, settings, "subnets/")
            db.save_raw(conn, source=NAME, endpoint="subnets/", payload=subnets)
            vlan_by_internal_id = {str(v["vlanId"]): int(v["number"]) for v in vlans if v.get("vlanId")}
            # fetch the whole address book BEFORE touching the table: a
            # timeout on subnet 40 of 50 must not leave a gutted address book
            fetched: list[tuple[dict, list]] = []
            for s in subnets:
                if not s.get("subnet") or s.get("isFolder") == "1":
                    continue
                fetched.append((s, self._get(client, settings, f"subnets/{s['id']}/addresses/")))
            conn.execute("DELETE FROM ipam_addresses")  # full refresh: IPAM is the master here
            seen_cidrs: list[str] = []
            for s, addrs in fetched:
                cidr = f"{s['subnet']}/{s['mask']}"
                seen_cidrs.append(cidr)
                db.upsert_subnet(
                    conn, cidr=cidr, source=NAME,
                    description=s.get("description"),
                    vlan=vlan_by_internal_id.get(str(s.get("vlanId"))),
                )
                n_subnets += 1
                for a in addrs:
                    if not a.get("ip"):
                        continue
                    # every documented address feeds the drift report...
                    state = {"1": "offline", "2": "used", "3": "reserved", "4": "dhcp"}.get(
                        str(a.get("tag") or ""), None)
                    conn.execute(
                        "INSERT OR REPLACE INTO ipam_addresses "
                        "(ip, hostname, mac, subnet_cidr, description, state, last_seen, "
                        " ipam_id, ipam_subnet_id, ipam_section_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (a["ip"], a.get("hostname") or None,
                         (a.get("mac") or "").strip().lower() or None,
                         cidr, a.get("description") or None, state, db.now(),
                         a.get("id"), s.get("id"), s.get("sectionId")),
                    )
                    n_addrs += 1
                    # ...but only MAC-keyed ones can join the endpoints table
                    mac = (a.get("mac") or "").strip().lower()
                    if mac:
                        db.upsert_endpoint(
                            conn, mac=mac, source=NAME,
                            ip=a.get("ip"), hostname=a.get("hostname"),
                        )
            # Retire subnets phpIPAM no longer documents. The address book
            # above gets a full refresh, but subnets were upsert-only, so one
            # deleted in phpIPAM stayed on the VLAN pages and in drift for
            # good. Guarded on a non-empty listing, like every other prune: an
            # IPAM hiccup returning zero subnets must not wipe the table.
            # phpIPAM is the only writer of `subnets`, so scoping by source is
            # both safe and complete. `vlans` gets no such prune -- three
            # collectors write it and the conflict clause doesn't update
            # `source`, so the column can't say who owns a row.
            if seen_cidrs:
                q = ",".join("?" * len(seen_cidrs))
                conn.execute(
                    f"DELETE FROM subnets WHERE source = ? AND cidr NOT IN ({q})",
                    (NAME, *seen_cidrs))
        return f"{n_subnets} subnets, {n_vlans} vlans, {n_addrs} addresses"


register(PhpIpamCollector())
