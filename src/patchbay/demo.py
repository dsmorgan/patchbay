"""Seed a fictional network into a fresh model, for demos and screenshots.

Nothing here describes a real site: names are invented, addresses come from
the RFC 5737/3849 documentation ranges, and every MAC is locally administered
(02:...). The seed writes the same raw evidence collectors would (devices,
interfaces, fdb, VLANs, subnets, config-parsed port membership, IPAM book,
gateways) and then runs the REAL normalizer over it — so inference, endpoint
placement, and link precedence are exercised, not faked.

What a viewer gets: a two-switch fabric with a firewall VM, two hypervisors
and their guests, three APs with wireless clients, an inferred unmanaged
switch, a declared cable to a NAS, a mirror port, a patch panel, IPAM drift
findings of every kind, and 24h of sine-wave rate history for the load view.
"""

from __future__ import annotations

import math
import random
import sqlite3

from . import db
from .normalize import normalize

MARKER = "demo_seed"          # app_state key marking a DB as demo-generated

VLANS = [(1, "mgmt"), (20, "servers"), (30, "iot"), (40, "wifi"), (103, "iscsi"),
         # the internet uplink: a VLAN with no subnet record, discovered as
         # the WAN rail from the default route's exit interface
         (199, "uplink")]
SUBNETS = [
    ("192.0.2.0/24", 1, "management"),
    ("198.51.100.0/24", 20, "servers"),
    ("203.0.113.0/24", 30, "iot devices"),
    ("2001:db8:0:20::/64", 20, "servers v6"),
    # storage network: unrouted on purpose — the routed view's isolated rail
    ("2001:db8:0:103::/64", 103, "iscsi storage"),
    # IPAM-style aggregate: nothing attaches inside it directly, so the
    # routed view's participation filter keeps it off the rails
    ("2001:db8::/32", None, "site aggregate"),
]

# (name, role, vendor, model, os, mgmt_ip, parent)
DEVICES = [
    ("fw1", "firewall", "Deciso", "DEC750", "opnsense 26.1", "192.0.2.1", "hyp1"),
    ("core1", "switch", "Exemplar", "ES-2400X", "exos 9.2", "192.0.2.2", None),
    ("edge1", "switch", "Exemplar", "ES-1200", "exos 9.2", "192.0.2.3", None),
    ("hyp1", "hypervisor", "Supermicro", "X12SPi-TF", "esxi 8.0.3", "192.0.2.10", None),
    ("hyp2", "hypervisor", "Supermicro", "X12SPi-TF", "esxi 8.0.3", "192.0.2.11", None),
    ("ap-attic", "ap", "UbiFi", "AP-6-Pro", "7.0.66", "192.0.2.31", None),
    ("ap-den", "ap", "UbiFi", "AP-6-Lite", "7.0.66", "192.0.2.32", None),
    ("ap-garage", "ap", "UbiFi", "AP-6-Lite", "7.0.66", "192.0.2.33", None),
    # multi-homed on purpose: the routed view's hardest cases (rim dots,
    # pass-under, attachment lines) need a NAS with three legs and
    # hypervisors with storage legs on the public demo (ADR-0002)
    ("nas1", "host", "BoxCo", "NS-424", "nasos 5.2", "192.0.2.21", None),
]

VMS = ["vm-web1", "vm-db1", "vm-git", "vm-media", "vm-backup", "vm-monitor"]

# core1 ports patched through the wall panel: description carries "[n]"
PANEL_RUNS = {1: "hyp1 vmnic0 [1]", 2: "hyp2 vmnic0 [2]", 3: "edge1 uplink [3]",
              5: "den wall jack [5]", 6: "attic AP [6]", 8: "garage AP [8]",
              10: "nas1 lan1 [10]", 12: "office drop [12]"}

LLDP = [  # (a_dev, a_if, b_dev, b_if)
    ("core1", "1/0/1", "hyp1", "vmnic0"),
    ("core1", "1/0/2", "hyp2", "vmnic0"),
    ("core1", "1/0/3", "edge1", "e1/0/1"),
    ("edge1", "e1/0/2", "ap-attic", "eth0"),
    ("edge1", "e1/0/3", "ap-den", "eth0"),
    ("edge1", "e1/0/4", "ap-garage", "eth0"),
]

DECLARED_LINKS = [("edge1", "e1/0/10", "nas1", "lan1")]

WIFI_CLIENTS = [  # (hostname, ip, ap, ssid, vlan)
    ("laptop-kay", "203.0.113.40", "ap-den", "HouseNet", 40),
    ("phone-sam", "203.0.113.41", "ap-den", "HouseNet", 40),
    ("tablet-liv", "203.0.113.42", "ap-attic", "HouseNet", 40),
    ("thermostat", "203.0.113.50", "ap-garage", "IoT", 30),
    ("doorbell", "203.0.113.51", "ap-garage", "IoT", 30),
]

# behind the unmanaged switch on edge1 e1/0/8 (5 MACs, no LLDP -> inferred)
BASEMENT = [("tv-basement", "203.0.113.60"), ("console", "203.0.113.61"),
            ("printer", "192.0.2.61"), ("pi-hole", "192.0.2.62"),
            ("shelly-plug", "203.0.113.62")]

IPAM = [  # (ip, hostname, state) — the "documented" side of drift
    ("192.0.2.1", "fw1", "used"), ("192.0.2.2", "core1", "used"),
    ("192.0.2.3", "edge1", "used"), ("192.0.2.10", "hyp1", "used"),
    ("192.0.2.11", "hyp2", "used"), ("192.0.2.21", "nas1", "used"),
    ("198.51.100.11", "vm-web1", "used"), ("198.51.100.12", "vm-db1", "used"),
    ("198.51.100.13", "vm-git", "used"),
    # drift: documented as web-staging, but the live host answers as vm-media
    ("198.51.100.14", "web-staging", "used"),
    # drift: reserved in IPAM yet a live endpoint uses it (decommission gone wrong)
    ("192.0.2.62", "old-scanner", "reserved"),
    ("203.0.113.50", "thermostat", "dhcp"), ("203.0.113.51", "doorbell", "dhcp"),
]


def _mac(n: int) -> str:
    return f"02:d3:{(n >> 24) & 255:02x}:{(n >> 16) & 255:02x}:{(n >> 8) & 255:02x}:{n & 255:02x}"


def seed(conn: sqlite3.Connection, *, rnd: random.Random | None = None) -> str:
    rnd = rnd or random.Random(11)   # deterministic: same demo every run
    db.init(conn)
    now = db.now()
    macs = iter(range(0x100, 0x2000, 7))
    mac_of: dict[str, str] = {}

    def mk_mac(key: str) -> str:
        mac_of.setdefault(key, _mac(next(macs)))
        return mac_of[key]

    ids: dict[str, int] = {}
    for name, role, vendor, model, os_, ip, parent in DEVICES:
        ids[name] = db.upsert_device(
            conn, name=name, source="demo", role=role, vendor=vendor,
            model=model, os=os_, mgmt_ip=ip, parent=parent, status="up")
    for i, vm in enumerate(VMS):
        vid = db.upsert_device(
            conn, name=vm, source="vsphere", role="vm", status="up",
            parent="hyp1" if i % 2 == 0 else "hyp2",
            mgmt_ip=f"198.51.100.{11 + i}", os="debian 13")
        db.upsert_interface(conn, device_id=vid, name="Network adapter 1",
                            mac=mk_mac(vm), oper_status="up")

    for vid_, vname in VLANS:
        conn.execute("INSERT OR REPLACE INTO vlans (vid, name, source, last_seen) "
                     "VALUES (?, ?, 'demo', ?)", (vid_, vname, now))
    for cidr, vid_, descr in SUBNETS:
        db.upsert_subnet(conn, cidr=cidr, source="phpipam", vlan=vid_, description=descr)

    # firewall interfaces: routed view of VLAN membership (one leg per subnet)
    for iface, ip in (("vmx0", "100.64.10.2"), ("vmx1", "192.0.2.1"),
                      ("vmx2", "198.51.100.1"), ("vmx3", "203.0.113.1")):
        db.upsert_interface(conn, device_id=ids["fw1"], name=iface, ip=ip,
                            ip6="2001:db8:0:20::1" if iface == "vmx2" else None,
                            mac=mk_mac(f"fw1:{iface}"), oper_status="up",
                            speed_bps=10_000_000_000)
    # the guest can't see its own tag — the hypervisor's port group can
    conn.execute("INSERT OR REPLACE INTO vnic_vlans (mac, vid, portgroup, source) "
                 "VALUES (?, 20, 'pg-servers', 'vsphere')", (mac_of["fw1:vmx2"],))
    # WAN: vmx0 is an untagged member of the uplink VLAN, and the default
    # route leaves on it — that's how the routed view finds the uplink rail
    conn.execute("INSERT OR REPLACE INTO port_vlans VALUES "
                 "('fw1', 'vmx0', 199, 0, 'config')")
    for dest, gw, proto in (("0.0.0.0/0", "100.64.10.1", "ipv4"),
                            ("::/0", "fe80::1%vmx0", "ipv6")):
        conn.execute("INSERT OR REPLACE INTO routes (device, destination, "
                     "gateway, interface, proto, flags, source, last_seen) "
                     "VALUES ('fw1', ?, ?, 'vmx0', ?, 'UGS', 'opnsense', ?)",
                     (dest, gw, proto, now))
    # a WireGuard tunnel to a second site (#42): the tunnel node draws
    # beside the internet cloud, and the site-b subnet hangs off it via
    # the route through wg1
    db.save_tunnels(conn, device="fw1", source="opnsense", type_="wireguard",
                    rows=[{"name": "site-b · wg-peer", "peer": "192.0.2.200:51820",
                           "interface": "wg1", "status": "up",
                           "last_handshake": now - 45,
                           "detail": "allowed 172.16.44.0/24"}])
    conn.execute("INSERT OR REPLACE INTO routes (device, destination, "
                 "gateway, interface, proto, flags, source, last_seen) "
                 "VALUES ('fw1', '172.16.44.0/24', NULL, 'wg1', 'ipv4', "
                 "'UGS', 'opnsense', ?)", (now,))

    # switches: full port complement, descriptions carry the panel tags
    for dev, prefix, count, speed in (("core1", "1/0/", 24, 10_000_000_000),
                                      ("edge1", "e1/0/", 12, 2_500_000_000)):
        for n in range(1, count + 1):
            iface = f"{prefix}{n}"
            up = any(l[0] == dev and l[1] == iface or l[2] == dev and l[3] == iface
                     for l in LLDP) or (dev, iface) in {("edge1", "e1/0/8"),
                                                        ("edge1", "e1/0/10"),
                                                        ("core1", "1/0/24")}
            db.upsert_interface(
                conn, device_id=ids[dev], name=iface, mac=mk_mac(f"{dev}:{iface}"),
                oper_status="up" if up else "down",
                speed_bps=speed if up else None,
                description=PANEL_RUNS.get(n) if dev == "core1" else None)
    # hypervisor NICs + vmkernel; vmk1 is the storage leg (multi-homed on
    # the routed view, home = the faster storage network)
    for i, hyp in enumerate(("hyp1", "hyp2")):
        for iface in ("vmnic0", "vmnic1", "vmk0", "vmk1"):
            ip = (f"192.0.2.{10 + i}" if iface == "vmk0" else None)
            ip6 = (f"2001:db8:0:103::{10 + i}" if iface == "vmk1" else None)
            db.upsert_interface(conn, device_id=ids[hyp], name=iface,
                                mac=mk_mac(f"{hyp}:{iface}"),
                                oper_status="up" if iface != "vmnic1" else "down",
                                ip=ip, ip6=ip6,
                                speed_bps=10_000_000_000 if iface.startswith("vmnic") else None)
    # nas1: three legs — mgmt, servers (fast, its home rail), and storage
    for iface, ip, ip6, speed in (
            ("lan1", "192.0.2.21", None, 1_000_000_000),
            ("lan2", "198.51.100.21", None, 10_000_000_000),
            ("lan3", None, "2001:db8:0:103::21", 10_000_000_000)):
        db.upsert_interface(conn, device_id=ids["nas1"], name=iface,
                            mac=mk_mac(f"nas1:{iface}"), oper_status="up",
                            ip=ip, ip6=ip6, speed_bps=speed)
    for ap, *_ in [d for d in DEVICES if d[1] == "ap"]:
        db.upsert_interface(conn, device_id=ids[ap], name="eth0", mac=mk_mac(f"{ap}:eth0"),
                            oper_status="up", speed_bps=2_500_000_000,
                            description="uplink")

    # config-parsed VLAN membership: trunks between fabric, access at the edge
    trunk_ports = [("core1", "1/0/1"), ("core1", "1/0/2"), ("core1", "1/0/3"),
                   ("edge1", "e1/0/1")]
    for dev, iface in trunk_ports:
        for vid_, _ in VLANS:
            conn.execute("INSERT OR REPLACE INTO port_vlans VALUES (?, ?, ?, ?, 'config')",
                         (dev, iface, vid_, 0 if vid_ == 1 else 1))
    for dev, iface, vid_ in (("edge1", "e1/0/2", 40), ("edge1", "e1/0/3", 40),
                             ("edge1", "e1/0/4", 40), ("edge1", "e1/0/8", 30),
                             ("edge1", "e1/0/10", 20), ("core1", "1/0/12", 1)):
        conn.execute("INSERT OR REPLACE INTO port_vlans VALUES (?, ?, ?, 0, 'config')",
                     (dev, iface, vid_))
    # the mirror port: configured as a SPAN destination, cabled to a probe
    conn.execute("INSERT OR REPLACE INTO port_roles VALUES "
                 "('core1', '1/0/24', 'monitor-dst', 'session 1 mirrors 1/0/1', 'config')")
    conn.execute("INSERT OR REPLACE INTO port_roles VALUES "
                 "('core1', '1/0/1', 'monitor-src', 'session 1 mirrored to 1/0/24', 'config')")

    for a_dev, a_if, b_dev, b_if in LLDP:
        db.upsert_link(conn, a_device=a_dev, a_interface=a_if,
                       b_device=b_dev, b_interface=b_if, source="lldp")
    db.upsert_link(conn, a_device="hyp2", a_interface="vmnic1",
                   b_device="core1", b_interface="1/0/4", source="vsphere-hint")

    # switch learning tables: VMs behind their hypervisor uplinks, the
    # basement crowd behind one quiet port (5 MACs + no LLDP -> inferred
    # unmanaged switch), wired NAS behind its declared port
    for i, vm in enumerate(VMS):
        port = "1/0/1" if i % 2 == 0 else "1/0/2"
        conn.execute("INSERT OR REPLACE INTO fdb VALUES ('core1', ?, ?, 'demo')",
                     (port, mac_of[vm]))
    for host, _ip in BASEMENT:
        conn.execute("INSERT OR REPLACE INTO fdb VALUES ('edge1', 'e1/0/8', ?, 'demo')",
                     (mk_mac(host),))
    conn.execute("INSERT OR REPLACE INTO fdb VALUES ('edge1', 'e1/0/10', ?, 'demo')",
                 (mk_mac("nas1"),))

    # address books: live endpoints (ARP/leases/wifi) + the documented IPAM side
    for host, ip in BASEMENT:
        db.upsert_endpoint(conn, mac=mk_mac(host), source="opnsense",
                           ip=ip, hostname=host)
    db.upsert_endpoint(conn, mac=mk_mac("nas1"), source="opnsense",
                       ip="192.0.2.21", hostname="nas1")
    for i, vm in enumerate(VMS):
        db.upsert_endpoint(conn, mac=mac_of[vm], source="opnsense",
                           ip=f"198.51.100.{11 + i}", hostname=vm)
    for host, ip, ap, ssid, vlan in WIFI_CLIENTS:
        db.upsert_endpoint(conn, mac=mk_mac(host), source="unifi", ip=ip,
                           hostname=host, device=ap, interface=ssid, vlan=vlan)
    # drift: a live device nobody documented, and one answering from outside
    # every known subnet
    db.upsert_endpoint(conn, mac=mk_mac("mystery"), source="opnsense",
                       ip="192.0.2.77", hostname="esp32-a1b2c3")
    db.upsert_endpoint(conn, mac=mk_mac("stray"), source="opnsense",
                       ip="198.18.0.9", hostname="lab-bench")
    for ip, host, state in IPAM:
        conn.execute(
            "INSERT OR REPLACE INTO ipam_addresses (ip, hostname, state, last_seen) "
            "VALUES (?, ?, ?, ?)", (ip, host, state, now))

    conn.execute("INSERT OR REPLACE INTO gateways "
                 "(name, address, status, loss, delay, source, last_seen) "
                 "VALUES ('WAN_GW', '100.64.10.1', 'Online', '0.0 %', "
                 "'8.1 ms', 'opnsense', ?)", (now,))

    # 24h of samples for every live link end: the load view's peak column.
    # A sine day (quiet at night, busy evenings) + jitter reads believably.
    live_ports = [(l[0], l[1]) for l in LLDP] + [(l[2], l[3]) for l in LLDP]
    conn.executemany(
        "INSERT INTO rate_history (device, interface, ts, in_bps, out_bps) "
        "VALUES (?, ?, ?, ?, ?)",
        [(dev, iface, now - s * 300,
          int(cap * load * rnd.uniform(0.6, 1.0)),
          int(cap * load * rnd.uniform(0.2, 0.5)))
         for dev, iface in live_ports
         for cap in [2_500_000_000 if dev.startswith(("ap", "edge")) else 10_000_000_000]
         for s in range(288)
         for load in [0.04 + 0.3 * max(0.0, math.sin((now - s * 300) % 86400
                                                     / 86400 * math.pi))]])
    conn.executemany(
        "UPDATE interfaces SET in_bps = ?, out_bps = ? "
        "WHERE name = ? AND device_id = (SELECT id FROM devices WHERE name = ?)",
        [(int(2e8 * rnd.uniform(0.2, 1.5)), int(8e7 * rnd.uniform(0.2, 1.5)),
          iface, dev) for dev, iface in live_ports])

    for src in ("librenms", "oxidized", "phpipam", "unifi", "opnsense", "vsphere"):
        db.save_raw(conn, source=src, endpoint="demo", payload={"demo": True})

    # the real engine derives the rest: endpoint placement from fdb, the
    # unmanaged-switch inference, declared links, guest VLAN resolution
    summary = normalize(conn, declared_links=DECLARED_LINKS)
    db.save_last_poll(conn, [
        "[ok]   demo: fictional network seeded — nothing here is real",
        f"[ok]   normalize: {summary}"])
    db.set_state(conn, MARKER, "1")
    return summary
