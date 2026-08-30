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


class ShrinkingClient:
    """Serves two subnets, then only the first — a subnet deleted in phpIPAM."""

    subnets = [
        {"subnet": "192.0.2.0", "mask": "24", "id": 1, "sectionId": 1},
        {"subnet": "198.51.100.0", "mask": "24", "id": 2, "sectionId": 1},
    ]

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        if url.endswith("vlan/"):
            return FakeResponse([])
        if url.endswith("subnets/"):
            return FakeResponse(self.subnets)
        return FakeResponse([])


def test_phpipam_retires_subnets_it_stops_reporting(conn, clean_env, monkeypatch):
    """Subnets were upsert-only while the address book got a full refresh, so
    a subnet deleted in phpIPAM stayed on the VLAN pages and in drift forever.
    Caught by comparing a long-running database against a fresh install: the
    older one held three prefixes its own last poll had not reported."""
    from patchbay.collectors.phpipam import PhpIpamCollector
    from patchbay.config import load_settings

    clean_env.setenv("IPAM_URL", "https://ipam.example/api")
    clean_env.setenv("IPAM_APP_ID", "app")
    clean_env.setenv("IPAM_TOKEN", "t")
    monkeypatch.setattr(httpx, "Client", ShrinkingClient)

    PhpIpamCollector().collect(load_settings(), conn)
    assert {r[0] for r in conn.execute("SELECT cidr FROM subnets")} == {
        "192.0.2.0/24", "198.51.100.0/24"}

    # the second subnet is deleted in phpIPAM
    ShrinkingClient.subnets = ShrinkingClient.subnets[:1]
    try:
        PhpIpamCollector().collect(load_settings(), conn)
        assert {r[0] for r in conn.execute("SELECT cidr FROM subnets")} == {
            "192.0.2.0/24"}

        # a source that returns nothing is a hiccup, not a decommission: the
        # guard every other prune uses applies here too
        ShrinkingClient.subnets = []
        PhpIpamCollector().collect(load_settings(), conn)
        assert {r[0] for r in conn.execute("SELECT cidr FROM subnets")} == {
            "192.0.2.0/24"}
    finally:
        ShrinkingClient.subnets = [
            {"subnet": "192.0.2.0", "mask": "24", "id": 1, "sectionId": 1},
            {"subnet": "198.51.100.0", "mask": "24", "id": 2, "sectionId": 1},
        ]


def test_phpipam_never_prunes_another_sources_subnets(conn, clean_env, monkeypatch):
    """The prune is scoped by source. phpIPAM is the only writer today, but a
    site-specific or third-party collector writing subnets must not have them
    swept away by an unrelated IPAM poll."""
    from patchbay.collectors.phpipam import PhpIpamCollector
    from patchbay.config import load_settings

    pdb.upsert_subnet(conn, cidr="203.0.113.0/24", source="netbox")
    clean_env.setenv("IPAM_URL", "https://ipam.example/api")
    clean_env.setenv("IPAM_APP_ID", "app")
    clean_env.setenv("IPAM_TOKEN", "t")
    monkeypatch.setattr(httpx, "Client", ShrinkingClient)
    PhpIpamCollector().collect(load_settings(), conn)
    assert "203.0.113.0/24" in {r[0] for r in conn.execute("SELECT cidr FROM subnets")}


def test_opnsense_host_accepts_scheme_and_bare_hostname():
    """OPNSENSE_HOST is either a bare hostname (https assumed) or a full URL
    with scheme (PR #8: plain-HTTP management interfaces); the short device
    name must come out the same either way, with scheme and port stripped."""
    from patchbay.collectors.opnsense import base_and_name

    assert base_and_name("fw1.example.net") == ("https://fw1.example.net/api", "fw1")
    assert base_and_name("http://fw1.example.net") == ("http://fw1.example.net/api", "fw1")
    assert base_and_name("https://fw1.example.net:8443/") == ("https://fw1.example.net:8443/api", "fw1")
    assert base_and_name("http://192.0.2.1") == ("http://192.0.2.1/api", "192")


# --- pfSense (PR #9) ---

class PfResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        assert self.status_code < 400

    def json(self):
        return {"code": self.status_code, "data": self._data}


class PfClient:
    """Sanitized pfrest v2 shapes: one physical WAN, one VLAN sub-interface,
    one OpenVPN tunnel (skipped), gateways incl. a VPN one (skipped) and an
    'unknown' one (passed through), and a DHCP static map. Class-level so a
    test can shrink the interface list between polls."""

    interfaces = [
        {"if": "igc0", "id": "wan", "descr": "WAN", "type": "dhcp",
         "ipaddr": "dhcp", "enable": True},
        {"if": "igc1.20", "id": "opt1", "descr": "servers",
         "ipaddr": "192.0.2.1", "enable": True},
        {"if": "ovpnc1", "id": "opt2", "descr": "vpn out", "type": "openvpn",
         "ipaddr": "198.51.100.77", "enable": True},
    ]
    fail_gateways = False

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        if url.endswith("status/system"):
            return PfResponse({"pfsense_version": "2.8.0"})
        if url.endswith("status/interfaces"):
            # pfrest v2 keys the physical name as "hwif", not "if" (PR #15)
            return PfResponse([
                {"hwif": "igc0", "name": "wan", "status": "up",
                 "macaddr": "02:d3:00:00:99:01",
                 "media": "1000baseT <full-duplex>", "ipaddr": "203.0.113.9",
                 "ipaddrv6": "2001:db8::9"},
                {"hwif": "igc1.20", "name": "opt1", "status": "up",
                 "macaddr": "02:d3:00:00:99:02"},
            ])
        if url.endswith("/interfaces"):
            return PfResponse(self.interfaces)
        if url.endswith("status/gateways"):
            if self.fail_gateways:
                return PfResponse(None, status=403)
            return PfResponse([
                {"name": "WAN_DHCP", "interface": "igc0", "status": "online",
                 "monitorip": "203.0.113.1", "loss": "0.0", "stddev": "1.2"},
                {"name": "HOME_VPNV4", "interface": "ovpnc1", "status": "online"},
                {"name": "WAN6_DHCP6", "interface": "igc0", "status": "unknown"},
            ])
        if url.endswith("services/dhcp_servers"):
            return PfResponse([{"interface": "opt1", "staticmap": [
                {"mac": "02:D3:00:00:99:10", "ipaddr": "192.0.2.40",
                 "hostname": "printer"}]}])
        return PfResponse(None, status=404)


def _pf_settings(clean_env):
    from patchbay.config import load_settings
    clean_env.setenv("PFSENSE_HOST", "https://fw2.example.net")
    clean_env.setenv("PFSENSE_API_KEY", "k")
    return load_settings()


def test_pfsense_poll_maps_the_model(conn, clean_env, monkeypatch):
    from patchbay.collectors.pfsense import PfsenseCollector

    monkeypatch.setattr(httpx, "Client", PfClient)
    summary = PfsenseCollector().collect(_pf_settings(clean_env), conn)
    assert "2 interfaces" in summary

    dev = conn.execute("SELECT * FROM devices WHERE name='fw2'").fetchone()
    assert dev["role"] == "firewall" and dev["os"] == "pfsense 2.8.0"

    names = {r[0] for r in conn.execute(
        "SELECT i.name FROM interfaces i JOIN devices d ON d.id=i.device_id "
        "WHERE d.name='fw2'")}
    assert names == {"igc0", "igc1.20"}          # the tunnel never lands
    wan = conn.execute(
        "SELECT * FROM interfaces i JOIN devices d ON d.id=i.device_id "
        "WHERE d.name='fw2' AND i.name='igc0'").fetchone()
    assert wan["speed_bps"] == 1_000_000_000     # from the media string
    assert wan["ip"] == "203.0.113.9"            # dhcp resolved from live status
    assert wan["mac"] == "02:d3:00:00:99:01"
    assert wan["ip6"] == "2001:db8::9"               # ipaddrv6 propagates (PR #15)
    assert dev["mgmt_ip"] == "fw2.example.net"       # the configured host, not '?'


    assert conn.execute("SELECT vid FROM port_vlans WHERE device='fw2' "
                        "AND interface='igc1.20'").fetchone()[0] == 20

    gws = {r["name"]: r["status"] for r in
           conn.execute("SELECT * FROM gateways WHERE source='pfsense'")}
    assert gws == {"WAN_DHCP": "up", "WAN6_DHCP6": "unknown"}  # VPN gw skipped

    ep = conn.execute("SELECT * FROM endpoints WHERE mac='02:d3:00:00:99:10'").fetchone()
    assert ep["ip"] == "192.0.2.40" and ep["hostname"] == "printer"


def test_pfsense_retracts_a_removed_vlan_subinterface(conn, clean_env, monkeypatch):
    from patchbay.collectors.pfsense import PfsenseCollector

    monkeypatch.setattr(httpx, "Client", PfClient)
    s = _pf_settings(clean_env)
    PfsenseCollector().collect(s, conn)
    assert conn.execute("SELECT COUNT(*) FROM port_vlans WHERE device='fw2'").fetchone()[0] == 1
    monkeypatch.setattr(PfClient, "interfaces", PfClient.interfaces[:1])  # subif deleted
    PfsenseCollector().collect(s, conn)
    assert conn.execute("SELECT COUNT(*) FROM port_vlans WHERE device='fw2'").fetchone()[0] == 0


def test_pfsense_403_degrades_and_is_named(conn, clean_env, monkeypatch):
    from patchbay.collectors.pfsense import PfsenseCollector

    monkeypatch.setattr(httpx, "Client", PfClient)
    monkeypatch.setattr(PfClient, "fail_gateways", True)
    summary = PfsenseCollector().collect(_pf_settings(clean_env), conn)
    assert "403" in summary                       # named, not fatal
    assert "2 interfaces" in summary              # the rest still polled


def test_pfsense_accepts_the_first_round_env_name(clean_env):
    from patchbay.collectors.pfsense import PfsenseCollector
    from patchbay.config import load_settings
    clean_env.setenv("PFSENSE_HOST", "https://fw2.example.net")
    clean_env.setenv("PFSENSE_API_SECRET", "legacy")
    s = load_settings()
    assert PfsenseCollector().configured(s) and s.pfsense_api_key == "legacy"


def test_librenms_interface_macs_are_colon_delimited():
    """Issue #14: LibreNMS emits ifPhysAddress as bare hex; everything else
    in the model is colon-delimited, so format at ingestion."""
    from patchbay.collectors.librenms import _colon_mac

    assert _colon_mac("288088734256") == "28:80:88:73:42:56"
    assert _colon_mac("28:80:88:73:42:54") == "28:80:88:73:42:54"  # passthrough
    assert _colon_mac("") is None and _colon_mac(None) is None
    assert _colon_mac("not-a-mac") == "not-a-mac"  # never invent structure


# --- opnsense: tunnel skip, admin_status, speed, VLAN tag --------------------

def test_parse_line_rate():
    from patchbay.collectors.opnsense import _parse_line_rate
    assert _parse_line_rate({"line rate": "1000000000 bit/s"}) == 1_000_000_000
    assert _parse_line_rate({"line rate": "100000000 bit/s"}) == 100_000_000
    assert _parse_line_rate({}) is None
    assert _parse_line_rate({"line rate": ""}) is None
    assert _parse_line_rate({"line rate": "not a number bit/s"}) is None


class _OnsClient:
    """OPNsense API stub: one physical iface, one VLAN sub, two tunnel ifaces."""
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def get(self, url, **kw):
        class R:
            status_code = 200
            def raise_for_status(self_): pass
            def json(self_):
                if "firmware" in url:
                    return {"product_version": "24.7"}
                if "overview" in url:
                    return [
                        {"device": "igc0", "status": "up", "enabled": True,
                         "macaddr": "02:00:00:00:01:01",
                         "statistics": {"line rate": "1000000000 bit/s"}},
                        {"device": "igc0.20", "status": "up", "enabled": True,
                         "vlan_tag": "20", "statistics": {}},
                        {"device": "tun0",   "status": "up", "enabled": True},
                        {"device": "ovpnc1", "status": "up", "enabled": True},
                    ]
                if "gateway" in url:
                    return {"items": []}
                if "get_arp" in url or "getArp" in url:
                    return []
                if "get_routes" in url:
                    return []
                if "searchLease" in url:
                    return {"rows": []}
                return None
        return R()


def _ons_settings(clean_env):
    from patchbay.config import load_settings
    clean_env.setenv("OPNSENSE_HOST", "https://fw1.example.net")
    clean_env.setenv("OPNSENSE_API_KEY", "k")
    clean_env.setenv("OPNSENSE_API_SECRET", "s")
    return load_settings()


def test_opnsense_tunnel_interfaces_skipped(conn, clean_env, monkeypatch):
    """tun/ovpn/gif/gre/ipsec/wg interfaces must never land as interface rows."""
    from patchbay.collectors.opnsense import OpnsenseCollector
    monkeypatch.setattr(httpx, "Client", _OnsClient)
    OpnsenseCollector().collect(_ons_settings(clean_env), conn)
    names = {r[0] for r in conn.execute(
        "SELECT i.name FROM interfaces i JOIN devices d ON d.id=i.device_id "
        "WHERE d.name='fw1'")}
    assert "tun0" not in names and "ovpnc1" not in names
    assert "igc0" in names and "igc0.20" in names


def test_opnsense_admin_status_and_speed(conn, clean_env, monkeypatch):
    """enabled=True → admin_status='up'; statistics line rate → speed_bps."""
    from patchbay.collectors.opnsense import OpnsenseCollector
    monkeypatch.setattr(httpx, "Client", _OnsClient)
    OpnsenseCollector().collect(_ons_settings(clean_env), conn)
    iface = conn.execute(
        "SELECT i.* FROM interfaces i JOIN devices d ON d.id=i.device_id "
        "WHERE d.name='fw1' AND i.name='igc0'").fetchone()
    assert iface["admin_status"] == "up"
    assert iface["speed_bps"] == 1_000_000_000


def test_opnsense_vlan_tag_written_to_port_vlans(conn, clean_env, monkeypatch):
    """A VLAN sub-interface writes its vlan_tag to port_vlans."""
    from patchbay.collectors.opnsense import OpnsenseCollector
    monkeypatch.setattr(httpx, "Client", _OnsClient)
    OpnsenseCollector().collect(_ons_settings(clean_env), conn)
    row = conn.execute(
        "SELECT vid FROM port_vlans WHERE device='fw1' AND interface='igc0.20'"
    ).fetchone()
    assert row is not None and row["vid"] == 20


class _OnsClient403(_OnsClient):
    """Same stub, but the interfaces export is denied (missing privilege)."""
    def get(self, url, **kw):
        r = super().get(url, **kw)
        if "overview" in url:
            r.status_code = 403
        return r


def test_opnsense_403_on_export_keeps_port_vlans(conn, clean_env, monkeypatch):
    """A denied interfaces export must not wipe port_vlans rows that are
    still true — only a successful export owns the delete-then-insert."""
    from patchbay.collectors.opnsense import OpnsenseCollector
    monkeypatch.setattr(httpx, "Client", _OnsClient)
    OpnsenseCollector().collect(_ons_settings(clean_env), conn)
    assert conn.execute("SELECT COUNT(*) FROM port_vlans WHERE device='fw1'").fetchone()[0] == 1
    monkeypatch.setattr(httpx, "Client", _OnsClient403)
    OpnsenseCollector().collect(_ons_settings(clean_env), conn)
    assert conn.execute("SELECT COUNT(*) FROM port_vlans WHERE device='fw1'").fetchone()[0] == 1


def test_opnsense_stale_tunnel_rows_purged(conn, clean_env, monkeypatch):
    """Tunnel interface rows written before the filter existed are removed."""
    from patchbay import db
    from patchbay.collectors.opnsense import OpnsenseCollector
    monkeypatch.setattr(httpx, "Client", _OnsClient)
    dev_id = db.upsert_device(conn, name="fw1", source="opnsense",
                              role="firewall", status="up")
    db.upsert_interface(conn, device_id=dev_id, name="wg1")
    db.upsert_interface(conn, device_id=dev_id, name="ipsec3")
    OpnsenseCollector().collect(_ons_settings(clean_env), conn)
    names = {r[0] for r in conn.execute(
        "SELECT i.name FROM interfaces i JOIN devices d ON d.id=i.device_id "
        "WHERE d.name='fw1'")}
    assert "wg1" not in names and "ipsec3" not in names
    assert "igc0" in names


# --- UniFi: USW collection, uplink links, wired-client FDB -------------------

_USW = {
    "type": "usw", "mac": "02:00:00:00:02:01",
    "name": "sw-access-01", "ip": "192.0.2.10",
    "model": "US-8-150W", "version": "6.5.59", "state": 1,
    "port_table": [
        {"port_idx": 1, "name": "Port 1", "up": True,  "enable": True, "speed": 1000},
        {"port_idx": 2, "name": "Port 2", "up": False, "enable": True, "speed": 0},
        {"port_idx": 9, "name": "SFP",    "up": True,  "enable": True, "speed": 1000},
    ],
    "uplink": {"uplink_device_name": "sw-core-01", "uplink_remote_port": 3, "port_idx": 9},
}

_UAP = {
    "type": "uap", "mac": "02:00:00:00:03:01",
    "name": "ap-floor1", "ip": "192.0.2.20",
    "model": "UAP-AC-PRO", "version": "6.3.4", "state": 1,
    "uplink": {
        "name": "eth0", "ifname": "eth0", "up": True, "speed": 1000,
        "uplink_device_name": "sw-access-01", "uplink_remote_port": 1,
    },
}

_WIRED_CLIENT = {
    "mac": "02:00:00:00:04:01", "ip": "192.0.2.50",
    "hostname": "workstation", "is_wired": True,
    "sw_mac": "02:00:00:00:02:01", "sw_port": 2,
}

_WIRELESS_CLIENT = {
    "mac": "02:00:00:00:04:02", "ap_mac": "02:00:00:00:03:01",
    "essid": "corp-wifi", "is_wired": False,
}


class _UnifiClient:
    """UniFi Network API stub: one USW, one AP, one wired and one wireless client."""
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def post(self, url, **kw):
        class R:
            status_code = 200
            def raise_for_status(self_): pass
            def json(self_): return {"data": [], "meta": {"rc": "ok"}}
        return R()

    def get(self, url, **kw):
        class R:
            status_code = 200
            def raise_for_status(self_): pass
            def json(self_):
                if "stat/device" in url:
                    return {"data": [_USW, _UAP]}
                if "stat/sta" in url:
                    return {"data": [_WIRED_CLIENT, _WIRELESS_CLIENT]}
                return {"data": []}
        return R()


def _unifi_settings(clean_env):
    from patchbay.config import load_settings
    clean_env.setenv("UNIFI_URL", "https://unifi.example.net:8443")
    clean_env.setenv("UNIFI_USER", "admin")
    clean_env.setenv("UNIFI_PASS", "secret")
    return load_settings()


def test_unifi_usw_collected_with_ports(conn, clean_env, monkeypatch):
    """USW devices land as role=switch with all port_table interfaces."""
    from patchbay.collectors.unifi import UnifiCollector
    monkeypatch.setattr(httpx, "Client", _UnifiClient)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    dev = conn.execute("SELECT * FROM devices WHERE name='sw-access-01'").fetchone()
    assert dev is not None and dev["role"] == "switch"
    port_names = {r[0] for r in conn.execute(
        "SELECT i.name FROM interfaces i JOIN devices d ON d.id=i.device_id "
        "WHERE d.name='sw-access-01'")}
    assert port_names == {"Port 1", "Port 2", "SFP"}


def test_unifi_switch_to_switch_uplink_link(conn, clean_env, monkeypatch):
    """USW uplink data writes a link from the local SFP port to the upstream switch."""
    from patchbay.collectors.unifi import UnifiCollector
    monkeypatch.setattr(httpx, "Client", _UnifiClient)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    row = conn.execute(
        "SELECT * FROM links WHERE "
        "(a_device='sw-access-01' AND a_interface='SFP') OR "
        "(b_device='sw-access-01' AND b_interface='SFP')").fetchone()
    assert row is not None


def test_unifi_ap_uplink_link(conn, clean_env, monkeypatch):
    """AP with uplink_device_name+uplink_remote_port writes a link to the switch."""
    from patchbay.collectors.unifi import UnifiCollector
    monkeypatch.setattr(httpx, "Client", _UnifiClient)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    row = conn.execute(
        "SELECT * FROM links WHERE "
        "(a_device='ap-floor1' OR b_device='ap-floor1')").fetchone()
    assert row is not None


def test_unifi_wired_client_fdb_row(conn, clean_env, monkeypatch):
    """Wired client with sw_mac+sw_port resolves to (switch, port_name) → fdb row."""
    from patchbay.collectors.unifi import UnifiCollector
    monkeypatch.setattr(httpx, "Client", _UnifiClient)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    row = conn.execute(
        "SELECT * FROM fdb WHERE mac='02:00:00:00:04:01'").fetchone()
    assert row is not None
    assert row["device"] == "sw-access-01"
    assert row["interface"] == "Port 2"
    assert row["source"] == "unifi"


def test_unifi_fdb_scoped_to_source(conn, clean_env, monkeypatch):
    """unifi poll replaces only its own fdb rows; librenms rows are untouched."""
    from patchbay.collectors.unifi import UnifiCollector
    conn.execute(
        "INSERT INTO fdb (device, interface, mac, source) "
        "VALUES ('sw-core-01', 'Port 5', '02:00:00:00:99:01', 'librenms')")
    monkeypatch.setattr(httpx, "Client", _UnifiClient)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM fdb WHERE source='librenms'").fetchone()[0] == 1


class _UnifiRenamedPorts(_UnifiClient):
    """Controller where the upstream switch's ports carry custom names and
    the upstream switch appears LATER in the device list than its downstream."""
    def get(self, url, **kw):
        core = {
            "type": "usw", "mac": "02:00:00:00:02:99",
            "name": "sw-core-01", "ip": "192.0.2.11",
            "model": "US-24", "version": "6.5.59", "state": 1,
            "port_table": [
                {"port_idx": 1, "name": "to-ap-floor1", "up": True, "enable": True, "speed": 1000},
                {"port_idx": 3, "name": "to-access-01", "up": True, "enable": True, "speed": 1000},
            ],
        }
        class R:
            status_code = 200
            def raise_for_status(self_): pass
            def json(self_):
                if "stat/device" in url:
                    return {"data": [_USW, core]}   # downstream first
                if "stat/sta" in url:
                    return {"data": []}
                return {"data": []}
        return R()


def test_unifi_uplink_resolves_renamed_remote_port(conn, clean_env, monkeypatch):
    """The remote end of an uplink must use the upstream switch's actual port
    name, not an assumed 'Port N' — even when the upstream switch appears
    later in the device list."""
    from patchbay.collectors.unifi import UnifiCollector
    monkeypatch.setattr(httpx, "Client", _UnifiRenamedPorts)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    row = conn.execute(
        "SELECT * FROM links WHERE a_device='sw-access-01' OR b_device='sw-access-01'"
    ).fetchone()
    assert row is not None
    ends = {(row["a_device"], row["a_interface"]), (row["b_device"], row["b_interface"])}
    assert ("sw-core-01", "to-access-01") in ends, ends


class _UnifiEmptyClients(_UnifiClient):
    """Controller answering stat/sta with an empty list (transient hiccup)."""
    def get(self, url, **kw):
        r = super().get(url, **kw)
        if "stat/sta" in url:
            class R:
                status_code = 200
                def raise_for_status(self_): pass
                def json(self_): return {"data": []}
            return R()
        return r


def test_unifi_empty_client_list_keeps_fdb_rows(conn, clean_env, monkeypatch):
    """An empty stat/sta response must not wipe unifi fdb rows — same
    empty-response guard the librenms fdb path uses."""
    from patchbay.collectors.unifi import UnifiCollector
    monkeypatch.setattr(httpx, "Client", _UnifiClient)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM fdb WHERE source='unifi'").fetchone()[0] == 1
    monkeypatch.setattr(httpx, "Client", _UnifiEmptyClients)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM fdb WHERE source='unifi'").fetchone()[0] == 1


def test_fdb_cross_source_rows_coexist_and_die_independently(conn):
    """source is part of the fdb key: two collectors seeing the same MAC on
    the same port each own a row, and one source dropping the MAC must not
    delete the other source's still-true row."""
    conn.execute("INSERT OR IGNORE INTO fdb (device, interface, mac, source) "
                 "VALUES ('sw1', '1/0/2', '02:00:00:00:07:01', 'unifi')")
    conn.execute("INSERT OR IGNORE INTO fdb (device, interface, mac, source) "
                 "VALUES ('sw1', '1/0/2', '02:00:00:00:07:01', 'librenms')")
    assert conn.execute("SELECT COUNT(*) FROM fdb").fetchone()[0] == 2
    conn.execute("DELETE FROM fdb WHERE source = 'unifi'")
    left = conn.execute("SELECT source FROM fdb").fetchall()
    assert [r["source"] for r in left] == ["librenms"]


def test_fdb_pk_migration_rebuilds_old_table(tmp_path):
    """A database whose fdb PRIMARY KEY predates the source column (or has
    source outside the key) is rebuilt in place, keeping its rows."""
    import sqlite3 as s3
    from patchbay import db as pdb
    p = str(tmp_path / "old.db")
    c = s3.connect(p)
    c.execute("CREATE TABLE fdb (device TEXT NOT NULL, interface TEXT NOT NULL, "
              "mac TEXT NOT NULL, PRIMARY KEY (device, interface, mac))")
    c.execute("INSERT INTO fdb VALUES ('sw1', '1/0/2', '02:00:00:00:07:01')")
    c.commit()
    pdb.init(c)
    pk = {r[1]: r[5] for r in c.execute("PRAGMA table_info(fdb)")}
    assert pk["source"] > 0, pk
    row = c.execute("SELECT * FROM fdb").fetchone()
    assert row == ("sw1", "1/0/2", "02:00:00:00:07:01", "librenms")
    pdb.init(c)  # idempotent
    assert c.execute("SELECT COUNT(*) FROM fdb").fetchone()[0] == 1


class _OnsClientEmptyExport(_OnsClient):
    """interfaces export answers 200 with an empty list."""
    def get(self, url, **kw):
        r = super().get(url, **kw)
        if "overview" in url:
            outer = self
            class E:
                status_code = 200
                def raise_for_status(self_): pass
                def json(self_): return []
            return E()
        return r


def test_opnsense_empty_export_keeps_port_vlans(conn, clean_env, monkeypatch):
    """A 200-with-empty-list interfaces export must not wipe port_vlans or
    run the tunnel purge — same stance as a denied export."""
    from patchbay.collectors.opnsense import OpnsenseCollector
    monkeypatch.setattr(httpx, "Client", _OnsClient)
    OpnsenseCollector().collect(_ons_settings(clean_env), conn)
    assert conn.execute("SELECT COUNT(*) FROM port_vlans WHERE device='fw1'").fetchone()[0] == 1
    monkeypatch.setattr(httpx, "Client", _OnsClientEmptyExport)
    OpnsenseCollector().collect(_ons_settings(clean_env), conn)
    assert conn.execute("SELECT COUNT(*) FROM port_vlans WHERE device='fw1'").fetchone()[0] == 1
