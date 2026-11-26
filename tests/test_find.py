"""find(): one lookup for a MAC, an IP, a hostname, or an alias, against
the demo network (fictional names, RFC 5737 addresses, 02: MACs)."""

import sqlite3

import pytest

from patchbay import demo, find, routed
from patchbay.config import load_settings


@pytest.fixture()
def model(clean_env, tmp_path):
    path = tmp_path / "test.db"
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    demo.seed(c)
    # an alias the operator declared for the NAS (PATCHBAY_ALIASES)
    c.execute("INSERT INTO aliases (alias, canonical, seeded) "
              "VALUES ('storage-old', 'nas1', 1)")
    c.commit()
    yield c, load_settings()
    c.close()


NAS_MAC = "02:d3:00:00:02:c7"


@pytest.mark.parametrize("query", [
    NAS_MAC, NAS_MAC.upper(), NAS_MAC.replace(":", "-"),
    "02d3.0000.02c7", "02d300-0002c7", "02d3-0000-02c7", "02D3000002C7",
    "2:d3:0:0:2:c7", "2-D3-0-0-2-C7",
])
def test_every_mac_spelling_finds_the_same_port(model, query):
    conn, settings = model
    assert find.classify(query) == ("mac", NAS_MAC)
    r = find.find(conn, settings, query)
    assert r["kind"] == "mac" and r["found"]
    assert [(f["device"], f["interface"]) for f in r["mac_table"]] == [("edge1", "e1/0/10")]
    # its own declared cable, so the port is the NAS's, not transit
    assert r["mac_table"][0]["toward"] is None
    assert r["endpoints"][0]["hostname"] == "nas1"
    assert r["maps"] == {"topology": ["nas1"], "routed": ["nas1"]}


def test_classify():
    assert find.classify("192.0.2.21") == ("ip", "192.0.2.21")
    assert find.classify("2001:DB8::1") == ("ip", "2001:db8::1")
    assert find.classify("192.0.2.21/24") == ("ip", "192.0.2.21")
    assert find.classify("NAS1.example.net") == ("name", "nas1")
    assert find.classify("02:d3:00:00:02") == ("name", "02:d3:00:00:02")  # too short
    assert find.classify("") == ("name", "")
    # six short groups are a MAC; IPv6 needs eight groups or a "::"
    assert find.classify("2:d3:0:0:2:c7") == ("mac", "02:d3:00:00:02:c7")
    assert find.classify("2001:db8:0:0:2:c7:0:1") == ("ip", "2001:db8::2:c7:0:1")
    assert find.classify("2001:db8::2:c7") == ("ip", "2001:db8::2:c7")
    assert find.classify("2:d3:0:0:2") == ("name", "2:d3:0:0:2")   # five groups
    assert find.classify("2:d3:0:0:2-c7")[0] == "name"            # mixed separators


def test_ip_finds_the_device_and_its_ipam_record(model):
    conn, settings = model
    r = find.find(conn, settings, "192.0.2.21")
    assert r["kind"] == "ip" and r["found"]
    assert r["devices"][0]["name"] == "nas1"
    assert r["devices"][0]["matched"] == "management address"
    assert [i["ip"] for i in r["ipam"]] == ["192.0.2.21"]
    assert {(l["device"], l["peer"]) for l in r["links"]} == {("nas1", "edge1")}


def test_interface_address_names_its_device(model):
    conn, settings = model
    ip = conn.execute("SELECT i.ip FROM interfaces i JOIN devices d "
                      "ON d.id = i.device_id WHERE d.name = 'fw1' "
                      "AND i.ip IS NOT NULL AND i.ip != d.mgmt_ip").fetchone()[0]
    r = find.find(conn, settings, ip.split("/")[0])
    assert any(d["name"] == "fw1" and d["matched"] == "interface address"
               for d in r["devices"])


def test_hostname_fqdn_and_alias(model):
    conn, settings = model
    for q in ("nas1", "NAS1.example.net", "storage-old"):
        r = find.find(conn, settings, q)
        assert r["found"], q
        assert [d["name"] for d in r["devices"]] == ["nas1"], q
        assert r["maps"]["topology"] == ["nas1"], q
    assert find.find(conn, settings, "storage-old")["devices"][0]["matched"] == "alias"


def test_host_behind_an_unmanaged_switch(model):
    conn, settings = model
    r = find.find(conn, settings, "printer")
    assert r["endpoints"][0]["device"].startswith("unmanaged@")
    assert r["mac_table"][0]["toward"].startswith("unmanaged@")
    assert r["maps"]["topology"] == ["printer"]
    assert any("behind that device" in n for n in r["notes"])


def test_guest_says_why_the_topology_leaves_it_out(model):
    conn, settings = model
    r = find.find(conn, settings, "vm-web1")
    assert r["devices"][0]["parent"] == "hyp1"
    assert r["maps"] == {"topology": [], "routed": ["vm-web1"]}
    assert any("guest of hyp1" in n for n in r["notes"])


def test_ipam_only_host_is_never_placed(model):
    conn, settings = model
    conn.execute("INSERT INTO ipam_addresses (ip, hostname, state) "
                 "VALUES ('198.51.100.200', 'paper-only', 'used')")
    r = find.find(conn, settings, "paper-only")
    assert r["found"] and r["ipam"][0]["ip"] == "198.51.100.200"
    assert r["maps"] == {"topology": [], "routed": []}
    assert any("Only IPAM" in n for n in r["notes"])


@pytest.mark.parametrize("query", ["no-such-host", "198.51.100.254",
                                   "02:d3:ff:ff:ff:ff", ""])
def test_not_found(model, query):
    conn, settings = model
    r = find.find(conn, settings, query)
    assert not r["found"]
    assert not (r["devices"] or r["endpoints"] or r["mac_table"] or r["ipam"])


def test_lists_stop_at_the_limit(model, monkeypatch):
    conn, settings = model
    monkeypatch.setattr(find, "FIND_LIMIT", 1)
    r = find.find(conn, settings, "core1")
    assert r["truncated"] and len(r["links"]) == 1


def test_routed_drawn_names_covers_every_box():
    graph = {"routers": [{"name": "fw1"}], "hosts": [{"name": "nas1"}],
             "rails": [{"host_names": ["printer", "192.0.2.99"]}],
             "hypervisors": [{"name": "hyp1", "guests": [{"name": "vm-db1"}],
                              "groups": {"v20": ["vm-web1"]}}],
             "aps": [{"name": "ap-den", "groups": {"v40": ["phone"]}}]}
    assert routed.drawn_names(graph) == {
        "fw1", "nas1", "printer", "192.0.2.99", "hyp1", "vm-db1", "vm-web1",
        "ap-den", "phone"}
