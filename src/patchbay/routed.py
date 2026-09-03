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


def _local_admin(mac: str) -> bool:
    """Locally-administered MAC: randomized privacy addresses (phones,
    tablets). Their hostnames are weak identity — four iPads all announce
    "iPad" — so they never take part in hostname fusion."""
    try:
        return bool(int(mac[:2], 16) & 0x02)
    except (ValueError, IndexError):
        return False


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
            "SELECT device, gateway, interface, flags FROM routes WHERE "
            "destination IN ('0.0.0.0/0', '::/0') ORDER BY proto"):
        if any(f in (r["flags"] or "") for f in ("B", "R")):
            continue
        if default is None:
            default = {"device": r["device"], "gateway": r["gateway"],
                       "interface": r["interface"], "rail": None}
    gws = [dict(r) for r in conn.execute(
        "SELECT name, address, status, loss, delay FROM gateways")]

    # which rail the default route leaves on: the exit interface's VLAN
    # membership (untagged first), else the rail holding the next-hop
    # address. An appliance whose WAN port never touches the switch fabric
    # resolves to neither — the cloud then attaches straight to the router,
    # which is also the truth.
    wan_rail = None
    if default:
        row = conn.execute(
            "SELECT vid FROM port_vlans WHERE device = ? AND interface = ? "
            "ORDER BY tagged, vid LIMIT 1",
            (default["device"], default["interface"] or "")).fetchone()
        if row is not None and f"v{row['vid']}" in rails.rails:
            wan_rail = f"v{row['vid']}"
        elif default["gateway"]:
            wan_rail = rails.rail_of_ip(default["gateway"])
        default["rail"] = wan_rail

    # VPN tunnels (#42): egress objects beside the internet cloud. A rail
    # reached *through* a tunnel (a route whose exit interface is the
    # tunnel's) hangs off the tunnel node, not the provider cloud — and it
    # draws even when nothing local has a leg on it, because reachability
    # through the tunnel is its participation. Routes are matched to a
    # tunnel by exact interface first, else by interface-prefix type when
    # exactly one tunnel of that type exists (openvpn/ipsec rows don't
    # always know their interface name).
    tun_rows = conn.execute(
        "SELECT * FROM tunnels ORDER BY device, type, name").fetchall()
    tunnels: list[dict] = []
    if tun_rows:
        type_label = {"wireguard": "WireGuard", "openvpn": "OpenVPN",
                      "ipsec": "IPsec", "vpn": "VPN"}
        tunnels = [{"name": t["name"], "type": t["type"],
                    "label": type_label.get(t["type"], "VPN"),
                    "status": t["status"], "device": t["device"],
                    "peer": t["peer"], "detail": t["detail"],
                    "rails": set()} for t in tun_rows]
        by_iface = {t["interface"]: i for i, t in enumerate(tun_rows)
                    if t["interface"]}
        by_type: dict[str, list[int]] = {}
        for i, t in enumerate(tun_rows):
            by_type.setdefault(t["type"], []).append(i)
        pfx_type = (("tun_wg", "wireguard"), ("wg", "wireguard"),
                    ("ovpn", "openvpn"), ("ipsec", "ipsec"), ("enc", "ipsec"))
        for r in conn.execute(
                "SELECT destination, interface, flags FROM routes "
                "WHERE destination NOT IN ('0.0.0.0/0', '::/0')"):
            if any(f in (r["flags"] or "") for f in ("B", "R")):
                continue
            iface = r["interface"] or ""
            idx = by_iface.get(iface)
            if idx is None and iface:
                for pfx, typ in pfx_type:
                    if iface.startswith(pfx) and len(by_type.get(typ, [])) == 1:
                        idx = by_type[typ][0]
                        break
            if idx is None:
                continue
            dest = _net(r["destination"])
            if dest is None:
                continue
            # an existing rail containing the destination wins (the remote
            # subnet may be documented in IPAM); else the route itself is
            # the rail's reason to exist
            key = rails.rail_of_ip(str(dest.network_address))
            if key is None:
                rails.add_subnet(r["destination"], None, None, "route")
                key = f"net:{r['destination']}"
            tunnels[idx]["rails"].add(key)
        for t in tunnels:
            t["rails"] = sorted(t["rails"])

    # hosts: multi-homed devices draw once; single-homed collapse to a count.
    # Routers live in the routing tier. Hypervisors and APs get spanning
    # boxes in tiers of their own, and their tenants — guest VMs, wireless
    # clients — live inside those boxes, so every host counts in exactly
    # one place on the page.
    hyp_names = {n for n, d in devices.items() if d["role"] == "hypervisor"}
    ap_names = {n for n, d in devices.items() if d["role"] == "ap"}
    hosts, single = [], {k: [] for k in rails.rails}
    hyp_groups: dict[str, dict[str, list[str]]] = {n: {} for n in hyp_names}
    hyp_guests: dict[str, list[dict]] = {n: [] for n in hyp_names}
    for dev, dl in sorted(legs.items()):
        if dev in router_names or dev in hyp_names or dev in ap_names:
            continue
        parent = devices.get(dev, {}).get("parent")
        entry = None
        if len(dl) >= 2:
            ordered = list(dl.values())
            entry = {"name": dev, "role": devices.get(dev, {}).get("role"),
                     "legs": ordered, "home": _home_rail(ordered, rails.rails)}
        if parent in hyp_names:
            if entry:
                hyp_guests[parent].append(entry)
            elif len(dl) == 1:
                k = next(iter(dl))
                hyp_groups[parent].setdefault(k, []).append(dev)
            continue
        if entry:
            hosts.append(entry)
        elif len(dl) == 1:
            single[next(iter(dl))].append(dev)

    # endpoints with an address participate when they aren't already a
    # device. A wireless client counts inside the AP that learned it; a
    # hostname with addresses on two networks is a dual-homed host worth a
    # box of its own (fused by canonical short hostname, the same rule the
    # topology's wired hosts use). A rail's gateway address is the router
    # wearing a per-VLAN hat, never a host.
    seen_devs = {d.lower() for d in devices}
    alias_map = dict(conn.execute("SELECT alias, canonical FROM aliases"))
    gw_ips = {r["gateway"].split("/")[0] for r in rails.rails.values()
              if r["gateway"]}
    ap_clients: dict[str, dict[str, list[str]]] = {n: {} for n in ap_names}
    fused: dict[str, dict[str, dict]] = {}
    for r in conn.execute(
            "SELECT hostname, ip, mac, device FROM endpoints WHERE ip IS NOT NULL"):
        hn = (r["hostname"] or "").split(".")[0].lower()
        hn = alias_map.get(hn, hn)
        if hn in seen_devs or r["ip"].split("/")[0] in gw_ips:
            continue
        key = rails.rail_of_ip(r["ip"])
        if key is None:
            continue
        label = r["hostname"] or r["ip"]
        if r["device"] in ap_names:
            ap_clients[r["device"]].setdefault(key, []).append(label)
        elif hn and not _local_admin(r["mac"] or ""):
            fused.setdefault(hn, {}).setdefault(key, {
                "rail": key, "iface": "?", "ip": r["ip"], "speed": 0})
        else:
            single[key].append(label)
    for hn, by_rail in sorted(fused.items()):
        if len(by_rail) >= 2:
            ordered = list(by_rail.values())
            hosts.append({"name": hn, "role": None, "legs": ordered,
                          "home": _home_rail(ordered, rails.rails)})
        else:
            single[next(iter(by_rail))].append(hn)

    # spanning boxes: a hypervisor's box covers every rail it or its guests
    # touch; an AP's covers its own addresses plus its clients'. A box with
    # no rail contact at all (nothing learned yet) stays off the map.
    hypervisors = []
    for hy in sorted(hyp_names):
        own = list(legs.get(hy, {}).values())
        span = ({l["rail"] for l in own} | set(hyp_groups[hy])
                | {l["rail"] for h in hyp_guests[hy] for l in h["legs"]})
        if span:
            hypervisors.append({
                "name": hy, "role": "hypervisor", "legs": own,
                "rails": sorted(span),
                "groups": {k: sorted(v) for k, v in hyp_groups[hy].items()},
                "guests": hyp_guests[hy],
                "status": devices.get(hy, {}).get("status")})
    aps = []
    for ap in sorted(ap_names):
        own = list(legs.get(ap, {}).values())
        span = {l["rail"] for l in own} | set(ap_clients[ap])
        if span:
            aps.append({"name": ap, "role": "ap", "legs": own,
                        "rails": sorted(span),
                        "groups": {k: sorted(v) for k, v in ap_clients[ap].items()},
                        "status": devices.get(ap, {}).get("status")})

    # a rail earns its place by participating: a router routes it, a drawn
    # host has a leg on it, or single-homed hosts count against it. IPAM
    # alone can't put a network on the map — supernets, aggregates, and
    # VLANs nothing claims are /vlans material, where documentation vs
    # reality is the point.
    attached = {l["rail"] for h in hosts for l in h["legs"]}
    for box in hypervisors + aps:
        attached |= set(box["rails"])
    live = {k: r for k, r in rails.rails.items()
            if r["routed"] or single.get(k) or k in attached}
    if wan_rail and wan_rail not in live:   # the way out always draws
        live[wan_rail] = rails.rails[wan_rail]
    for t in tunnels:                       # so does the far side of a tunnel
        for k in t["rails"]:
            live.setdefault(k, rails.rails[k])

    # spanning boxes pull their rails together harder than a two-legged
    # host does — a hypervisor covering scattered rails stretches across
    # the whole picture, so its adjacency is worth more
    pullers = hosts + [
        {"legs": [{"rail": k} for k in b["rails"]], "weight": 3}
        for b in hypervisors + aps]
    order = order_rails(live, pullers, declared=getattr(
        settings, "routed_order", ()) or ())
    pos = {k: i for i, k in enumerate(order)}
    for h in hosts:
        h["legs"].sort(key=lambda l: pos.get(l["rail"], 0))
    hosts.sort(key=lambda h: pos.get(h["home"], 0))
    rows = assign_rows(hosts, pos)
    for h, row in zip(hosts, rows):
        h["row"] = row
    for boxes in (hypervisors, aps):
        for b in boxes:
            b["rails"].sort(key=lambda k: pos.get(k, 0))
            b["legs"].sort(key=lambda l: pos.get(l["rail"], 0))
            for h in b.get("guests", ()):
                h["legs"].sort(key=lambda l: pos.get(l["rail"], 0))
        for b in boxes:
            b.setdefault("guests", [])
            b["guests"].sort(key=lambda h: pos.get(h["home"], 0))
        spans = assign_rows(
            [{"legs": [{"rail": k} for k in b["rails"]]} for b in boxes], pos)
        for b, row in zip(boxes, spans):
            b["row"] = row

    via_tunnel: dict[str, list[str]] = {}
    for t in tunnels:
        for k in t["rails"]:
            via_tunnel.setdefault(k, []).append(t["name"])
    out_rails = []
    for k in order:
        r = live[k]
        names = single.get(k, [])
        out_rails.append({**r, "sources": sorted(r["sources"]),
                          "hosts": len(names), "host_names": sorted(names),
                          "wan": k == wan_rail,
                          "via_tunnel": via_tunnel.get(k, [])})
    return {
        "rails": out_rails,
        "routers": routers,
        "default": default,
        "gateways": gws,
        "hosts": hosts,
        "hypervisors": hypervisors,
        "aps": aps,
        "tunnels": tunnels,
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
                total += h.get("weight", 1) * (max(ps) - min(ps))
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
