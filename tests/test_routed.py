"""The routed view's builder and layout engine (ADR-0002, #17)."""
import sqlite3

import pytest

from patchbay import db as pdb
from patchbay.routed import build_routed_graph, order_rails, assign_rows


class _S:  # minimal settings stand-in
    routed_order = ()
    wan_names = ("internet",)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pdb.init(c)
    yield c
    c.close()


def dev(c, name, role=None, **kw):
    return pdb.upsert_device(c, name=name, source="test", role=role,
                             status="up", last_seen=pdb.now(), **kw)


def iface(c, did, name, **kw):
    pdb.upsert_interface(c, device_id=did, name=name, **kw)


def subnet(c, cidr, vlan=None, descr=None):
    pdb.upsert_subnet(c, cidr=cidr, source="test", vlan=vlan, description=descr)


def vlan(c, vid, name):
    c.execute("INSERT OR IGNORE INTO vlans (vid, name, source) VALUES (?, ?, 'test')",
              (vid, name))


def seed_site(c):
    """Two routed nets, one unrouted VLAN, a firewall, one multi-homed host.
    The unrouted iscsi VLAN has a storage leg on nas1 — an unattached rail
    would be filtered out entirely."""
    vlan(c, 1, "mgmt"); vlan(c, 20, "servers"); vlan(c, 103, "iscsi")
    subnet(c, "192.0.2.0/24", vlan=1)
    subnet(c, "198.51.100.0/24", vlan=20)
    subnet(c, "203.0.113.0/24", vlan=103)
    fw = dev(c, "fw1", role="firewall")
    iface(c, fw, "vmx0", ip="192.0.2.1", speed_bps=10_000_000_000)
    iface(c, fw, "vmx1", ip="198.51.100.1", speed_bps=10_000_000_000)
    nas = dev(c, "nas1")
    iface(c, nas, "eth0", ip="192.0.2.40", speed_bps=1_000_000_000)
    iface(c, nas, "eth1", ip="198.51.100.40", speed_bps=10_000_000_000)
    iface(c, nas, "eth2", ip="203.0.113.40", speed_bps=1_000_000_000)
    web = dev(c, "web1")
    iface(c, web, "eth0", ip="198.51.100.50", speed_bps=1_000_000_000)
    return c


def test_rails_union_vlans_and_subnets_and_routed_flag(conn):
    seed_site(conn)
    g = build_routed_graph(conn, _S())
    by = {r["key"]: r for r in g["rails"]}
    assert set(by) == {"v1", "v20", "v103"}
    assert by["v1"]["routed"] and by["v20"]["routed"]
    assert not by["v103"]["routed"]              # no fan line = isolated badge
    assert by["v1"]["gateway"] == "192.0.2.1"    # hover detail
    assert g["routers"][0]["name"] == "fw1"
    assert sorted(g["routers"][0]["rails"]) == ["v1", "v20"]


def test_dual_stack_vlan_is_one_rail(conn):
    vlan(conn, 20, "servers")
    subnet(conn, "198.51.100.0/24", vlan=20)
    subnet(conn, "2001:db8:20::/64", vlan=20)
    d = dev(conn, "web1")
    iface(conn, d, "eth0", ip="198.51.100.50")
    g = build_routed_graph(conn, _S())
    assert [r["key"] for r in g["rails"]] == ["v20"]
    assert len(g["rails"][0]["subnets"]) == 2


def test_multi_homed_host_drawn_once_single_homed_counted(conn):
    seed_site(conn)
    g = build_routed_graph(conn, _S())
    names = [h["name"] for h in g["hosts"]]
    assert names == ["nas1"]                     # fw1 is routing tier, web1 counted
    by = {r["key"]: r for r in g["rails"]}
    assert by["v20"]["hosts"] == 1               # web1
    assert by["v1"]["hosts"] == 0


def test_home_rail_is_fastest_then_highest_vlan(conn):
    seed_site(conn)
    g = build_routed_graph(conn, _S())
    nas = g["hosts"][0]
    assert nas["home"] == "v20"                  # its 10G leg, not the 1G mgmt leg
    # equal speeds → highest vid wins
    legs = [{"rail": "v1", "iface": "a", "ip": None, "speed": 1000},
            {"rail": "v20", "iface": "b", "ip": None, "speed": 1000}]
    from patchbay.routed import _home_rail
    rails = {"v1": {"vid": 1}, "v20": {"vid": 20}}
    assert _home_rail(legs, rails) == "v20"


def test_endpoints_count_toward_single_homed(conn):
    seed_site(conn)
    pdb.upsert_endpoint(conn, mac="02:00:00:00:09:01", source="test",
                        ip="192.0.2.77", hostname="printer")
    g = build_routed_graph(conn, _S())
    by = {r["key"]: r for r in g["rails"]}
    assert by["v1"]["hosts"] == 1


def test_order_pulls_co_attached_rails_together():
    rails = {f"v{v}": {"key": f"v{v}", "vid": v, "name": ""} for v in (1, 20, 30, 40)}
    hosts = [{"legs": [{"rail": "v1"}, {"rail": "v40"}]}]
    order = order_rails(rails, hosts)
    pos = {k: i for i, k in enumerate(order)}
    assert abs(pos["v1"] - pos["v40"]) == 1      # pulled adjacent
    # deterministic
    assert order == order_rails(rails, hosts)


def test_declared_order_pins_left():
    rails = {f"v{v}": {"key": f"v{v}", "vid": v, "name": n}
             for v, n in ((1, "mgmt"), (20, "servers"), (30, "iot"))}
    order = order_rails(rails, [], declared=("servers", "30"))
    assert order[:2] == ["v20", "v30"]


def test_rows_never_overlap():
    pos = {"a": 0, "b": 1, "c": 2, "d": 3}
    hosts = [{"legs": [{"rail": "a"}, {"rail": "c"}]},   # span 0-2
             {"legs": [{"rail": "b"}, {"rail": "d"}]},   # span 1-3 overlaps
             {"legs": [{"rail": "d"}, {"rail": "d"}]}]   # span 3-3 fits row 0? no (3 !< ...)
    rows = assign_rows(hosts, pos)
    assert rows[0] != rows[1]
    assert len(rows) == 3


def test_unattached_rails_are_filtered(conn):
    # documentation-only networks stay on /vlans: an IPAM VLAN nothing
    # claims, a supernet, and an aggregate all vanish from the rails; the
    # unrouted iscsi VLAN survives on the strength of its nas1 leg
    seed_site(conn)
    vlan(conn, 4001, "dmz-on-paper")
    subnet(conn, "10.0.0.0/8", descr="supernet")
    subnet(conn, "2001:db8::/32", descr="aggregate")
    g = build_routed_graph(conn, _S())
    assert {r["key"] for r in g["rails"]} == {"v1", "v20", "v103"}


def test_default_route_skips_discard_flags(conn):
    seed_site(conn)
    conn.execute("INSERT INTO routes (device, destination, gateway, proto, flags, source) "
                 "VALUES ('fw1', '0.0.0.0/0', '203.0.113.1', 'ipv4', 'UGS', 'test')")
    conn.execute("INSERT INTO routes (device, destination, gateway, proto, flags, source) "
                 "VALUES ('fw1', '::/0', 'fe80::1', 'ipv6', 'UGB', 'test')")
    g = build_routed_graph(conn, _S())
    assert g["default"]["device"] == "fw1"
    assert g["default"]["gateway"] == "203.0.113.1"


def test_wan_rail_from_default_route_exit_vlan(conn):
    # the default route's exit interface is an untagged member of VLAN 299:
    # that rail is the internet uplink, and it draws even with nothing on it
    seed_site(conn)
    vlan(conn, 299, "xternal")
    conn.execute("INSERT INTO routes (device, destination, gateway, interface, "
                 "proto, flags, source) VALUES "
                 "('fw1', '0.0.0.0/0', '203.0.113.1', 'wan0', 'ipv4', 'UGS', 'test')")
    conn.execute("INSERT INTO port_vlans (device, interface, vid, tagged, source) "
                 "VALUES ('fw1', 'wan0', 299, 0, 'test')")
    g = build_routed_graph(conn, _S())
    assert g["default"]["rail"] == "v299"
    by = {r["key"]: r for r in g["rails"]}
    assert by["v299"]["wan"] is True
    assert not by["v1"]["wan"]


def test_wan_rail_falls_back_to_gateway_address(conn):
    # no VLAN membership on the exit interface, but the next hop lives in a
    # known rail's subnet — good enough to name the way out
    seed_site(conn)
    conn.execute("INSERT INTO routes (device, destination, gateway, interface, "
                 "proto, flags, source) VALUES "
                 "('fw1', '0.0.0.0/0', '198.51.100.254', 'wan0', 'ipv4', 'UGS', 'test')")
    g = build_routed_graph(conn, _S())
    assert g["default"]["rail"] == "v20"


def test_tunnel_routed_subnet_hangs_off_the_tunnel(conn):
    """A route whose exit interface is a tunnel's puts its destination on
    the map as that tunnel's rail — participation via reachability, even
    with nothing local attached (#42)."""
    seed_site(conn)
    conn.execute("INSERT INTO tunnels (device, type, name, peer, interface, "
                 "status, source, last_seen) VALUES "
                 "('fw1', 'wireguard', 'site-b', '192.0.2.200:51820', 'wg1', "
                 "'up', 'opnsense', ?)", (pdb.now(),))
    conn.execute("INSERT INTO routes (device, destination, interface, proto, "
                 "flags, source) VALUES "
                 "('fw1', '172.16.44.0/24', 'wg1', 'ipv4', 'UGS', 'test')")
    g = build_routed_graph(conn, _S())
    by = {r["key"]: r for r in g["rails"]}
    assert by["net:172.16.44.0/24"]["via_tunnel"] == ["site-b"]
    (t,) = g["tunnels"]
    assert t["label"] == "WireGuard" and t["rails"] == ["net:172.16.44.0/24"]


def test_tunnel_route_matches_unique_type_by_prefix(conn):
    """openvpn/ipsec rows don't always know their interface name: a route
    out ovpnc1 attaches to the only openvpn tunnel; with two candidates
    nothing is guessed."""
    seed_site(conn)
    conn.execute("INSERT INTO tunnels (device, type, name, status, source, "
                 "last_seen) VALUES ('fw1', 'openvpn', 'roadwarrior', 'up', "
                 "'opnsense', ?)", (pdb.now(),))
    conn.execute("INSERT INTO tunnels (device, type, name, status, source, "
                 "last_seen) VALUES ('fw1', 'ipsec', 'sa-1', 'up', "
                 "'opnsense', ?)", (pdb.now(),))
    conn.execute("INSERT INTO tunnels (device, type, name, status, source, "
                 "last_seen) VALUES ('fw1', 'ipsec', 'sa-2', 'up', "
                 "'opnsense', ?)", (pdb.now(),))
    conn.execute("INSERT INTO routes (device, destination, interface, proto, "
                 "flags, source) VALUES "
                 "('fw1', '172.16.45.0/24', 'ovpnc1', 'ipv4', 'UGS', 'test')")
    conn.execute("INSERT INTO routes (device, destination, interface, proto, "
                 "flags, source) VALUES "
                 "('fw1', '172.16.46.0/24', 'ipsec1', 'ipv4', 'UGS', 'test')")
    g = build_routed_graph(conn, _S())
    by_name = {t["name"]: t for t in g["tunnels"]}
    assert by_name["roadwarrior"]["rails"] == ["net:172.16.45.0/24"]
    assert by_name["sa-1"]["rails"] == [] and by_name["sa-2"]["rails"] == []
    assert not any(r["key"] == "net:172.16.46.0/24" for r in g["rails"])


def test_tunnel_route_into_documented_rail_reuses_it(conn):
    """A remote subnet already documented (IPAM) keeps its rail — the
    tunnel attaches to it instead of inventing a net: twin."""
    seed_site(conn)
    conn.execute("INSERT INTO tunnels (device, type, name, interface, status, "
                 "source, last_seen) VALUES ('fw1', 'wireguard', 'site-b', "
                 "'wg1', 'up', 'opnsense', ?)", (pdb.now(),))
    conn.execute("INSERT INTO routes (device, destination, interface, proto, "
                 "flags, source) VALUES "
                 "('fw1', '203.0.113.0/24', 'wg1', 'ipv4', 'UGS', 'test')")
    g = build_routed_graph(conn, _S())
    (t,) = g["tunnels"]
    assert t["rails"] == ["v103"]           # the documented rail, not net:
    by = {r["key"]: r for r in g["rails"]}
    assert by["v103"]["via_tunnel"] == ["site-b"]


# --- spanning tiers: hypervisors, APs, endpoint fusion (routed redesign) ----

def test_vms_live_inside_their_hypervisor(conn):
    seed_site(conn)
    hy = dev(conn, "hyp1", role="hypervisor")
    iface(conn, hy, "vmk0", ip="192.0.2.60", speed_bps=10_000_000_000)
    vm = dev(conn, "vm1", role="vm", parent="hyp1")
    iface(conn, vm, "eth0", ip="198.51.100.60")
    g = build_routed_graph(conn, _S())
    assert [h["name"] for h in g["hosts"]] == ["nas1"]     # vm1 not outside
    hyp = g["hypervisors"][0]
    assert hyp["name"] == "hyp1"
    assert hyp["groups"] == {"v20": ["vm1"]}
    assert set(hyp["rails"]) == {"v1", "v20"}
    by = {r["key"]: r for r in g["rails"]}
    assert by["v20"]["hosts"] == 1                          # web1 only


def test_wireless_clients_count_inside_their_ap(conn):
    seed_site(conn)
    ap = dev(conn, "ap1", role="ap")
    iface(conn, ap, "eth0", ip="192.0.2.70")
    pdb.upsert_endpoint(conn, mac="02:00:00:00:09:11", source="unifi",
                        ip="198.51.100.90", hostname="phone",
                        device="ap1", interface="ssid-home")
    g = build_routed_graph(conn, _S())
    a = g["aps"][0]
    assert a["name"] == "ap1"
    assert a["groups"] == {"v20": ["phone"]}
    assert [l["rail"] for l in a["legs"]] == ["v1"]         # dot: AP's own IP
    by = {r["key"]: r for r in g["rails"]}
    assert by["v20"]["hosts"] == 1                          # web1; not phone


def test_dual_homed_endpoint_fuses_to_a_host_box(conn):
    """Two ARP rows sharing a hostname on two networks are one dual-homed
    host, fused by canonical short hostname exactly like the topology's
    wired hosts."""
    seed_site(conn)
    pdb.upsert_endpoint(conn, mac="00:00:5e:00:53:21", source="test",
                        ip="192.0.2.80", hostname="nas9.lan")
    pdb.upsert_endpoint(conn, mac="00:00:5e:00:53:22", source="test",
                        ip="198.51.100.80", hostname="NAS9")
    g = build_routed_graph(conn, _S())
    fused = next(h for h in g["hosts"] if h["name"] == "nas9")
    assert {l["rail"] for l in fused["legs"]} == {"v1", "v20"}


def test_gateway_addresses_never_become_hosts(conn):
    """dnsmasq hands the router's per-VLAN addresses one hostname; fusing
    those would invent a phantom multi-homed host spanning every network."""
    seed_site(conn)
    pdb.upsert_endpoint(conn, mac="02:00:00:00:09:31", source="test",
                        ip="192.0.2.1", hostname="gateway")
    pdb.upsert_endpoint(conn, mac="02:00:00:00:09:32", source="test",
                        ip="198.51.100.1", hostname="gateway")
    g = build_routed_graph(conn, _S())
    assert all(h["name"] != "gateway" for h in g["hosts"])
    by = {r["key"]: r for r in g["rails"]}
    assert by["v1"]["hosts"] == 0


def test_randomized_macs_never_fuse(conn):
    """Four iPads all announce the hostname "iPad" from randomized
    (locally-administered) MACs — fusing those invents one impossible
    dual-homed tablet. Privacy MACs count as singles instead."""
    seed_site(conn)
    pdb.upsert_endpoint(conn, mac="be:e2:33:00:00:01", source="test",
                        ip="192.0.2.91", hostname="ipad")
    pdb.upsert_endpoint(conn, mac="c2:ea:3f:00:00:02", source="test",
                        ip="198.51.100.91", hostname="ipad")
    g = build_routed_graph(conn, _S())
    assert all(h["name"] != "ipad" for h in g["hosts"])
    by = {r["key"]: r for r in g["rails"]}
    assert by["v1"]["hosts"] == 1 and by["v20"]["hosts"] == 2  # web1 + ipad
    assert "ipad" in by["v1"]["host_names"]


def test_tunnel_source_supernet_never_becomes_a_network(conn):
    """A tunnel route whose destination CONTAINS local networks (WireGuard
    allowed-ips for the home supernet) is the tunnel's source side, not
    somewhere it leads — named on hover, never drawn as a lane."""
    seed_site(conn)
    conn.execute("INSERT INTO tunnels (device, type, name, interface, status, "
                 "source, last_seen) VALUES ('fw1', 'wireguard', 'egress', "
                 "'wg0', 'up', 'test', strftime('%s','now'))")
    conn.execute("INSERT INTO routes (device, destination, interface, source, "
                 "last_seen) VALUES ('fw1', '192.0.0.0/16', 'wg0', 'test', "
                 "strftime('%s','now'))")
    g = build_routed_graph(conn, _S())
    t = g["tunnels"][0]
    assert t["rails"] == []
    assert t["local_nets"] == ["192.0.0.0/16"]
    assert all(r["key"] != "net:192.0.0.0/16" for r in g["rails"])


def test_ipam_enriches_a_live_hosts_legs(conn):
    """An IPAM address matching a drawn host's hostname adds a leg on a
    network no observer can report (isolated storage VLAN: no ARP there);
    IPAM alone still draws nothing."""
    seed_site(conn)
    vlan(conn, 200, "backup"); subnet(conn, "198.18.0.0/24", vlan=200)
    conn.execute("INSERT INTO ipam_addresses (ip, hostname) VALUES "
                 "('198.18.0.40', 'nas1.lan')")
    conn.execute("INSERT INTO ipam_addresses (ip, hostname) VALUES "
                 "('198.18.0.90', 'ghost.lan')")
    g = build_routed_graph(conn, _S())
    nas = next(h for h in g["hosts"] if h["name"] == "nas1")
    doc = next(l for l in nas["legs"] if l["rail"] == "v200")
    assert doc["iface"] == "ipam" and doc["ip"] == "198.18.0.40"
    assert all(h["name"] != "ghost" for h in g["hosts"])
    by = {r["key"]: r for r in g["rails"]}
    assert by["v200"]["hosts"] == 0                  # doc adds legs, not hosts


def test_ipam_leg_promotes_a_one_legged_sighting(conn):
    """One live observation plus an IPAM address on another network is a
    dual-homed host: documentation supplies identity, the sighting supplies
    liveness."""
    seed_site(conn)
    pdb.upsert_endpoint(conn, mac="00:00:5e:00:53:60", source="fdb",
                        ip="192.0.2.60", hostname="tape1")
    conn.execute("INSERT INTO ipam_addresses (ip, hostname) VALUES "
                 "('203.0.113.60', 'tape1.lan')")
    g = build_routed_graph(conn, _S())
    tape = next(h for h in g["hosts"] if h["name"] == "tape1")
    assert {l["rail"] for l in tape["legs"]} == {"v1", "v103"}
    assert next(l for l in tape["legs"] if l["rail"] == "v103")["iface"] == "ipam"
