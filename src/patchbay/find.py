"""Where is it? One lookup for a MAC, an IP address, a hostname, or an alias.

`find` answers from the model the pages draw: the device a name or address
belongs to, the switch ports that learned a MAC, the port the normalizer
placed it on, the cables at either end, the IPAM record, and whether the
topology and routed maps draw it. The MCP `find` tool (ADR-0004) calls it,
and a search box in the UI can too.

Every result is built from named columns, never whole rows, so a column
added later can't leak into an answer. The function only reads.
"""

from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
from datetime import datetime, timezone

from .normalize import canon_mac, canonical_name

# Each list in a result stops at this many entries; `truncated` says so.
FIND_LIMIT = 50

# The MAC spellings people paste: colons or hyphens between six octets
# (02:00:5e:10:00:01, 02-00-5E-10-00-01), with leading zeros dropped as
# macOS `arp -a` prints them (2:0:5e:10:0:1), Cisco dotted (0200.5e10.0001),
# HP hyphenated (02005e-100001, 0200-5e10-0001), or 12 bare hex digits.
# Six colon groups can't be an IPv6 address: that takes eight, or a "::",
# and an empty group never matches here.
_OCTETS_RE = re.compile(r"^[0-9a-f]{1,2}([:-])(?:[0-9a-f]{1,2}\1){4}[0-9a-f]{1,2}$", re.I)
_MAC_RE = re.compile(
    r"^(?:[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}"
    r"|[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}"
    r"|[0-9a-f]{6}-[0-9a-f]{6}"
    r"|[0-9a-f]{12})$", re.I)


def classify(query: str) -> tuple[str, str]:
    """(kind, normalized query): "mac" with the colon form, "ip" with the
    compressed address, or "name" with the canonical name."""
    q = (query or "").strip()
    if _OCTETS_RE.match(q):
        return "mac", ":".join(o.zfill(2) for o in re.split("[:-]", q.lower()))
    if _MAC_RE.match(q):
        h = canon_mac(q)
        return "mac", ":".join(h[i:i + 2] for i in range(0, 12, 2))
    try:
        return "ip", str(ipaddress.ip_address(q.split("/", 1)[0].split("%", 1)[0]))
    except ValueError:
        return "name", canonical_name(q)


def _same_ip(stored: str | None, want: str) -> bool:
    if not stored:
        return False
    try:
        return str(ipaddress.ip_address(
            stored.split("/", 1)[0].split("%", 1)[0])) == want
    except ValueError:
        return False


def iso_utc(ts: float | None) -> str | None:
    """Epoch seconds as UTC ISO 8601, or None."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


# MAC columns compared in canonical hex, whatever spelling a source stored
_HEX = "replace(replace(replace(lower({}), ':', ''), '-', ''), '.', '')"


def _capped(rows: list[dict], out: dict) -> list[dict]:
    if len(rows) > FIND_LIMIT:
        out["truncated"] = True
    return rows[:FIND_LIMIT]


def find(conn: sqlite3.Connection, settings, query: str) -> dict:
    """Look `query` up across the model. Returns a dict with `query`,
    `kind` (mac, ip, or name), `found`, and lists: `devices` (devices the
    query names or whose interface it matches), `endpoints` (where the
    normalizer placed each matching MAC), `mac_table` (every switch port
    that learned one, with the device at the far end when the port carries
    a cable, which makes it a transit port rather than the host's own),
    `links` (cables at a matched device), `ipam` (IPAM records), `maps`
    (the labels each map draws for it), and `notes` (why something isn't
    drawn, where the model says)."""
    kind, q = classify(query)
    out: dict = {"query": query, "kind": kind, "found": False, "devices": [],
                 "endpoints": [], "mac_table": [], "links": [], "ipam": [],
                 "maps": {"topology": [], "routed": []}, "notes": [],
                 "truncated": False}
    if not q:
        return out
    alias_map = {a.lower(): c for a, c in conn.execute(
        "SELECT alias, canonical FROM aliases")}
    devices = {r["name"]: r for r in conn.execute(
        "SELECT name, role, status, mgmt_ip, parent, vendor, model, last_seen "
        "FROM devices")}

    dev_hits: dict[str, dict] = {}

    def hit_device(name: str, matched: str, interface: str | None = None) -> None:
        d = devices.get(name)
        if d is None or name in dev_hits:
            return
        dev_hits[name] = {
            "name": name, "role": d["role"], "status": d["status"],
            "mgmt_ip": d["mgmt_ip"], "parent": d["parent"],
            "vendor": d["vendor"], "model": d["model"],
            "last_seen": iso_utc(d["last_seen"]),
            "matched": matched, "interface": interface}

    macs: set[str] = set()          # canonical hex of every MAC in play
    names: set[str] = set()         # canonical names of every host in play
    ips: set[str] = set()

    if kind == "mac":
        macs.add(canon_mac(q))
    elif kind == "ip":
        ips.add(q)
        for name, d in devices.items():
            if _same_ip(d["mgmt_ip"], q):
                hit_device(name, "management address")
    else:
        names.add(q)
        target = alias_map.get(query.strip().lower()) or alias_map.get(q)
        if q in devices:
            hit_device(q, "name")
        if target in devices:
            hit_device(target, "alias")
            names.add(target)

    # interfaces: a device's own MAC or address names the device
    for r in conn.execute(
            "SELECT d.name AS dev, i.name AS iface, i.mac, i.ip, i.ip6 "
            "FROM interfaces i JOIN devices d ON d.id = i.device_id"):
        if kind == "mac" and r["mac"] and canon_mac(r["mac"]) in macs:
            hit_device(r["dev"], "interface MAC", r["iface"])
        elif kind == "ip" and (_same_ip(r["ip"], q) or _same_ip(r["ip6"], q)):
            hit_device(r["dev"], "interface address", r["iface"])
            if r["mac"]:
                macs.add(canon_mac(r["mac"]))

    # endpoints: where the normalizer placed a MAC
    ep_rows = []
    for r in conn.execute(
            "SELECT mac, ip, hostname, device, interface, vlan, source, last_seen "
            "FROM endpoints ORDER BY mac"):
        hn = canonical_name(r["hostname"] or "")
        hn = alias_map.get(hn, hn)
        if ((kind == "mac" and canon_mac(r["mac"]) in macs)
                or (kind == "ip" and _same_ip(r["ip"], q))
                or (kind == "name" and hn and hn in names)):
            ep_rows.append(r)
    for r in ep_rows:
        macs.add(canon_mac(r["mac"]))
        if r["ip"]:
            ips.add(r["ip"].split("/", 1)[0])
        if r["hostname"]:
            hn = canonical_name(r["hostname"])
            names.add(alias_map.get(hn, hn))
    out["endpoints"] = _capped([
        {"mac": r["mac"], "ip": r["ip"], "hostname": r["hostname"],
         "device": r["device"], "interface": r["interface"], "vlan": r["vlan"],
         "source": r["source"], "last_seen": iso_utc(r["last_seen"])}
        for r in ep_rows], out)

    # a device found by name brings its own MACs, so the MAC table can say
    # which switch port its NICs are on
    for name in list(dev_hits):
        for r in conn.execute(
                "SELECT i.mac FROM interfaces i JOIN devices d ON d.id = i.device_id "
                "WHERE d.name = ? AND i.mac IS NOT NULL", (name,)):
            macs.add(canon_mac(r["mac"]))
    names.update(dev_hits)

    # IPAM: by address, MAC, or name
    ipam_rows = []
    for r in conn.execute(
            "SELECT ip, hostname, mac, subnet_cidr, state, description "
            "FROM ipam_addresses ORDER BY ip"):
        hn = canonical_name(r["hostname"] or "")
        hn = alias_map.get(hn, hn)
        if ((r["ip"] and any(_same_ip(r["ip"], i) for i in ips | {q}))
                or (r["mac"] and canon_mac(r["mac"]) in macs)
                or (hn and hn in names)):
            ipam_rows.append(r)
    out["ipam"] = _capped([
        {"ip": r["ip"], "hostname": r["hostname"], "mac": r["mac"],
         "subnet": r["subnet_cidr"], "state": r["state"],
         "description": r["description"]} for r in ipam_rows], out)

    # cables: every link end at a matched device, seen from that device
    link_rows = conn.execute(
        "SELECT a_device, a_interface, b_device, b_interface, source FROM links "
        "ORDER BY a_device, a_interface").fetchall()
    links = []
    for r in link_rows:
        for dev, iface, peer, piface in (
                (r["a_device"], r["a_interface"], r["b_device"], r["b_interface"]),
                (r["b_device"], r["b_interface"], r["a_device"], r["a_interface"])):
            if dev in names or alias_map.get(dev.lower()) in names:
                links.append({"device": dev, "interface": iface, "peer": peer,
                              "peer_interface": piface, "source": r["source"]})
    out["links"] = _capped(links, out)

    # the MAC table: every port that learned one of the MACs. A port that
    # carries a cable to another device is transit, and says where it leads.
    toward: dict[tuple[str, str], str] = {}
    for r in link_rows:
        toward.setdefault((r["a_device"], r["a_interface"]), r["b_device"])
        toward.setdefault((r["b_device"], r["b_interface"]), r["a_device"])
    fdb = []
    if macs:
        marks = ",".join("?" * len(macs))
        for r in conn.execute(
                f"SELECT device, interface, mac, vlan, source FROM fdb "
                f"WHERE {_HEX.format('mac')} IN ({marks}) "
                "ORDER BY device, interface, vlan", sorted(macs)):
            far = toward.get((r["device"], r["interface"]))
            fdb.append({"device": r["device"], "interface": r["interface"],
                        "mac": r["mac"], "vlan": r["vlan"] or None,
                        "source": r["source"],
                        # the subject's own cable is its port, not transit
                        "toward": (None if far in names else far)})
    out["mac_table"] = _capped(fdb, out)
    out["devices"] = _capped(sorted(dev_hits.values(), key=lambda d: d["name"]), out)

    out["found"] = any(out[k] for k in ("devices", "endpoints", "mac_table",
                                        "links", "ipam"))
    if not out["found"]:
        return out
    labels = {n.lower() for n in names}
    labels |= {r["hostname"].lower() for r in [*ep_rows, *ipam_rows] if r["hostname"]}
    labels |= {i.lower() for i in ips}
    out["maps"] = _drawn(conn, settings, labels)
    out["notes"] = _notes(out)
    return out


def _drawn(conn: sqlite3.Connection, settings, labels: set[str]) -> dict:
    """The labels each map draws for the subject: its topology node names
    and its routed-view boxes, chips, guests, or clients."""
    from . import routed
    # build_topology_graph still lives with its page; ADR-0004's next stage
    # moves it out and returns a dict, and this parse goes away
    from .web import build_topology_graph

    graph_json, _ = build_topology_graph(conn, settings)
    topo = {n["name"] for n in json.loads(graph_json)["nodes"]}
    routed_names = routed.drawn_names(routed.build_routed_graph(conn, settings))
    return {"topology": sorted(n for n in topo if n.lower() in labels),
            "routed": sorted(n for n in routed_names if n.lower() in labels)}


def _notes(out: dict) -> list[str]:
    """Why the maps leave something out, where the model says. Each note
    restates a placement rule; none guesses."""
    notes = []
    drawn = out["maps"]["topology"] or out["maps"]["routed"]
    if out["ipam"] and not (out["devices"] or out["endpoints"] or out["mac_table"]):
        notes.append("Only IPAM records it. IPAM names and fills in hosts but "
                     "never places one: no source has observed it on the network.")
    if not drawn and out["mac_table"] and not any(
            e["hostname"] or e["ip"] for e in out["endpoints"]) and not out["devices"]:
        notes.append("A switch learned the MAC, but no source gives it a "
                     "hostname or address, so the maps count it without "
                     "drawing it (often a bond member or kernel port of a "
                     "host already on the map).")
    if out["mac_table"] and all(f["toward"] for f in out["mac_table"]):
        behind = sorted({f["toward"] for f in out["mac_table"]})
        notes.append("Every switch port that learned the MAC carries a cable "
                     f"to another device ({', '.join(behind)}), so it sits "
                     "behind that device rather than on a port of its own.")
    for d in out["devices"]:
        if d["parent"] and d["name"] not in out["maps"]["topology"]:
            notes.append(f"{d['name']} is a guest of {d['parent']}: the "
                         "topology counts guests on their hypervisor's node "
                         "instead of drawing them.")
    return notes
