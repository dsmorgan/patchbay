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
    assert g["default"] == {"device": "fw1", "gateway": "203.0.113.1"}
