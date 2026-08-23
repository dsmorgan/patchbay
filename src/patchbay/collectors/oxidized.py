"""Oxidized collector: parse per-port VLAN membership out of device configs.

The running configs Oxidized backs up are the authoritative record of access
and trunk membership — richer than anything SNMP exposes on these switches.
Configs are fetched over the REST API, parsed in memory, and DISCARDED: they
contain secrets and must never land in patchbay's database. Only the derived
port/VLAN facts are stored.
"""

from __future__ import annotations

import re
import sqlite3

import httpx

from ..config import Settings
from .. import db
from . import register
from ..ports import port_kind

NAME = "oxidized"


def _expand_range(a: str, b: str) -> list[str]:
    """FastIron 'ethe 1/2/1 to 1/2/3' — the range varies the last component."""
    pa, pb = a.rsplit("/", 1), b.rsplit("/", 1)
    if pa[0] != pb[0] or not (pa[1].isdigit() and pb[1].isdigit()):
        return [a, b]
    return [f"{pa[0]}/{i}" for i in range(int(pa[1]), int(pb[1]) + 1)]


def parse_ironware(text: str) -> tuple[dict[int, str], list[tuple[str, int, bool]],
                                       list[tuple[str, str, str]]]:
    """FastIron vlan blocks -> ({vid: name}, [(interface, vid, tagged)],
    [(interface, role, detail)]).

    Ports untagged nowhere fall into the default VLAN implicitly, which the
    caller resolves against the interface table.
    """
    names: dict[int, str] = {}
    rows: list[tuple[str, int, bool]] = []
    roles: list[tuple[str, str, str]] = []
    vid = None
    iface = None
    for line in text.splitlines():
        m = re.match(r"^vlan (\d+)(?: name (\S+))?", line)
        if m:
            vid = int(m.group(1))
            names[vid] = m.group(2) or ""
            iface = None
            continue
        # global mirror-port designates the destination a source port copies to
        m = re.match(r"^mirror-port ethe(?:rnet)?\s+(\S+)", line)
        if m:
            roles.append((f"ethernet{m.group(1)}", "monitor-dst", "mirror destination"))
            continue
        if not line.startswith(" "):
            vid = None
            m = re.match(r"^interface ethe(?:rnet)?\s+(\S+)", line)
            iface = f"ethernet{m.group(1)}" if m else None
            continue
        m = re.match(r"^ monitor ethe(?:rnet)?\s+(\S+)(?:\s+(\S+))?", line)
        if m and iface:
            roles.append((iface, "monitor-src",
                          f"mirrored to ethernet{m.group(1)}"
                          + (f" ({m.group(2)})" if m.group(2) else "")))
            continue
        m = re.match(r"^ (tagged|untagged) (.+)$", line)
        if m and vid is not None:
            ports: list[str] = []
            for pm in re.finditer(r"ethe(?:rnet)?\s+(\S+)(?:\s+to\s+(\S+))?", m.group(2)):
                ports += _expand_range(pm.group(1), pm.group(2)) if pm.group(2) else [pm.group(1)]
            rows += [(f"ethernet{p}", vid, m.group(1) == "tagged") for p in ports]
    return names, rows, roles


def _vids(s: str) -> set[int]:
    out: set[int] = set()
    for part in s.replace(",", " ").split():
        if "-" in part:
            a, b = part.split("-", 1)
            if a.isdigit() and b.isdigit() and int(b) - int(a) < 4096:
                out |= set(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.add(int(part))
    return out


def _netgear_monitor(text: str) -> list[tuple[str, str, str]]:
    """M4300 SPAN sessions:

      monitor session 1 source vlan 1
      monitor session 1 destination interface 1/0/14

    A destination port transmits copies of the source traffic. It doesn't
    forward, learn, or advertise, so no discovery protocol can ever say what
    it's plugged into — only a declaration can.
    """
    srcs: dict[str, list[str]] = {}
    dsts: dict[str, str] = {}
    for m in re.finditer(r"^\s*monitor session (\d+) (source|destination) "
                         r"(vlan|interface) (\S+)", text, re.M):
        sess, side, kind, what = m.groups()
        if side == "destination" and kind == "interface":
            dsts[sess] = what
        elif side == "source":
            srcs.setdefault(sess, []).append(f"{kind} {what}")
    roles: list[tuple[str, str, str]] = []
    for sess, port in dsts.items():
        what = ", ".join(srcs.get(sess, [])) or "no source configured"
        roles.append((port, "monitor-dst", f"session {sess} mirrors {what}"))
    for sess, sources in srcs.items():
        for s in sources:
            if s.startswith("interface "):
                roles.append((s.removeprefix("interface "), "monitor-src",
                              f"session {sess} mirrored to "
                              f"{dsts.get(sess, 'an unconfigured port')}"))
    return roles


def parse_netgear(text: str) -> tuple[dict[int, str], list[tuple[str, int, bool]],
                                      list[tuple[str, str, str]]]:
    """M4300 running-config, both dialects:

      vlan database                      interface 1/0/3
      vlan 13,14,21-24,73                 switchport mode access
      vlan name 13 "VM13"                 switchport access vlan 73
                                         interface 1/0/11
      interface 1/0/16                    switchport mode trunk
       vlan participation include 24      switchport trunk native vlan 13
       vlan pvid 24                       switchport trunk allowed vlan 1,24,73
       vlan tagging 24

    'Trunk Allowed VLANs 1-4093' is the factory default meaning "everything
    that exists", so allowed/tagged sets are clipped to the VLAN database.
    """
    names: dict[int, str] = {}
    existing: set[int] = set()
    for m in re.finditer(r"^\s*vlan ([\d,\- ]+)\s*$", text, re.M):
        existing |= _vids(m.group(1))
    if existing:
        existing.add(1)  # the default VLAN exists even if the database omits it
    for m in re.finditer(r'^\s*vlan name (\d+) "?([^"\n]*)"?\s*$', text, re.M):
        names[int(m.group(1))] = m.group(2)
    for v in existing:
        names.setdefault(v, "")

    rows: list[tuple[str, int, bool]] = []
    iface = None
    tagged: set[int] = set()
    member: set[int] = set()
    pvid: int | None = None
    trunk_mode = False

    def flush() -> None:
        nonlocal tagged, member, pvid, trunk_mode
        if iface:
            tag = tagged & existing if existing else tagged
            if trunk_mode and not tag:
                # a bare `switchport mode trunk` (no allowed-list) is the
                # factory default: every VLAN that exists passes tagged
                tag = set(existing)
            if pvid is None and (member or tag) and 1 in existing:
                # M4300 keeps untagged VLAN 1 by default; being a default it
                # never appears in running-config, so imply it on any port
                # that has VLAN config but no explicit pvid/access/native
                pvid = 1
            for v in (member & existing if existing else member) | tag | ({pvid} if pvid else set()):
                rows.append((iface, v, v in tag and v != pvid))
        tagged, member, pvid, trunk_mode = set(), set(), None, False

    def last_int(s: str) -> int | None:
        p = s.rsplit(" ", 1)[-1]
        return int(p) if p.isdigit() else None

    for line in text.splitlines():
        m = re.match(r"^\s*interface (\d+/\d+/\d+)\s*$", line)
        if m:
            flush()
            iface = m.group(1)
            continue
        s = line.strip()
        # M4300 blocks aren't indented; they close with `exit`
        if s == "exit" and iface:
            flush()
            iface = None
        if iface:
            if s.startswith("vlan participation include "):
                member |= _vids(s.removeprefix("vlan participation include "))
            elif s.startswith("vlan tagging "):
                tagged |= _vids(s.removeprefix("vlan tagging "))
            elif s.startswith("vlan pvid ") or s.startswith("switchport access vlan ") \
                    or s.startswith("switchport trunk native vlan "):
                pvid = last_int(s) or pvid
            elif s.startswith("switchport trunk allowed vlan "):
                tagged |= _vids(s.removeprefix("switchport trunk allowed vlan "))
            elif s == "switchport mode trunk":
                trunk_mode = True
    flush()
    return names, rows, _netgear_monitor(text)


PARSERS = {"ironware": parse_ironware, "netgear": parse_netgear}


class OxidizedCollector:
    name = NAME

    def configured(self, settings: Settings) -> bool:
        return bool(settings.oxidized_url)

    def collect(self, settings: Settings, conn: sqlite3.Connection) -> str:
        base = settings.oxidized_url.rstrip("/")
        parsed = []
        with httpx.Client(verify=settings.tls_verify, timeout=20) as client:
            r = client.get(f"{base}/nodes.json")
            r.raise_for_status()
            for node in r.json():
                model = (node.get("model") or "").lower()
                parser = PARSERS.get(model)
                status = (node.get("last") or {}).get("status") or node.get("status")
                if parser is None or status != "success":
                    continue
                full = node.get("full_name") or node.get("name")
                cr = client.get(f"{base}/node/fetch/{full}")
                if cr.status_code != 200:
                    continue
                dev = (node.get("name") or full).split(".")[0].lower()
                # config text parsed, then dropped
                names, rows, roles = parser(cr.text)
                if not rows and not names:
                    # a fetched config that parses to nothing is a dialect the
                    # parser doesn't understand (firmware change, truncated
                    # backup) — keep the last good membership, don't wipe it
                    continue
                if model == "ironware" and 1 in names:
                    # FastIron: ports untagged in no VLAN sit in the default VLAN
                    untagged_elsewhere = {i for i, v, t in rows if not t and v != 1}
                    physical = [x[0] for x in conn.execute(
                        "SELECT i.name FROM interfaces i JOIN devices d ON d.id = i.device_id "
                        "WHERE d.name = ?", (dev,)) if port_kind(x[0]) == "physical"]
                    rows += [(p, 1, False) for p in physical
                             if p not in untagged_elsewhere
                             and not any(r0 == p and v0 == 1 for r0, v0, _ in rows)]
                conn.execute("DELETE FROM port_vlans WHERE device = ?", (dev,))
                conn.execute("DELETE FROM port_roles WHERE device = ? AND source = ?",
                             (dev, NAME))
                for iface, role, detail in roles:
                    conn.execute(
                        "INSERT OR REPLACE INTO port_roles "
                        "(device, interface, role, detail, source) VALUES (?, ?, ?, ?, ?)",
                        (dev, iface, role, detail, NAME))
                # config evidence replaces prior trunk *assumptions* (but never
                # a live Q-BRIDGE report) for this device
                conn.execute("DELETE FROM device_vlans WHERE device = ? AND source = 'trunk'",
                             (dev,))
                for iface, vid, tag in rows:
                    conn.execute(
                        "INSERT OR REPLACE INTO port_vlans (device, interface, vid, tagged, source) "
                        "VALUES (?, ?, ?, ?, ?)", (dev, iface, vid, int(tag), NAME))
                    conn.execute(
                        "INSERT OR IGNORE INTO device_vlans (device, vid, source) "
                        "VALUES (?, ?, 'config')", (dev, vid))
                for vid, vname in names.items():
                    conn.execute(
                        "INSERT INTO vlans (vid, name, source, last_seen) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(vid) DO UPDATE SET last_seen=excluded.last_seen, "
                        "name=COALESCE(vlans.name, excluded.name)",
                        (vid, vname or None, NAME, db.now()))
                parsed.append(f"{dev}: {len(rows)} port/vlan rows"
                              + (f", {len(roles)} monitor ports" if roles else ""))
        return "; ".join(parsed) if parsed else "no parseable configs"


register(OxidizedCollector())
