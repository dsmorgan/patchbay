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


def test_opnsense_403_notes_name_the_privilege():
    """A 403 should tell the operator what to click under System → Access,
    not just that a privilege is missing somewhere."""
    from patchbay.collectors.opnsense import _forbidden_note

    assert "'Status: OpenVPN'" in _forbidden_note("openvpn/service/searchSessions")
    assert "'VPN: Wireguard: Status'" in _forbidden_note("wireguard/service/show")
    assert "'Diagnostics: ARP Table'" in _forbidden_note("diagnostics/interface/get_arp")
    # unknown endpoints keep the generic hint
    assert "matching page privilege" in _forbidden_note("some/new/endpoint")


def test_ifname_echo_descr_scrubbed():
    """FastIron echoes the long-form port name as ifAlias when no comment is
    set — same port, different spelling, zero information."""
    assert _descr("GigabitEthernet1/1/10", "ethernet1/1/10") == ""
    assert _descr("10GigabitEthernet1/2/1", "ethernet1/2/1") == ""
    # a real comment survives, and so does a name that isn't this port's
    assert _descr("[1] bedroom4", "ethernet1/1/1") == "[1] bedroom4"
    assert _descr("GigabitEthernet1/1/9", "ethernet1/1/10") == "GigabitEthernet1/1/9"


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
            text = ""
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
                    return [
                        {"destination": "default", "gateway": "203.0.113.1",
                         "netif": "igc0", "proto": "ipv4", "flags": "UGS"},
                        {"destination": "198.51.100.0/24", "gateway": "link#2",
                         "netif": "igc0.20", "proto": "ipv4", "flags": "U"},
                    ]
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


def test_opnsense_default_route_normalized(conn, clean_env, monkeypatch):
    """'default' is stored as 0.0.0.0/0 — it names the WAN exit interface
    for the routed view; reachability consumers skip /0 themselves."""
    from patchbay.collectors.opnsense import OpnsenseCollector
    monkeypatch.setattr(httpx, "Client", _OnsClient)
    OpnsenseCollector().collect(_ons_settings(clean_env), conn)
    row = conn.execute(
        "SELECT gateway, interface FROM routes WHERE device='fw1' "
        "AND destination='0.0.0.0/0'").fetchone()
    assert row is not None
    assert row["gateway"] == "203.0.113.1" and row["interface"] == "igc0"


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
    "general_temperature": 52, "has_temperature": True,
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
    "general_temperature": 0, "has_temperature": False,
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


# --- UniFi: MODEL_NAMES translation ------------------------------------------

class _UnifiRawModel(_UnifiClient):
    """Controller returning a known raw model code for the AP."""
    def get(self, url, **kw):
        r = super().get(url, **kw)
        if "stat/device" in url:
            ap = dict(_UAP, model="U7PG2")
            class R:
                status_code = 200
                def raise_for_status(self_): pass
                def json(self_): return {"data": [_USW, ap]}
            return R()
        return r


def test_unifi_model_code_translated(conn, clean_env, monkeypatch):
    """Raw UniFi model codes are translated to display names via MODEL_NAMES."""
    from patchbay.collectors.unifi import UnifiCollector
    monkeypatch.setattr(httpx, "Client", _UnifiRawModel)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    dev = conn.execute("SELECT model FROM devices WHERE name='ap-floor1'").fetchone()
    assert dev is not None and dev["model"] == "AC Pro", dev


# --- LibreNMS: HARDWARE_ALIASES correction ------------------------------------

def _lnms_settings(clean_env):
    from patchbay.config import load_settings
    clean_env.setenv("LIBRENMS_URL", "http://librenms.example.net")
    clean_env.setenv("LIBRENMS_TOKEN", "testtoken")
    return load_settings()


class _LnmsClient:
    """LibreNMS API stub: one device with UAP-AC-Pro-Gen2 as hardware."""
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def get(self, url, **kw):
        class R:
            status_code = 200
            def raise_for_status(self_): pass
            def json(self_):
                if url.endswith("/devices"):
                    return {"devices": [{"device_id": 1, "sysName": "ap1",
                                         "hardware": "UAP-AC-Pro-Gen2",
                                         "os": "unifi", "status": 1,
                                         "disabled": 0, "serial": None,
                                         "ip": "192.0.2.20", "overwrite_ip": None}]}
                if "/ports" in url:
                    return {"ports": []}
                return {"links": [], "vlans": [], "ports_fdb": []}
        return R()


def test_librenms_hardware_alias_corrected(conn, clean_env, monkeypatch):
    """HARDWARE_ALIASES maps wrong sysDescr values to correct product names."""
    from patchbay.collectors.librenms import LibreNmsCollector
    monkeypatch.setattr(httpx, "Client", _LnmsClient)
    LibreNmsCollector().collect(_lnms_settings(clean_env), conn)
    dev = conn.execute("SELECT vendor FROM devices WHERE name='ap1'").fetchone()
    assert dev is not None and dev["vendor"] == "UAP-AC-Pro", dev


# --- opnsense: firewall config history (#23) ---------------------------------

_CFG_XML = """<?xml version="1.0"?>
<opnsense>
  <revision>
    <username>root@192.0.2.9</username>
    <time>1756500000.1</time>
    <description>/firewall_rules.php made changes</description>
  </revision>
  <system><hostname>fw1</hostname></system>
  <cert><crt>FAKECERTB64</crt><prv>FAKEKEYB64</prv></cert>
  <user><password>$2y$10$fakehash</password><authorizedkeys>ssh-ed25519 FAKE</authorizedkeys></user>
  <filter><rule><descr>allow lan</descr></rule></filter>
</opnsense>
"""


def test_prepare_config_redacts_and_lifts_revision_meta():
    from patchbay.collectors.opnsense import prepare_config
    text, msg, author = prepare_config(_CFG_XML)
    for secret in ("FAKEKEYB64", "FAKECERTB64", "$2y$10$fakehash", "ssh-ed25519 FAKE"):
        assert secret not in text
    assert "redacted:" in text
    assert "<revision>" not in text
    assert "allow lan" in text                      # real content survives
    assert msg == "/firewall_rules.php made changes"
    assert author == "root@192.0.2.9"


def test_config_revision_noop_save_is_no_revision(conn):
    from patchbay.collectors.opnsense import save_config_revision
    assert save_config_revision(conn, "fw1", _CFG_XML) is True
    resaved = _CFG_XML.replace("1756500000.1", "1756500999.9").replace(
        "/firewall_rules.php made changes", "no-op save")
    assert save_config_revision(conn, "fw1", resaved) is False
    assert conn.execute("SELECT COUNT(*) FROM config_revisions").fetchone()[0] == 1


def test_config_revision_real_change_is_recorded(conn):
    from patchbay.collectors.opnsense import save_config_revision
    save_config_revision(conn, "fw1", _CFG_XML)
    changed = _CFG_XML.replace("allow lan", "allow lan and dmz").replace(
        "/firewall_rules.php made changes", "rule edited")
    assert save_config_revision(conn, "fw1", changed) is True
    rows = conn.execute("SELECT message FROM config_revisions "
                        "ORDER BY fetched_at DESC, id DESC").fetchall()
    assert len(rows) == 2 and rows[0]["message"] == "rule edited"


def test_config_revision_rotated_key_still_reads_as_change(conn):
    # a rotated private key must register as a change without being stored
    from patchbay.collectors.opnsense import save_config_revision
    save_config_revision(conn, "fw1", _CFG_XML)
    rotated = _CFG_XML.replace("FAKEKEYB64", "NEWFAKEKEY")
    assert save_config_revision(conn, "fw1", rotated) is True
    for r in conn.execute("SELECT text FROM config_revisions"):
        assert "FAKEKEYB64" not in r["text"] and "NEWFAKEKEY" not in r["text"]


def test_config_revisions_pruned_to_keep(conn, monkeypatch):
    from patchbay.collectors import opnsense as ons
    monkeypatch.setattr(ons, "CONFIG_REVISIONS_KEEP", 3)
    for i in range(5):
        ons.save_config_revision(conn, "fw1", _CFG_XML.replace("allow lan", f"rule {i}"))
    assert conn.execute("SELECT COUNT(*) FROM config_revisions").fetchone()[0] == 3
    kept = [r["text"] for r in conn.execute(
        "SELECT text FROM config_revisions ORDER BY fetched_at, id")]
    assert "rule 4" in kept[-1]


class _OnsClientWithConfig(_OnsClient):
    """Adds a config.xml response on the backup endpoint."""
    def get(self, url, **kw):
        r = super().get(url, **kw)
        if "backup/download" in url:
            class C:
                status_code = 200
                text = _CFG_XML
                def raise_for_status(self_): pass
            return C()
        return r


def test_opnsense_collect_stores_config_revision(conn, clean_env, monkeypatch):
    from patchbay.collectors.opnsense import OpnsenseCollector
    monkeypatch.setattr(httpx, "Client", _OnsClientWithConfig)
    summary = OpnsenseCollector().collect(_ons_settings(clean_env), conn)
    assert "new config revision" in summary
    row = conn.execute("SELECT * FROM config_revisions WHERE device='fw1'").fetchone()
    assert row is not None and "FAKEKEYB64" not in row["text"]
    # raw config.xml must never land in raw_payloads
    for r in conn.execute("SELECT payload FROM raw_payloads"):
        assert "FAKEKEYB64" not in r["payload"]


def test_prepare_config_redacts_bare_key_and_unknown_long_blobs():
    # an ACME account key ships under a bare <key>; unknown plugins invent
    # more names, so any long base64 run is redacted by shape too
    from patchbay.collectors.opnsense import prepare_config
    blob = "QUJD" * 100  # 400 chars of base64-looking content
    raw = _CFG_XML.replace(
        "<filter>",
        f"<acme><account><key>{blob}</key></account></acme>"
        f"<mystery><widget>{blob}</widget></mystery><filter>")
    text, _, _ = prepare_config(raw)
    assert blob not in text
    assert text.count("redacted:") >= 6
    assert "allow lan" in text          # short real content untouched


def test_unifi_temperature_stored_when_sensed(conn, clean_env, monkeypatch):
    """#40: general_temperature lands as a device fact — but only where
    has_temperature says the sensor is real, and only while the device is
    up (stale liveness is omitted, not written)."""
    from patchbay.collectors.unifi import UnifiCollector, _temperature
    monkeypatch.setattr(httpx, "Client", _UnifiClient)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    t = {r["name"]: r["temperature"] for r in conn.execute(
        "SELECT name, temperature FROM devices")}
    assert t["sw-access-01"] == 52.0
    assert t["ap-floor1"] is None            # has_temperature: false
    assert _temperature({"general_temperature": 47}) == 47.0
    assert _temperature({"general_temperature": "junk"}) is None


def test_unifi_down_device_temperature_not_written(conn, clean_env, monkeypatch):
    """A non-up device's cached reading is stale liveness — omitted, and the
    last good value stands (upsert drops None)."""
    from patchbay.collectors.unifi import UnifiCollector

    class DownSwitch(_UnifiClient):
        def get(self, url, **kw):
            class R:
                status_code = 200
                def raise_for_status(self_): pass
                def json(self_):
                    if "stat/device" in url:
                        # the controller still relays the cached reading
                        return {"data": [{**_USW, "state": 0}, _UAP]}
                    return {"data": []}
            return R()

    monkeypatch.setattr(httpx, "Client", _UnifiClient)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    monkeypatch.setattr(httpx, "Client", DownSwitch)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    row = conn.execute("SELECT status, temperature FROM devices "
                       "WHERE name='sw-access-01'").fetchone()
    assert row["status"] != "up"
    assert row["temperature"] == 52.0        # last good reading stands


def test_save_raw_strips_credential_fields(conn):
    """raw_payloads is a debugging aid, never a credential store: any key
    whose name smells like a secret has its value dropped before storage."""
    import json as _json
    pdb.save_raw(conn, source="librenms", endpoint="devices", payload=[{
        "hostname": "sw1.example.lan", "community": "s3cret",
        "authpass": "p", "cryptopass": "c", "authlevel": "authPriv",
        "port": 161, "uplink_remote_port": 3,
    }, {"x_authkey": "k", "guest_token": "t", "syslog_key": "s", "name": "ap1"}])
    stored = _json.loads(conn.execute(
        "SELECT payload FROM raw_payloads").fetchone()["payload"])
    a, b = stored
    assert a["hostname"] == "sw1.example.lan"      # useful fields survive
    assert a["port"] == 161 and a["uplink_remote_port"] == 3
    for k in ("community", "authpass", "cryptopass"):
        assert a[k] == "<stripped>", k
    for k in ("x_authkey", "guest_token", "syslog_key"):
        assert b[k] == "<stripped>", k


def test_prepare_config_matches_secret_tags_as_substrings(conn):
    """Exact-tag matching let <rocommunity>, <sharedsecret>, <otp_seed>, and
    <varusersfreeradiuspassword> through — plugin tags merely *contain* the
    secret keyword. Innocent elements still survive."""
    from patchbay.collectors.opnsense import prepare_config
    raw = ("<opnsense><otp_seed>GEZDGNBVGY3TQOJQ</otp_seed>"
           "<rocommunity>notpublic</rocommunity>"
           "<sharedsecret>radiuspw</sharedsecret>"
           "<varusersfreeradiuspassword>frp</varusersfreeradiuspassword>"
           "<descr>allow lan</descr><hostname>fw1</hostname></opnsense>")
    text, _, _ = prepare_config(raw)
    for secret in ("GEZDGNBVGY3TQOJQ", "notpublic", "radiuspw", "frp"):
        assert secret not in text, secret
    assert "allow lan" in text and "fw1" in text


def test_pfsense_failed_calls_keep_last_good_rows(conn, clean_env, monkeypatch):
    """A 403/empty response is degraded visibility, not an empty network:
    gateways and port_vlans from the last good poll must survive it."""
    from patchbay.collectors.pfsense import PfsenseCollector

    monkeypatch.setattr(httpx, "Client", PfClient)
    s = _pf_settings(clean_env)
    PfsenseCollector().collect(s, conn)
    assert conn.execute("SELECT COUNT(*) FROM gateways WHERE source='pfsense'"
                        ).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM port_vlans WHERE device='fw2'"
                        ).fetchone()[0] == 1
    monkeypatch.setattr(PfClient, "fail_gateways", True)
    monkeypatch.setattr(PfClient, "interfaces", [])   # 200 with empty body
    PfsenseCollector().collect(s, conn)
    assert conn.execute("SELECT COUNT(*) FROM gateways WHERE source='pfsense'"
                        ).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM port_vlans WHERE device='fw2'"
                        ).fetchone()[0] == 1


def test_phpipam_empty_listing_keeps_address_book(conn, clean_env, monkeypatch):
    """subnets/ answering 404 (phpIPAM's 'no results') must not commit an
    empty address book — the fetch-before-delete guard's other half."""
    from patchbay.collectors.phpipam import PhpIpamCollector
    from patchbay.config import load_settings

    conn.execute("INSERT INTO ipam_addresses (ip, hostname) VALUES ('192.0.2.9', 'old')")
    clean_env.setenv("IPAM_URL", "https://ipam.example/api")
    clean_env.setenv("IPAM_APP_ID", "app")
    clean_env.setenv("IPAM_TOKEN", "t")

    class EmptyClient(FlakyClient):
        def get(self, url, **kw):
            if url.endswith("vlan/"):
                return FakeResponse([])
            if url.endswith("subnets/"):
                r = FakeResponse([])
                r.status_code = 404
                return r
            raise AssertionError("unexpected call")

    monkeypatch.setattr(httpx, "Client", EmptyClient)
    summary = PhpIpamCollector().collect(load_settings(), conn)
    assert "kept previous" in summary
    rows = [r["ip"] for r in conn.execute("SELECT ip FROM ipam_addresses")]
    assert rows == ["192.0.2.9"]


def test_phpipam_prunes_deleted_vlans_but_not_claimed_ones(conn, clean_env, monkeypatch):
    """A VLAN deleted from IPAM leaves the model — unless a device still
    claims it, in which case it just loses its IPAM documentation. An
    empty listing prunes nothing."""
    from patchbay.collectors.phpipam import PhpIpamCollector
    from patchbay.config import load_settings

    for vid in (10, 2001, 24):
        conn.execute("INSERT INTO vlans (vid, name, source) VALUES (?, 'x', 'phpipam')",
                     (vid,))
    conn.execute("INSERT INTO port_vlans VALUES ('sw1', '1/0/1', 24, 1, 'config')")
    clean_env.setenv("IPAM_URL", "https://ipam.example/api")
    clean_env.setenv("IPAM_APP_ID", "app")
    clean_env.setenv("IPAM_TOKEN", "t")

    class VlanClient(FlakyClient):
        listing = [{"number": "10", "name": "keep", "vlanId": "1"}]

        def get(self, url, **kw):
            if url.endswith("vlan/"):
                return FakeResponse(self.listing)
            if url.endswith("subnets/"):
                return FakeResponse([{"subnet": "192.0.2.0", "mask": "24",
                                      "id": 1, "sectionId": 1}])
            if "subnets/1/" in url:
                return FakeResponse([])
            raise AssertionError(url)

    monkeypatch.setattr(httpx, "Client", VlanClient)
    PhpIpamCollector().collect(load_settings(), conn)
    left = {r[0] for r in conn.execute("SELECT vid FROM vlans")}
    assert left == {10, 24}                 # 2001 pruned; claimed 24 survives

    monkeypatch.setattr(VlanClient, "listing", [])
    PhpIpamCollector().collect(load_settings(), conn)
    assert {r[0] for r in conn.execute("SELECT vid FROM vlans")} == {10, 24}


# --- audit round 4 (#41): liveness gates and pruning -------------------------

import sys
import types

_ns = types.SimpleNamespace


class _VEthCard:
    """Stands in for vim.vm.device.VirtualEthernetCard."""

    def __init__(self, mac, portgroup):
        self.macAddress = mac
        self.backing = _ns(deviceName=portgroup)
        self.deviceInfo = _ns(label="Network adapter 1")
        self.connectable = _ns(connected=True)


def _vs_host(name, state="connected", portgroups=()):
    return _ns(
        name=name,
        runtime=_ns(connectionState=state),
        hardware=_ns(systemInfo=_ns(vendor="ACME", model="Rack1")),
        config=_ns(
            product=_ns(version="8.0.2"),
            network=_ns(
                vnic=[], pnic=[],
                portgroup=[_ns(spec=_ns(name=pg, vlanId=vid))
                           for pg, vid in portgroups]),
        ),
        configManager=_ns(networkSystem=_ns(QueryNetworkHint=lambda: [])),
    )


def _vs_vm(name, host, power="poweredOn", nics=()):
    return _ns(
        name=name,
        runtime=_ns(host=host, powerState=power),
        guest=_ns(ipAddress=None),
        config=_ns(template=False, guestFullName="FreeBSD 14",
                   hardware=_ns(device=list(nics))),
    )


def _vs_stub(monkeypatch, hosts, vms):
    """Install fake pyVim/pyVmomi modules serving the given inventory."""
    class _Vim:
        HostSystem = type("HostSystem", (), {})
        VirtualMachine = type("VirtualMachine", (), {})
        dvs = _ns(DistributedVirtualPortgroup=type("DVPG", (), {}))
        vm = _ns(device=_ns(VirtualEthernetCard=_VEthCard))

    def create_view(_root, kinds, _recurse):
        if kinds[0] is _Vim.HostSystem:
            return _ns(view=hosts)
        if kinds[0] is _Vim.VirtualMachine:
            return _ns(view=vms)
        return _ns(view=[])

    content = _ns(viewManager=_ns(CreateContainerView=create_view))
    content.rootFolder = None
    si = _ns(RetrieveContent=lambda: content)
    pyvim_connect = types.ModuleType("pyVim.connect")
    pyvim_connect.SmartConnect = lambda **kw: si
    pyvim_connect.Disconnect = lambda s: None
    pyvim = types.ModuleType("pyVim")
    pyvim.connect = pyvim_connect
    pyvmomi = types.ModuleType("pyVmomi")
    pyvmomi.vim = _Vim
    monkeypatch.setitem(sys.modules, "pyVim", pyvim)
    monkeypatch.setitem(sys.modules, "pyVim.connect", pyvim_connect)
    monkeypatch.setitem(sys.modules, "pyVmomi", pyvmomi)


def _vs_settings(clean_env):
    from patchbay.config import load_settings
    clean_env.setenv("VSPHERE_HOST", "vcenter.example.net")
    clean_env.setenv("VSPHERE_USER", "u")
    clean_env.setenv("VSPHERE_PASS", "p")
    return load_settings()


def test_vsphere_vm_status_gated_on_host_liveness(conn, clean_env, monkeypatch):
    """A notResponding host's VM powerState is a cached value: writing it with
    a fresh last_seen would let the merge prefer it over the device's own
    collector. Placement still lands (identity); status stays no-opinion."""
    from patchbay.collectors.vsphere import VsphereCollector
    pdb.upsert_device(conn, name="fw1", source="opnsense", role="firewall",
                      status="up")
    hyp_ok = _vs_host("hyp1")
    hyp_stale = _vs_host("hyp2", state="notResponding")
    vms = [_vs_vm("vm-live", hyp_ok, power="poweredOff"),
           _vs_vm("vm-cached", hyp_stale, power="poweredOff"),
           _vs_vm("fw1", hyp_stale, power="poweredOff")]
    _vs_stub(monkeypatch, [hyp_ok, hyp_stale], vms)
    VsphereCollector().collect(_vs_settings(clean_env), conn)
    st = {r["name"]: r["status"]
          for r in conn.execute("SELECT name, status FROM devices")}
    assert st["vm-live"] == "down"      # live host: powerState is the truth
    assert st["vm-cached"] is None      # cached report: no opinion written
    assert st["fw1"] == "up"            # own collector's status survives
    assert conn.execute("SELECT parent FROM devices WHERE name='fw1'"
                        ).fetchone()[0] == "hyp2"


def test_vsphere_vnic_vlans_refresh_replaces_stale_rows(conn, clean_env, monkeypatch):
    """vnic_vlans was upsert-only: a deleted VM's MAC (or a port group moved
    to untagged) lingered forever and kept re-emitting phantom port_vlans.
    Refresh-by-replace, scoped to this collector's rows."""
    from patchbay.collectors.vsphere import VsphereCollector
    conn.execute("INSERT INTO vnic_vlans (mac, vid, portgroup, source) VALUES "
                 "('02:00:00:00:0f:01', 99, 'PG99', 'vsphere')")
    conn.execute("INSERT INTO vnic_vlans (mac, vid, portgroup, source) VALUES "
                 "('02:00:00:00:0f:02', 12, 'other', 'not-vsphere')")
    hyp = _vs_host("hyp1", portgroups=[("PG13", 13)])
    vm = _vs_vm("vm-a", hyp, nics=[_VEthCard("02:00:00:00:0f:03", "PG13")])
    _vs_stub(monkeypatch, [hyp], [vm])
    VsphereCollector().collect(_vs_settings(clean_env), conn)
    rows = {r["mac"]: (r["vid"], r["source"])
            for r in conn.execute("SELECT * FROM vnic_vlans")}
    assert "02:00:00:00:0f:01" not in rows          # stale own row pruned
    assert rows["02:00:00:00:0f:02"] == (12, "not-vsphere")  # foreign row kept
    assert rows["02:00:00:00:0f:03"] == (13, "vsphere")


class _UnifiDownSwitch(_UnifiClient):
    """Same fixture devices, but the switch is reported down (cached
    port_table) and the AP heartbeat-missed."""

    def get(self, url, **kw):
        class R:
            status_code = 200
            def raise_for_status(self_): pass
            def json(self_):
                if "stat/device" in url:
                    return {"data": [dict(_USW, state=0), dict(_UAP, state=6)]}
                return {"data": []}
        return R()


def test_unifi_down_device_port_liveness_omitted(conn, clean_env, monkeypatch):
    """A non-up device's port_table is the controller's cache: oper/admin/
    speed must not overwrite the last good values (omitted means no opinion,
    same stance as vCenter's disconnected hosts)."""
    from patchbay.collectors.unifi import UnifiCollector
    monkeypatch.setattr(httpx, "Client", _UnifiClient)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    monkeypatch.setattr(httpx, "Client", _UnifiDownSwitch)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    sw = conn.execute("SELECT status FROM devices WHERE name='sw-access-01'"
                      ).fetchone()
    assert sw["status"] == "down"           # device liveness IS the report
    port = conn.execute(
        "SELECT i.oper_status, i.speed_bps FROM interfaces i "
        "JOIN devices d ON d.id=i.device_id "
        "WHERE d.name='sw-access-01' AND i.name='Port 1'").fetchone()
    assert port["oper_status"] == "up"      # last good value stands
    assert port["speed_bps"] == 1_000_000_000
    ap = conn.execute(
        "SELECT i.oper_status, i.mac FROM interfaces i "
        "JOIN devices d ON d.id=i.device_id "
        "WHERE d.name='ap-floor1' AND i.name='eth0'").fetchone()
    assert ap["oper_status"] == "up"        # cached uplink state not written
    assert ap["mac"] == "02:00:00:00:03:01"  # identity still lands


class _UnifiNoDevices(_UnifiClient):
    def get(self, url, **kw):
        class R:
            status_code = 200
            def raise_for_status(self_): pass
            def json(self_): return {"data": []}
        return R()


def test_unifi_retires_devices_the_controller_forgot(conn, clean_env, monkeypatch):
    """An AP removed from the controller must leave the model; rows owned by
    other collectors (and non-ap/switch roles) are not unifi's to retire.
    An empty listing never prunes."""
    from patchbay.collectors.unifi import UnifiCollector
    pdb.upsert_device(conn, name="ap-old", source="unifi", role="ap")
    pdb.upsert_device(conn, name="ap-lnms", source="librenms", role="ap")
    monkeypatch.setattr(httpx, "Client", _UnifiClient)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    names = {r[0] for r in conn.execute("SELECT name FROM devices")}
    assert "ap-old" not in names
    assert {"ap-lnms", "sw-access-01", "ap-floor1"} <= names

    pdb.upsert_device(conn, name="ap-old", source="unifi", role="ap")
    monkeypatch.setattr(httpx, "Client", _UnifiNoDevices)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    assert conn.execute("SELECT COUNT(*) FROM devices WHERE name='ap-old'"
                        ).fetchone()[0] == 1


class _LnmsFqdnClient(_LnmsClient):
    """ap1 plus a device LibreNMS names by FQDN."""

    def get(self, url, **kw):
        class R:
            status_code = 200
            def raise_for_status(self_): pass
            def json(self_):
                if url.endswith("/devices"):
                    return {"devices": [
                        {"device_id": 1, "sysName": "ap1",
                         "hardware": "UAP-AC-Pro-Gen2", "os": "unifi",
                         "status": 1, "disabled": 0, "serial": None,
                         "ip": "192.0.2.20", "overwrite_ip": None},
                        {"device_id": 2, "sysName": "core-sw1.example.net",
                         "hardware": "M4300", "os": "netgear",
                         "status": 1, "disabled": 0, "serial": None,
                         "ip": "192.0.2.21", "overwrite_ip": None}]}
                if "/ports" in url:
                    return {"ports": []}
                return {"links": [], "vlans": [], "ports_fdb": []}
        return R()


def test_librenms_retires_removed_devices(conn, clean_env, monkeypatch):
    """A device deleted from LibreNMS leaves the model. Post-normalize rows
    hold canonical (short) names while the listing may say FQDN — both forms
    must match. Other collectors' rows are untouched."""
    from patchbay.collectors.librenms import LibreNmsCollector
    pdb.upsert_device(conn, name="gone-sw", source="librenms", role="switch")
    pdb.upsert_device(conn, name="core-sw1", source="librenms", role="switch")
    pdb.upsert_device(conn, name="fw1", source="opnsense", role="firewall")
    monkeypatch.setattr(httpx, "Client", _LnmsFqdnClient)
    LibreNmsCollector().collect(_lnms_settings(clean_env), conn)
    names = {r[0] for r in conn.execute("SELECT name FROM devices")}
    assert "gone-sw" not in names
    assert {"core-sw1", "fw1", "ap1"} <= names


class _LnmsVlanClient(_LnmsClient):
    vlans = [{"device_id": 1, "vlan_vlan": 22, "vlan_name": "lab"}]

    def get(self, url, **kw):
        outer = self

        class R:
            status_code = 200
            def raise_for_status(self_): pass
            def json(self_):
                if url.endswith("/devices"):
                    return {"devices": [{"device_id": 1, "sysName": "ap1",
                                         "hardware": None, "os": "unifi",
                                         "status": 1, "disabled": 0,
                                         "serial": None, "ip": "192.0.2.20",
                                         "overwrite_ip": None}]}
                if "/ports" in url:
                    return {"ports": []}
                if url.endswith("resources/vlans"):
                    return {"vlans": outer.vlans}
                return {"links": [], "vlans": [], "ports_fdb": []}
        return R()


def test_librenms_vlan_prune_claim_aware(conn, clean_env, monkeypatch):
    """A VLAN Q-BRIDGE stops reporting is pruned — but only librenms's own
    rows, never one a device still claims, and never on an empty listing."""
    from patchbay.collectors.librenms import LibreNmsCollector
    now = pdb.now()
    conn.executemany(
        "INSERT INTO vlans (vid, name, source, last_seen) VALUES (?, ?, ?, ?)",
        [(30, "stale", "librenms", now), (31, "claimed", "librenms", now),
         (40, "foreign", "oxidized", now)])
    conn.execute("INSERT INTO port_vlans (device, interface, vid, tagged, source) "
                 "VALUES ('sw9', 'ethernet1/1/1', 31, 1, 'oxidized')")
    monkeypatch.setattr(httpx, "Client", _LnmsVlanClient)
    LibreNmsCollector().collect(_lnms_settings(clean_env), conn)
    left = {r[0] for r in conn.execute("SELECT vid FROM vlans")}
    assert left == {22, 31, 40}             # 30 pruned; claimed + foreign kept

    conn.execute("INSERT INTO vlans (vid, name, source, last_seen) "
                 "VALUES (30, 'stale', 'librenms', ?)", (now,))
    monkeypatch.setattr(_LnmsVlanClient, "vlans", [])
    LibreNmsCollector().collect(_lnms_settings(clean_env), conn)
    assert 30 in {r[0] for r in conn.execute("SELECT vid FROM vlans")}


class _OxClient:
    """Oxidized API stub: one FastIron switch whose config defines VLAN 22."""
    nodes = [{"name": "sw1.example.net", "full_name": "sw1.example.net",
              "model": "ironware", "last": {"status": "success"}}]
    config = "vlan 22 name lab\n tagged ethe 1/1/1\n"

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def get(self, url, **kw):
        outer = self

        class R:
            status_code = 200
            text = outer.config
            def raise_for_status(self_): pass
            def json(self_): return outer.nodes
        return R()


def _ox_settings(clean_env):
    from patchbay.config import load_settings
    clean_env.setenv("OXIDIZED_URL", "http://oxidized.example.net:8888")
    return load_settings()


def test_oxidized_port_vlans_delete_scoped_to_source(conn, clean_env, monkeypatch):
    """The rewrite must only clear oxidized's own rows for the device — a
    device-wide delete could remove another writer's rows."""
    from patchbay.collectors.oxidized import OxidizedCollector
    conn.execute("INSERT INTO port_vlans (device, interface, vid, tagged, source) "
                 "VALUES ('sw1', 'old-port', 9, 1, 'oxidized')")
    conn.execute("INSERT INTO port_vlans (device, interface, vid, tagged, source) "
                 "VALUES ('sw1', 'vmx1', 13, 0, 'vsphere')")
    monkeypatch.setattr(httpx, "Client", _OxClient)
    OxidizedCollector().collect(_ox_settings(clean_env), conn)
    rows = {(r["interface"], r["vid"], r["source"]) for r in
            conn.execute("SELECT * FROM port_vlans WHERE device='sw1'")}
    assert ("old-port", 9, "oxidized") not in rows      # own stale row gone
    assert ("vmx1", 13, "vsphere") in rows              # foreign row kept
    assert ("ethernet1/1/1", 22, "oxidized") in rows


def test_oxidized_vlan_prune_claim_aware(conn, clean_env, monkeypatch):
    """A VLAN removed from every parsed config is pruned — own rows only,
    claims protect it (including a skipped device's surviving port_vlans)."""
    from patchbay.collectors.oxidized import OxidizedCollector
    now = pdb.now()
    conn.executemany(
        "INSERT INTO vlans (vid, name, source, last_seen) VALUES (?, ?, ?, ?)",
        [(30, "stale", "oxidized", now), (31, "claimed", "oxidized", now),
         (40, "foreign", "phpipam", now)])
    # VLAN 31 belongs to a device whose config was skipped this run — its
    # surviving port_vlans row is the claim that protects it
    conn.execute("INSERT INTO port_vlans (device, interface, vid, tagged, source) "
                 "VALUES ('sw2', 'ethernet2/1/1', 31, 1, 'oxidized')")
    monkeypatch.setattr(httpx, "Client", _OxClient)
    OxidizedCollector().collect(_ox_settings(clean_env), conn)
    left = {r[0] for r in conn.execute("SELECT vid FROM vlans")}
    assert left == {22, 31, 40}             # 30 pruned; claimed + foreign kept


# --- VPN tunnels (#42) -------------------------------------------------------

def test_scrub_payload_strips_hyphenated_key_fields(conn):
    from patchbay.db import _scrub_payload
    out = _scrub_payload({"public-key": "AAA", "preshared-key": "BBB",
                          "psk": "CCC", "endpoint": "192.0.2.200:51820",
                          "monkey": "notakey"})
    assert out["public-key"] == "<stripped>"
    assert out["preshared-key"] == "<stripped>"
    assert out["psk"] == "<stripped>"
    assert out["endpoint"] == "192.0.2.200:51820"
    assert out["monkey"] == "notakey"


def test_wg_status_thresholds():
    from patchbay.collectors.opnsense import WG_HANDSHAKE_FRESH, wg_status
    now = 1_800_000_000.0
    assert wg_status(None, now) == "down"
    assert wg_status(0, now) == "down"
    assert wg_status(now - 30, now) == "up"
    assert wg_status(now - WG_HANDSHAKE_FRESH - 1, now) == "idle"


class _OnsClientVpn(_OnsClient):
    """Adds WireGuard/OpenVPN/IPsec status payloads to the base stub."""
    wg_rows = None      # set per-test; None = endpoint answers nothing (404)

    def get(self, url, **kw):
        outer = self

        class R:
            status_code = 200
            text = ""
            def raise_for_status(self_): pass
            def json(self_):
                if "wireguard/service/show" in url:
                    return {"rows": outer.wg_rows} if outer.wg_rows is not None else None
                if "searchSessions" in url:
                    return {"rows": [
                        {"id": "1", "description": "roadwarrior",
                         "status": "connected", "real_address": "198.51.100.9:1194"},
                        {"id": "1", "description": "roadwarrior",
                         "status": "connected", "real_address": "198.51.100.10:1194"},
                    ]}
                if "searchPhase1" in url:
                    return {"rows": [
                        {"phase1desc": "site-c", "connected": True,
                         "remote-addrs": "203.0.113.77", "version": "2"},
                    ]}
                return None
        r = R()
        if any(s in url for s in ("wireguard/service/show",)) and outer.wg_rows is None:
            return super().get(url, **kw)
        if r.json() is not None or any(
                s in url for s in ("wireguard", "searchSessions", "searchPhase1")):
            return r
        return super().get(url, **kw)


def test_opnsense_collects_vpn_tunnels(conn, clean_env, monkeypatch):
    """All three types land as first-class tunnel rows; WireGuard peers are
    named instance · peer, status from handshake age; OpenVPN sessions fold
    per instance; no key material reaches the database."""
    import time
    from patchbay.collectors.opnsense import OpnsenseCollector
    monkeypatch.setattr(_OnsClientVpn, "wg_rows", [
        {"if": "wg1", "type": "interface", "name": "wg-home",
         "public-key": "FAKEPUBKEYAAA", "status": "up"},
        {"if": "wg1", "type": "peer", "name": "site-b",
         "public-key": "FAKEPUBKEYBBB", "endpoint": "192.0.2.200:51820",
         "allowed-ips": "172.16.44.0/24",
         "latest-handshake": time.time() - 30},
    ])
    monkeypatch.setattr(httpx, "Client", _OnsClientVpn)
    OpnsenseCollector().collect(_ons_settings(clean_env), conn)
    rows = {(r["type"], r["name"]): dict(r) for r in
            conn.execute("SELECT * FROM tunnels")}
    wg = rows[("wireguard", "wg-home · site-b")]
    assert wg["peer"] == "192.0.2.200:51820" and wg["interface"] == "wg1"
    assert wg["status"] == "up" and wg["detail"] == "allowed 172.16.44.0/24"
    ov = rows[("openvpn", "roadwarrior")]
    assert ov["status"] == "up" and ov["detail"] == "2 sessions"
    sa = rows[("ipsec", "site-c")]
    assert sa["status"] == "up" and sa["peer"] == "203.0.113.77"
    assert sa["detail"] == "IKEv2"
    # no key material anywhere — tunnels or raw payloads
    for table in ("tunnels", "raw_payloads"):
        for r in conn.execute(f"SELECT * FROM {table}"):
            assert "FAKEPUBKEY" not in str(tuple(r))


def test_opnsense_absent_tunnel_endpoints_keep_rows(conn, clean_env, monkeypatch):
    """A firewall without the endpoints (older release) answers nothing —
    previously collected tunnel rows must stand, and the poll must not
    fail."""
    from patchbay.collectors.opnsense import OpnsenseCollector
    conn.execute("INSERT INTO tunnels (device, type, name, status, source, "
                 "last_seen) VALUES ('fw1', 'wireguard', 'site-b', 'up', "
                 "'opnsense', ?)", (pdb.now(),))
    monkeypatch.setattr(httpx, "Client", _OnsClient)   # base stub: no VPN answers
    OpnsenseCollector().collect(_ons_settings(clean_env), conn)
    assert conn.execute("SELECT COUNT(*) FROM tunnels").fetchone()[0] == 1


def test_opnsense_empty_tunnel_listing_prunes_own_rows(conn, clean_env, monkeypatch):
    """An answered-but-empty listing is the truth: stale rows of that type
    leave. Other devices' rows are not this collector's to prune."""
    from patchbay.collectors.opnsense import OpnsenseCollector
    conn.execute("INSERT INTO tunnels (device, type, name, status, source, "
                 "last_seen) VALUES ('fw1', 'wireguard', 'old-peer', 'up', "
                 "'opnsense', ?)", (pdb.now(),))
    conn.execute("INSERT INTO tunnels (device, type, name, status, source, "
                 "last_seen) VALUES ('fw9', 'wireguard', 'other-site', 'up', "
                 "'pfsense', ?)", (pdb.now(),))
    monkeypatch.setattr(_OnsClientVpn, "wg_rows", [])
    monkeypatch.setattr(httpx, "Client", _OnsClientVpn)
    OpnsenseCollector().collect(_ons_settings(clean_env), conn)
    left = {(r["device"], r["name"]) for r in conn.execute("SELECT * FROM tunnels")}
    assert ("fw1", "old-peer") not in left
    assert ("fw9", "other-site") in left
    # the answered openvpn/ipsec listings landed their rows normally
    assert ("fw1", "roadwarrior") in left and ("fw1", "site-c") in left


class _PfClientVpn(PfClient):
    """Adds a WireGuard VPN gateway and OpenVPN/IPsec status payloads."""

    def get(self, url, **kw):
        if url.endswith("status/gateways"):
            return PfResponse([
                {"name": "WAN_DHCP", "interface": "igc0", "status": "online",
                 "monitorip": "203.0.113.1", "loss": "0.0", "stddev": "1.2"},
                {"name": "WG_HOME_VPNV4", "interface": "tun_wg0",
                 "status": "online", "monitorip": "172.16.44.1"},
                {"name": "HOME_VPNV4", "interface": "ovpnc1", "status": "online"},
            ])
        if url.endswith("status/openvpn"):
            return PfResponse([{"name": "site-a", "conns": [{}, {}]}])
        if url.endswith("status/ipsec"):
            return PfResponse([{"con_id": "con1", "state": "ESTABLISHED",
                                "remote_host": "203.0.113.5", "version": 2}])
        return super().get(url, **kw)


def test_pfsense_vpn_gateways_and_status_become_tunnels(conn, clean_env, monkeypatch):
    """The WireGuard gateway (pfrest has no WG status endpoint) becomes
    tunnel health instead of being dropped; OpenVPN/IPsec come from their
    status endpoints; VPN gateways still stay out of the gateways table."""
    from patchbay.collectors.pfsense import PfsenseCollector
    monkeypatch.setattr(httpx, "Client", _PfClientVpn)
    PfsenseCollector().collect(_pf_settings(clean_env), conn)
    rows = {(r["type"], r["name"]): dict(r) for r in
            conn.execute("SELECT * FROM tunnels")}
    wg = rows[("wireguard", "WG_HOME_VPNV4")]
    assert wg["status"] == "up" and wg["interface"] == "tun_wg0"
    assert rows[("openvpn", "site-a")]["detail"] == "2 clients"
    assert rows[("ipsec", "con1")]["status"] == "up"
    gw_names = {r[0] for r in conn.execute("SELECT name FROM gateways")}
    assert "WG_HOME_VPNV4" not in gw_names and "HOME_VPNV4" not in gw_names
    assert "WAN_DHCP" in gw_names


def test_pfsense_absent_vpn_status_endpoints_keep_rows(conn, clean_env, monkeypatch):
    """Base stub 404s status/openvpn and status/ipsec: previously stored
    rows of those types survive the poll."""
    from patchbay.collectors.pfsense import PfsenseCollector
    conn.execute("INSERT INTO tunnels (device, type, name, status, source, "
                 "last_seen) VALUES ('fw2', 'openvpn', 'site-a', 'up', "
                 "'pfsense', ?)", (pdb.now(),))
    monkeypatch.setattr(httpx, "Client", PfClient)
    PfsenseCollector().collect(_pf_settings(clean_env), conn)
    assert conn.execute("SELECT COUNT(*) FROM tunnels WHERE type='openvpn'"
                        ).fetchone()[0] == 1


class _IpamAddresses(ShrinkingClient):
    """One documented address carrying a MAC and hostname."""

    def get(self, url, **kw):
        if url.endswith("/addresses/"):
            return FakeResponse([{"ip": "192.0.2.40", "hostname": "nas1.lan",
                                  "mac": "00:00:5e:00:53:40", "id": 9}])
        return super().get(url, **kw)


def test_phpipam_endpoints_are_identity_not_liveness(conn, clean_env, monkeypatch):
    """IPAM lends names; it never counts as a sighting. The old full-row
    upsert re-stamped last_seen every poll, so documented-but-gone hosts
    never aged out of the endpoint TTL and stale device attribution never
    cleared."""
    from patchbay.collectors.phpipam import PhpIpamCollector
    from patchbay.config import load_settings

    clean_env.setenv("IPAM_URL", "https://ipam.example/api")
    clean_env.setenv("IPAM_APP_ID", "app")
    clean_env.setenv("IPAM_TOKEN", "t")
    monkeypatch.setattr(httpx, "Client", _IpamAddresses)
    # a leftover doc-only row from the old behavior, and a real observation
    # that IPAM knows a name for
    pdb.upsert_endpoint(conn, mac="00:00:5e:00:53:99", source="phpipam",
                        ip="192.0.2.99")
    pdb.upsert_endpoint(conn, mac="00:00:5e:00:53:40", source="fdb")
    PhpIpamCollector().collect(load_settings(), conn)
    macs = {r[0] for r in conn.execute("SELECT mac FROM endpoints")}
    assert "00:00:5e:00:53:99" not in macs           # doc-only row retired
    row = conn.execute("SELECT hostname, source FROM endpoints "
                       "WHERE mac='00:00:5e:00:53:40'").fetchone()
    assert row["hostname"] == "nas1.lan"             # identity filled in
    assert row["source"] == "fdb"                    # the observer keeps the row


def test_unifi_clears_stale_ap_attribution(conn, clean_env, monkeypatch):
    """A client absent from the controller's current station list is no
    longer on any AP: the endpoint survives (ARP may still see it) but the
    AP attribution goes, so it stops counting as a wireless client."""
    from patchbay.collectors.unifi import UnifiCollector

    pdb.upsert_endpoint(conn, mac="00:00:5e:00:53:77", source="unifi",
                        ip="192.0.2.77", device="ap-floor1", interface="ssid-home")
    monkeypatch.setattr(httpx, "Client", _UnifiClient)
    UnifiCollector().collect(_unifi_settings(clean_env), conn)
    row = conn.execute("SELECT device, interface FROM endpoints "
                       "WHERE mac='00:00:5e:00:53:77'").fetchone()
    assert row is not None
    assert row["device"] is None and row["interface"] is None
