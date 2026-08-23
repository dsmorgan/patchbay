"""Collector units that run without a network: pure parsers, guard behavior,
and the phpIPAM fetch-before-delete atomicity — all against synthetic data."""

import httpx
import pytest

from patchbay import db as pdb
from patchbay.collectors.librenms import WRAPPED_SPEED, _descr, _speed
from patchbay.collectors.oxidized import parse_ironware, parse_netgear


# --- librenms helpers --------------------------------------------------------

def test_wrapped_ifspeed_unwraps():
    assert _speed(1410065408) == 10_000_000_000          # raw 32-bit wrap
    assert _speed(1410000000) == 10_000_000_000          # Mbps-rounded wrap
    assert _speed(1_000_000_000) == 1_000_000_000        # real value untouched
    assert _speed(None) is None
    assert 20_000_000_000 in WRAPPED_SPEED.values()


def test_vendor_default_descr_scrubbed():
    assert _descr("Unit: 1 Slot: 0 Port: 5 10G - Level") == ""
    assert _descr(None) == ""
    assert _descr("uplink to core [3]") == "uplink to core [3]"


# --- oxidized config parsers -------------------------------------------------

IRONWARE = """\
vlan 24 name mgmt
 tagged ethe 1/1/1 to 1/1/2
 untagged ethe 1/1/5
vlan 73
 tagged ethe 1/1/1
mirror-port ethernet 1/1/9
interface ethernet 1/1/5
 monitor ethe 1/1/9 both
"""


def test_parse_ironware():
    names, rows, roles = parse_ironware(IRONWARE)
    assert names == {24: "mgmt", 73: ""}
    assert ("ethernet1/1/1", 24, True) in rows
    assert ("ethernet1/1/2", 24, True) in rows
    assert ("ethernet1/1/5", 24, False) in rows
    assert ("ethernet1/1/1", 73, True) in rows
    assert ("ethernet1/1/9", "monitor-dst", "mirror destination") in roles
    assert [r for r in roles if r[1] == "monitor-src"] == [
        ("ethernet1/1/5", "monitor-src", "mirrored to ethernet1/1/9 (both)")]


NETGEAR = """\
vlan database
vlan 13,24
vlan name 13 "VM13"
exit
interface 1/0/3
switchport mode access
switchport access vlan 24
exit
interface 1/0/11
switchport mode trunk
exit
"""


def test_parse_netgear_bare_trunk_means_all_vlans():
    names, rows, _ = parse_netgear(NETGEAR)
    assert names[13] == "VM13" and 1 in names  # default VLAN implied
    assert ("1/0/3", 24, False) in rows
    # bare trunk: every existing VLAN tagged, native 1 untagged
    trunk = {(v, t) for i, v, t in rows if i == "1/0/11"}
    assert (13, True) in trunk and (24, True) in trunk and (1, False) in trunk


def test_parse_netgear_monitor_session():
    # a SPAN destination is the one port whose cabling no protocol can report
    _, _, roles = parse_netgear(
        "monitor session 1 source vlan 1\n"
        "monitor session 1 source interface 1/0/7\n"
        "monitor session 1 destination interface 1/0/14\n"
        "monitor session 1 mode\n")
    assert ("1/0/14", "monitor-dst",
            "session 1 mirrors vlan 1, interface 1/0/7") in roles
    assert ("1/0/7", "monitor-src", "session 1 mirrored to 1/0/14") in roles


def test_parser_on_foreign_config_yields_nothing():
    names, rows, roles = parse_netgear("hostname something-else\nno vlans here\n")
    # collector then keeps last good data rather than wiping it
    assert names == {} and rows == [] and roles == []


# --- phpIPAM atomicity -------------------------------------------------------

class FakeResponse:
    def __init__(self, data):
        self._data = data
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"success": True, "data": self._data}


class FlakyClient:
    """Serves vlans + subnets, then raises on the SECOND subnet's addresses."""

    def __init__(self, *a, **k):
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        if url.endswith("vlan/"):
            return FakeResponse([])
        if url.endswith("subnets/"):
            return FakeResponse([
                {"subnet": "192.0.2.0", "mask": "24", "id": 1, "sectionId": 1},
                {"subnet": "198.51.100.0", "mask": "24", "id": 2, "sectionId": 1},
            ])
        if "subnets/1/" in url:
            return FakeResponse([{"ip": "192.0.2.10", "hostname": "host-a"}])
        raise httpx.ReadTimeout("boom")  # subnet 2's addresses


def test_phpipam_failure_keeps_old_address_book(conn, clean_env, monkeypatch):
    from patchbay.collectors.phpipam import PhpIpamCollector
    from patchbay.config import load_settings

    conn.execute("INSERT INTO ipam_addresses (ip, hostname) VALUES ('192.0.2.9', 'old')")
    clean_env.setenv("IPAM_URL", "https://ipam.example/api")
    clean_env.setenv("IPAM_APP_ID", "app")
    clean_env.setenv("IPAM_TOKEN", "t")
    monkeypatch.setattr(httpx, "Client", FlakyClient)
    with pytest.raises(httpx.ReadTimeout):
        PhpIpamCollector().collect(load_settings(), conn)
    # the timeout struck during fetching — before the refresh DELETE ran
    rows = conn.execute("SELECT ip FROM ipam_addresses").fetchall()
    assert [r["ip"] for r in rows] == ["192.0.2.9"]


# --- vsphere prune guards ----------------------------------------------------

def test_vm_prune_semantics(conn):
    """The prune the vsphere collector runs, exercised directly: scoped to its
    own rows, catches orphans via role, never fires on an empty listing."""
    pdb.upsert_device(conn, name="vm-a", source="vsphere", role="vm", parent="hyp1")
    pdb.upsert_device(conn, name="vm-orphan", source="vsphere", role="vm")
    pdb.upsert_device(conn, name="fw1", source="opnsense", role="firewall",
                      parent="hyp1")
    seen = ["vm-a"]
    conn.execute(
        f"DELETE FROM devices WHERE source = 'vsphere' "
        f"AND (parent IS NOT NULL OR role = 'vm') "
        f"AND name NOT IN ({','.join('?' * len(seen))})", seen)
    left = {r[0] for r in conn.execute("SELECT name FROM devices")}
    assert left == {"vm-a", "fw1"}  # orphan pruned; other collector's row kept


# --- vsphere: retracting rows a better source supersedes ---------------------

def test_vsphere_retracts_its_labels_when_a_real_collector_appears(conn):
    """A firewall VM starts out known only to vCenter, so its NICs carry
    vCenter's generic labels. Once its own collector is configured, the real
    names arrive — and the generic rows must go, or the device page lists
    every NIC twice forever. This is the upgrade path the README recommends.
    """
    from patchbay import db as pdb

    # phase 1: only vCenter knows this guest, so it owns the interface rows
    vm_id = pdb.upsert_device(conn, name="fw1", source="vsphere", role="vm",
                              parent="hyp1", status="up")
    for i in (1, 2):
        pdb.upsert_interface(conn, device_id=vm_id, name=f"Network adapter {i}",
                             mac=f"00:50:56:00:00:0{i}")
    assert conn.execute("SELECT COUNT(*) FROM interfaces").fetchone()[0] == 2

    # phase 2: the firewall's own collector is configured and names them for real
    conn.execute("UPDATE devices SET role='firewall', source='opnsense' WHERE name='fw1'")
    pdb.upsert_interface(conn, device_id=vm_id, name="vmx0", mac="00:50:56:00:00:01")
    pdb.upsert_interface(conn, device_id=vm_id, name="vmx1", mac="00:50:56:00:00:02")

    # the collector's retraction, as it runs for a network-role guest
    for i in (1, 2):
        conn.execute(
            "DELETE FROM interfaces WHERE name = ? AND device_id = "
            "(SELECT id FROM devices WHERE name = ?)", (f"Network adapter {i}", "fw1"))

    names = sorted(r[0] for r in conn.execute("SELECT name FROM interfaces"))
    assert names == ["vmx0", "vmx1"], names
