"""The routed view's data and layout (ADR-0002, #17).

Networks are vertical rails; everything here is server-side and pure so the
layout unit-tests without a browser. `build_routed_graph()` assembles the
graph JSON; `order_rails()` and `assign_rows()` are the layout engine —
deterministic given the same inventory, so the picture only changes when
attachments actually change. No pins: the escape hatch for a bad ordering
is the PATCHBAY_ROUTED_ORDER declaration, not dragging.
"""

from __future__ import annotations

import ipaddress
import sqlite3
from typing import Any

# rails are keyed by VLAN id where one is known ("v20"), else by the subnet
# ("net:192.0.2.0/24") — a dual-stack VLAN is ONE rail, not two


def _net(cidr: str):
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None


def _addr(ip: str):
    try:
        return ipaddress.ip_address(ip.split("/")[0])
    except ValueError:
        return None


class _Rails:
    """Rail assembly: VLANs ∪ subnets, with containment lookup."""

    def __init__(self) -> None:
        self.rails: dict[str, dict] = {}
        self._nets: list[tuple[Any, str]] = []   # (ip_network, rail key)

    def rail_for_vid(self, vid: int, name: str | None = None) -> dict:
        key = f"v{vid}"
        r = self.rails.setdefault(key, {
            "key": key, "vid": vid, "name": name or "", "subnets": [],
            "routed": False, "gateway": None, "sources": set()})
        if name and not r["name"]:
            r["name"] = name
        return r

    def add_subnet(self, cidr: str, vid: int | None, name: str | None,
                   source: str) -> None:
        net = _net(cidr)
        if net is None:
            return
        if vid is not None:
            r = self.rail_for_vid(vid, name)
        else:
            key = f"net:{cidr}"
            r = self.rails.setdefault(key, {
                "key": key, "vid": None, "name": name or cidr, "subnets": [],
                "routed": False, "gateway": None, "sources": set()})
        if cidr not in r["subnets"]:
            r["subnets"].append(cidr)
            self._nets.append((net, r["key"]))
        r["sources"].add(source)

    def rail_of_ip(self, ip: str) -> str | None:
        a = _addr(ip)
        if a is None:
            return None
        best = None
        for net, key in self._nets:
            if a.version == net.version and a in net:
                if best is None or net.prefixlen > best[0]:
                    best = (net.prefixlen, key)
        return best[1] if best else None


def build_routed_graph(conn: sqlite3.Connection, settings) -> dict:
    rails = _Rails()
    for r in conn.execute("SELECT vid, name, source FROM vlans"):
        rl = rails.rail_for_vid(r["vid"], r["name"])
        rl["sources"].add(r["source"])
    for r in conn.execute("SELECT cidr, vlan, description, source FROM subnets"):
        rails.add_subnet(r["cidr"], r["vlan"], r["description"], r["source"])

    devices = {r["name"]: dict(r) for r in conn.execute(
        "SELECT name, role, parent, status FROM devices")}
    router_names = [n for n, d in devices.items()
                    if d["role"] in ("firewall", "router")]

    # interface legs: device -> {rail key: leg}. A leg remembers its fastest
    # interface for the home rule; kernel ports never count as a leg of the
    # HOST's routed identity twice (vmk + pnic share a MAC, one leg per rail)
    legs: dict[str, dict[str, dict]] = {}
    mac_owner: dict[str, str] = {}
    for r in conn.execute(
            "SELECT d.name AS dev, i.name AS iface, i.ip, i.ip6, i.speed_bps, "
            "i.mac FROM interfaces i JOIN devices d ON d.id = i.device_id"):
        if r["mac"]:
            mac_owner.setdefault(r["mac"].lower(), r["dev"])
        for ip in (r["ip"], r["ip6"]):
            if not ip:
                continue
            key = rails.rail_of_ip(ip)
            if key is None:
                continue
            leg = legs.setdefault(r["dev"], {}).get(key)
            speed = r["speed_bps"] or 0
            if leg is None or speed > leg["speed"]:
                legs.setdefault(r["dev"], {})[key] = {
                    "rail": key, "iface": r["iface"], "ip": ip, "speed": speed}

    # guests: the hypervisor knows a vNIC's port-group VLAN even when no
    # address is visible — that's an attachment to the VLAN's rail
    for r in conn.execute("SELECT mac, vid FROM vnic_vlans WHERE vid NOT IN (0, 4095)"):
        dev = mac_owner.get((r["mac"] or "").lower())
        if not dev:
            continue
        key = f"v{r['vid']}"
        if key in rails.rails and key not in legs.get(dev, {}):
            legs.setdefault(dev, {})[key] = {
                "rail": key, "iface": "?", "ip": None, "speed": 0}

    # routers claim rails: an interface IP inside a subnet routes that
    # network; the interface address is its gateway (shown on hover)
    routers = []
    for name in sorted(router_names):
        claimed = []
        for leg in legs.get(name, {}).values():
            rl = rails.rails[leg["rail"]]
            rl["routed"] = True
            rl["gateway"] = leg["ip"]
            claimed.append(leg["rail"])
        routers.append({"name": name, "rails": sorted(claimed)})

    # default route + WAN health
    default = None
    for r in conn.execute(
            "SELECT device, gateway, flags FROM routes WHERE destination IN "
            "('0.0.0.0/0', '::/0') ORDER BY proto"):
        if any(f in (r["flags"] or "") for f in ("B", "R")):
            continue
        if default is None:
            default = {"device": r["device"], "gateway": r["gateway"]}
    gws = [dict(r) for r in conn.execute(
        "SELECT name, address, status, loss, delay FROM gateways")]

    # hosts: multi-homed devices draw once; single-homed collapse to a count.
    # Routers live in the routing tier, never as host boxes.
    hosts, single = [], {k: 0 for k in rails.rails}
    for dev, dl in sorted(legs.items()):
        if dev in router_names:
            continue
        if len(dl) >= 2:
            ordered = list(dl.values())
            hosts.append({
                "name": dev, "role": devices.get(dev, {}).get("role"),
                "legs": ordered, "home": _home_rail(ordered, rails.rails)})
        elif len(dl) == 1:
            single[next(iter(dl))] += 1

    # endpoints with an address land in their rail's count when they aren't
    # already a device (their MAC would have made them one)
    seen_devs = {d.lower() for d in devices}
    for r in conn.execute("SELECT hostname, ip FROM endpoints WHERE ip IS NOT NULL"):
        if (r["hostname"] or "").lower() in seen_devs:
            continue
        key = rails.rail_of_ip(r["ip"])
        if key:
            single[key] += 1

    # a rail earns its place by participating: a router routes it, a drawn
    # host has a leg on it, or single-homed hosts count against it. IPAM
    # alone can't put a network on the map — supernets, aggregates, and
    # VLANs nothing claims are /vlans material, where documentation vs
    # reality is the point.
    attached = {l["rail"] for h in hosts for l in h["legs"]}
    live = {k: r for k, r in rails.rails.items()
            if r["routed"] or single.get(k, 0) or k in attached}

    order = order_rails(live, hosts, declared=getattr(
        settings, "routed_order", ()) or ())
    pos = {k: i for i, k in enumerate(order)}
    for h in hosts:
        h["legs"].sort(key=lambda l: pos.get(l["rail"], 0))
    hosts.sort(key=lambda h: pos.get(h["home"], 0))
    rows = assign_rows(hosts, pos)
    for h, row in zip(hosts, rows):
        h["row"] = row

    out_rails = []
    for k in order:
        r = live[k]
        out_rails.append({**r, "sources": sorted(r["sources"]),
                          "hosts": single.get(k, 0)})
    return {
        "rails": out_rails,
        "routers": routers,
        "default": default,
        "gateways": gws,
        "hosts": hosts,
        "wan_names": list(getattr(settings, "wan_names", ()) or ()),
    }


def _home_rail(legs: list[dict], rails: dict[str, dict]) -> str:
    """The home rail: fastest interface's network, ties broken by highest
    VLAN id, then first-seen. Chosen over 'the mgmt interface' because mgmt
    legs share one VLAN and would stack every box on the same rail."""
    def score(i_leg):
        i, leg = i_leg
        vid = rails[leg["rail"]].get("vid") or -1
        return (-(leg["speed"] or 0), -vid, i)
    return min(enumerate(legs), key=score)[1]["rail"]


def order_rails(rails: dict[str, dict], hosts: list[dict],
                declared: tuple[str, ...] = ()) -> list[str]:
    """Left-to-right rail order: VLAN-number order by default, then a greedy
    adjacent-swap pass pulls a multi-homed host's networks together to
    shorten its attachment lines. Deterministic. A PATCHBAY_ROUTED_ORDER
    declaration pins named rails to the front, in the declared order."""
    def base_key(k: str):
        vid = rails[k].get("vid")
        return (0, vid) if vid is not None else (1, rails[k]["name"])
    order = sorted(rails, key=base_key)

    def cost(o: list[str]) -> int:
        pos = {k: i for i, k in enumerate(o)}
        total = 0
        for h in hosts:
            ps = [pos[l["rail"]] for l in h["legs"] if l["rail"] in pos]
            if len(ps) > 1:
                total += max(ps) - min(ps)
        return total

    improved = True
    guard = 0
    while improved and guard < 50:
        improved, guard = False, guard + 1
        for i in range(len(order) - 1):
            cand = order[:i] + [order[i + 1], order[i]] + order[i + 2:]
            if cost(cand) < cost(order):
                order = cand
                improved = True

    if declared:
        by_name = {}
        for k, r in rails.items():
            by_name[str(r.get("vid"))] = k
            if r.get("name"):
                by_name[r["name"].lower()] = k
        pinned = [by_name[d.strip().lower()] for d in declared
                  if d.strip().lower() in by_name]
        rest = [k for k in order if k not in pinned]
        order = pinned + rest
    return order


def assign_rows(hosts: list[dict], pos: dict[str, int]) -> list[int]:
    """Give each host box a row so that no two hosts whose attachment spans
    overlap share one — greedy interval coloring, stable for stable input."""
    spans = []
    for h in hosts:
        ps = [pos.get(l["rail"], 0) for l in h["legs"]]
        spans.append((min(ps), max(ps)))
    rows: list[int] = []
    row_end: list[int] = []          # rightmost occupied position per row
    for lo, hi in spans:
        for i, end in enumerate(row_end):
            if end < lo:
                row_end[i] = hi
                rows.append(i)
                break
        else:
            row_end.append(hi)
            rows.append(len(row_end) - 1)
    return rows
