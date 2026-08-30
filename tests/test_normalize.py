"""Normalizer invariants — every case here is a bug that actually happened
(or was caught in review) at least once."""

import json
import time

from patchbay import db as pdb
from patchbay.normalize import (EVIDENCE_TTL, _expire_stale_evidence,
                                canonical_name, normalize)

NOW = time.time()


def dev(conn, name, source, last_seen=None, **fields):
    rid = pdb.upsert_device(conn, name=name, source=source, **fields)
    if last_seen is not None:
        conn.execute("UPDATE devices SET last_seen=? WHERE id=?", (last_seen, rid))
    return rid


def get_dev(conn, name):
    return conn.execute("SELECT * FROM devices WHERE name=?", (name,)).fetchone()


def links(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT a_device, a_interface, b_device, b_interface, source FROM links "
        "ORDER BY a_device, a_interface, source")]


def test_canonical_name():
    assert canonical_name("SW1.example.lan.") == "sw1"
    assert canonical_name("  host ") == "host"


def test_merge_freshest_row_wins_ties(conn):
    # same source, FQDN duplicate recreated each poll: the fresh row's facts
    # must win — the stable sort used to hand the oldest row every conflict
    dev(conn, "sw1", "librenms", last_seen=NOW - 900, mgmt_ip="192.0.2.1")
    dev(conn, "sw1.example.lan", "librenms", last_seen=NOW, mgmt_ip="192.0.2.99")
    normalize(conn)
    d = get_dev(conn, "sw1")
    assert d["mgmt_ip"] == "192.0.2.99"
    assert d["last_seen"] >= NOW - 1


def test_merge_junk_and_versioned_os(conn):
    dev(conn, "fw1", "librenms", last_seen=NOW, os="freebsd", vendor="amd64")
    dev(conn, "fw1.example.lan", "opnsense", last_seen=NOW,
        os="opnsense 26.1.2", vendor="OPNsense")
    normalize(conn)
    d = get_dev(conn, "fw1")
    assert d["os"] == "opnsense 26.1.2"
    assert d["vendor"] == "OPNsense"  # arch junk never outranks a real vendor


def test_merge_network_role_beats_vm(conn):
    dev(conn, "fw1", "opnsense", last_seen=NOW, role="firewall")
    dev(conn, "fw1.example.lan", "vsphere", last_seen=NOW, role="vm", parent="hyp1")
    normalize(conn)
    d = get_dev(conn, "fw1")
    assert d["role"] == "firewall"
    assert d["parent"] == "hyp1"  # placement still folds in


def test_merge_freshest_status_wins(conn):
    dev(conn, "hyp1", "librenms", last_seen=NOW - 3000, status="down")
    dev(conn, "hyp1.example.lan", "vsphere", last_seen=NOW, status="up")
    normalize(conn)
    assert get_dev(conn, "hyp1")["status"] == "up"


def test_rename_rewrites_fdb(conn):
    # fdb rows keyed by a raw sysName used to survive the rename and poison
    # inference (phantom unmanaged switches, alias<->canonical self-links)
    dev(conn, "sw1.example.lan", "librenms", last_seen=NOW, role="switch")
    for i in range(3):
        conn.execute("INSERT INTO fdb (device, interface, mac) VALUES (?, ?, ?)",
                     ("sw1.example.lan", "1/0/24", f"02:00:00:00:00:0{i}"))
    normalize(conn)
    devs = {r[0] for r in conn.execute("SELECT DISTINCT device FROM fdb")}
    assert devs == {"sw1"}
    # and the inference that runs on it names the canonical device
    assert get_dev(conn, "unmanaged@sw1:1/0/24") is not None


def test_alias_chain_resolves_transitively(conn):
    conn.execute("INSERT INTO aliases VALUES ('oldest', 'mid')")
    conn.execute("INSERT INTO aliases VALUES ('mid', 'sw1')")
    dev(conn, "sw1", "librenms", last_seen=NOW, role="switch")
    pdb.upsert_link(conn, a_device="oldest", a_interface="1", b_device="x",
                    b_interface="2", source="lldp")
    normalize(conn)
    assert all(r[0] != "oldest" and r[0] != "mid" for r in links(conn))
    assert ("sw1", "1", "x", "2", "lldp") in links(conn)


def test_question_mark_is_not_a_port_identity(conn):
    # an lldp link with a '?' far end on hyp1 must NOT suppress an fdb-uplink
    # on a *different* switch port that also ends at (hyp1, '?')
    for n, r in (("sw-a", "switch"), ("sw-b", "switch"), ("hyp1", "hypervisor")):
        dev(conn, n, "librenms", last_seen=NOW, role=r)
    pdb.upsert_link(conn, a_device="sw-a", a_interface="p1", b_device="hyp1",
                    b_interface="?", source="lldp")
    pdb.upsert_link(conn, a_device="sw-b", a_interface="p7", b_device="hyp1",
                    b_interface="?", source="fdb-uplink")
    normalize(conn)
    assert ("hyp1", "?", "sw-b", "p7", "fdb-uplink") in links(conn)
    # but a real same-port supersede still fires
    pdb.upsert_link(conn, a_device="sw-a", a_interface="p1", b_device="hyp1",
                    b_interface="?", source="fdb-uplink")
    normalize(conn)
    assert ("hyp1", "?", "sw-a", "p1", "fdb-uplink") not in links(conn)


def test_evidence_expires_but_declared_does_not(conn):
    dev(conn, "sw1", "librenms", last_seen=NOW, role="switch")
    stale = NOW - EVIDENCE_TTL - 60
    conn.execute("INSERT INTO links (a_device, a_interface, b_device, b_interface, "
                 "source, last_seen) VALUES ('sw1', 'p1', 'gone', 'e0', 'lldp', ?)",
                 (stale,))
    conn.execute("INSERT INTO links (a_device, a_interface, b_device, b_interface, "
                 "source, last_seen) VALUES ('sw1', 'p2', 'kept', 'e0', 'declared', ?)",
                 (stale,))
    dropped = _expire_stale_evidence(conn)
    assert dropped == 1
    assert [r[4] for r in links(conn)] == ["declared"]


def test_stale_inferred_switch_expires(conn):
    stale = NOW - EVIDENCE_TTL - 60
    rid = dev(conn, "unmanaged@sw1:1/0/9", "inference", last_seen=stale,
              role="unmanaged-switch")
    conn.execute("INSERT INTO links (a_device, a_interface, b_device, b_interface, "
                 "source, last_seen) VALUES ('sw1', '1/0/9', 'unmanaged@sw1:1/0/9', "
                 "'?', 'fdb-inference', ?)", (stale,))
    _expire_stale_evidence(conn)
    assert get_dev(conn, "unmanaged@sw1:1/0/9") is None
    assert links(conn) == []


def test_declared_links_prune_when_undeclared(conn):
    dev(conn, "sw1", "librenms", last_seen=NOW, role="switch")
    normalize(conn, declared_links=[("sw1", "p1", "nas", "lan1")])
    assert ("nas", "lan1", "sw1", "p1", "declared") in links(conn)
    normalize(conn, declared_links=[])
    assert links(conn) == []


def test_fuse_halves_into_one_cable(conn):
    dev(conn, "sw1", "librenms", last_seen=NOW, role="switch")
    dev(conn, "sw2", "librenms", last_seen=NOW, role="switch")
    pdb.upsert_link(conn, a_device="sw1", a_interface="p1", b_device="sw2",
                    b_interface="?", source="lldp")
    pdb.upsert_link(conn, a_device="sw1", a_interface="?", b_device="sw2",
                    b_interface="p9", source="lldp")
    normalize(conn)
    assert links(conn) == [("sw1", "p1", "sw2", "p9", "lldp")]


def test_flood_guard_drops_nonswitch_extras(conn):
    for n, r in (("sw1", "switch"), ("sw2", "switch"), ("hyp1", "hypervisor")):
        dev(conn, n, "librenms", last_seen=NOW, role=r)
    pdb.upsert_link(conn, a_device="sw1", a_interface="p1", b_device="sw2",
                    b_interface="p2", source="lldp")
    pdb.upsert_link(conn, a_device="sw1", a_interface="p1", b_device="hyp1",
                    b_interface="vmnic2", source="lldp")  # flooded artifact
    normalize(conn)
    ls = links(conn)
    assert ("sw1", "p1", "sw2", "p2", "lldp") in ls
    assert all("hyp1" not in (a, b) for a, _, b, _, _ in ls)


def test_inferred_switch_retired_when_port_gains_real_link(conn):
    # a hypervisor's VM MACs, briefly ownerless, spawn a ghost unmanaged
    # switch on the switch port; once the vsphere-hint uplink appears on that
    # same port the ghost must be retired, not linger until the TTL
    for n, r in (("core1", "switch"), ("vmhost1", "hypervisor")):
        dev(conn, n, "librenms", last_seen=NOW, role=r)
    dev(conn, "unmanaged@core1:1/0/3", "inference", last_seen=NOW,
        role="unmanaged-switch")
    conn.execute("INSERT INTO links (a_device, a_interface, b_device, b_interface, "
                 "source, last_seen) VALUES ('core1', '1/0/3', "
                 "'unmanaged@core1:1/0/3', '?', 'fdb-inference', ?)", (NOW,))
    for i in range(3):
        conn.execute("INSERT INTO endpoints (mac, source, device, interface) "
                     "VALUES (?, 'fdb', 'unmanaged@core1:1/0/3', '?')",
                     (f"00:50:56:00:00:0{i}",))
    # vmhost1's uplink lands on the same port
    pdb.upsert_link(conn, a_device="vmhost1", a_interface="vmnic6",
                    b_device="core1", b_interface="1/0/3", source="vsphere-hint")
    normalize(conn)
    assert get_dev(conn, "unmanaged@core1:1/0/3") is None
    assert not any(a == "unmanaged@core1:1/0/3" or b == "unmanaged@core1:1/0/3"
                   for a, _, b, _, _ in links(conn))
    assert ("core1", "1/0/3", "vmhost1", "vmnic6", "vsphere-hint") in links(conn)


def test_declared_unmanaged_survives_real_link_on_port(conn):
    # an operator-DECLARED unmanaged switch is authoritative — a real link on
    # the port must not retire it
    dev(conn, "sw1", "librenms", last_seen=NOW, role="switch")
    normalize(conn, declared_unmanaged=[(None, "sw1", "1/0/5")])
    assert get_dev(conn, "unmanaged@sw1:1/0/5") is not None
    pdb.upsert_link(conn, a_device="sw1", a_interface="1/0/5", b_device="host",
                    b_interface="eth0", source="lldp")
    normalize(conn, declared_unmanaged=[(None, "sw1", "1/0/5")])
    assert get_dev(conn, "unmanaged@sw1:1/0/5") is not None  # declared, kept


def test_kernel_port_macs_identify_the_host_not_a_ghost_switch(conn):
    # ESXi vmkernel MACs land in a switch's FDB. They must resolve to the HOST
    # (one cable, far end unknown — no cable ends on a vSwitch port), never
    # spawn an unmanaged switch, and never appear as a link end themselves.
    dev(conn, "core1", "librenms", last_seen=NOW, role="switch")
    hid = dev(conn, "vmhost1", "vsphere", last_seen=NOW, role="hypervisor")
    for name, mac in (("vmk0", "00:50:56:00:00:01"), ("vmk1", "00:50:56:00:00:02"),
                      ("vmk2", "00:50:56:00:00:03")):
        pdb.upsert_interface(conn, device_id=hid, name=name, mac=mac)
    for mac in ("00:50:56:00:00:01", "00:50:56:00:00:02", "00:50:56:00:00:03"):
        conn.execute("INSERT INTO fdb (device, interface, mac) VALUES ('core1', '1/0/3', ?)",
                     (mac,))
    normalize(conn)
    ls = links(conn)
    assert ls == [("core1", "1/0/3", "vmhost1", "?", "fdb-uplink")], ls
    assert get_dev(conn, "unmanaged@core1:1/0/3") is None


def test_physical_interface_wins_a_shared_mac(conn):
    # ESXi gives vmk0 its backing pnic's MAC; the cable ends on the pnic, and
    # the winner must not depend on row order
    dev(conn, "edge1", "librenms", last_seen=NOW, role="switch")
    hid = dev(conn, "vmhost1", "vsphere", last_seen=NOW, role="hypervisor")
    shared = "00:25:90:00:00:03"
    pdb.upsert_interface(conn, device_id=hid, name="vmk0", mac=shared)
    pdb.upsert_interface(conn, device_id=hid, name="vmnic4", mac=shared)
    conn.execute("INSERT INTO fdb (device, interface, mac) VALUES ('edge1', 'e1/1/26', ?)",
                 (shared,))
    normalize(conn)
    assert ("edge1", "e1/1/26", "vmhost1", "vmnic4", "fdb-uplink") in links(conn)


def test_kernel_link_ends_are_demoted_and_deduped(conn):
    # rows written before this rule (or by a future collector) get healed:
    # both vmk-named ends collapse onto the single '?' cable
    dev(conn, "core1", "librenms", last_seen=NOW, role="switch")
    dev(conn, "vmhost1", "vsphere", last_seen=NOW, role="hypervisor")
    for iface in ("vmk1", "vmk2", "?"):
        conn.execute(
            "INSERT INTO links (a_device, a_interface, b_device, b_interface, "
            "source, last_seen) VALUES ('vmhost1', ?, 'core1', '1/0/3', 'fdb-uplink', ?)",
            (iface, NOW))
    from patchbay.normalize import _demote_kernel_link_ends
    _demote_kernel_link_ends(conn)
    # rows went in through raw SQL, so they keep the side order given here;
    # only upsert_link direction-normalizes
    assert links(conn) == [("vmhost1", "?", "core1", "1/0/3", "fdb-uplink")]


def test_evidence_survives_while_a_device_is_down(conn):
    # a homelab host that is normally powered off (noisy backup server) must
    # keep its place on the map: its silence is explained, so the cable is
    # remembered rather than aged out
    dev(conn, "core1", "librenms", last_seen=NOW, role="switch", status="up")
    dev(conn, "vmhost1", "vsphere", last_seen=NOW, role="hypervisor",
        status="notResponding")
    dev(conn, "sw2", "librenms", last_seen=NOW, role="switch", status="up")
    stale = NOW - EVIDENCE_TTL - 60
    conn.execute("INSERT INTO links (a_device, a_interface, b_device, b_interface, "
                 "source, last_seen) VALUES ('vmhost1', '?', 'core1', '1/0/3', "
                 "'fdb-uplink', ?)", (stale,))
    # ...while stale evidence between two LIVE devices still expires
    conn.execute("INSERT INTO links (a_device, a_interface, b_device, b_interface, "
                 "source, last_seen) VALUES ('core1', 'p9', 'sw2', 'p1', 'lldp', ?)",
                 (stale,))
    dropped = _expire_stale_evidence(conn)
    remaining = [(a, b) for a, _, b, _, _ in links(conn)]
    assert ("vmhost1", "core1") in remaining
    assert ("core1", "sw2") not in remaining
    assert dropped == 1


def test_alias_rewrite_keeps_the_fresh_timestamp(conn):
    # librenms names a host by FQDN, so every poll re-inserts the alias row;
    # renaming it onto the canonical row must carry the fresh sighting over,
    # or the survivor ages out while the link is being reported the whole time
    dev(conn, "edge1", "librenms", last_seen=NOW, role="switch")
    dev(conn, "vmhost2", "vsphere", last_seen=NOW, role="hypervisor", status="up")
    conn.execute("INSERT INTO aliases VALUES ('vmhost2.example.lan', 'vmhost2')")
    stale = NOW - 3600
    conn.execute("INSERT INTO links (a_device, a_interface, b_device, b_interface, "
                 "source, last_seen) VALUES ('edge1', 'p1', 'vmhost2', 'vmnic4', 'lldp', ?)",
                 (stale,))
    pdb.upsert_link(conn, a_device="edge1", a_interface="p1",
                    b_device="vmhost2.example.lan", b_interface="vmnic4", source="lldp")
    normalize(conn)
    rows = conn.execute("SELECT last_seen FROM links WHERE b_device='vmhost2'").fetchall()
    assert len(rows) == 1
    assert rows[0]["last_seen"] > stale, "canonical row kept the stale timestamp"


def test_down_port_mac_residue_is_not_evidence(conn):
    # a MAC table on a down port is residue that hasn't aged out; inferring
    # from it invents links (and switches) for cables nothing can see
    sid = dev(conn, "sw1", "librenms", last_seen=NOW, role="switch")
    pdb.upsert_interface(conn, device_id=sid, name="p1", oper_status="down")
    for i in range(4):
        conn.execute("INSERT INTO fdb (device, interface, mac) VALUES ('sw1', 'p1', ?)",
                     (f"02:00:00:00:00:0{i}",))
    normalize(conn)
    assert get_dev(conn, "unmanaged@sw1:p1") is None
    assert links(conn) == []


def test_intermittent_hint_outlives_the_default_ttl(conn):
    # vsphere-hint is only reported for ~60s after each CDP advertisement
    dev(conn, "core1", "librenms", last_seen=NOW, role="switch", status="up")
    dev(conn, "vmhost2", "vsphere", last_seen=NOW, role="hypervisor", status="up")
    conn.execute("INSERT INTO links (a_device, a_interface, b_device, b_interface, "
                 "source, last_seen) VALUES ('vmhost2', 'vmnic2', 'core1', '1/0/4', "
                 "'vsphere-hint', ?)", (NOW - EVIDENCE_TTL - 600,))
    _expire_stale_evidence(conn)
    assert len(links(conn)) == 1, "an intermittent hint was expired too early"


def test_declared_and_discovered_same_cable_is_one_edge(conn):
    # declaring a cable that LLDP also reports must not double the edge; the
    # live report wins while it lasts, and the declaration returns when it stops
    dev(conn, "edge1", "librenms", last_seen=NOW, role="switch", status="up")
    dev(conn, "vmhost1", "vsphere", last_seen=NOW, role="hypervisor", status="up")
    pdb.upsert_link(conn, a_device="edge1", a_interface="e1/1/26",
                    b_device="vmhost1", b_interface="vmnic4", source="lldp")
    decl = [("edge1", "e1/1/26", "vmhost1", "vmnic4")]
    normalize(conn, declared_links=decl)
    assert links(conn) == [("edge1", "e1/1/26", "vmhost1", "vmnic4", "lldp")]
    # host powers off, LLDP stops being reported and ages out -> declaration
    # carries the cable on its own
    conn.execute("DELETE FROM links WHERE source='lldp'")
    normalize(conn, declared_links=decl)
    assert links(conn) == [("edge1", "e1/1/26", "vmhost1", "vmnic4", "declared")]


def test_discovery_beats_a_stale_declaration_and_reports_it(conn):
    # a port carries one cable: when live discovery contradicts a declaration,
    # the observation wins and the operator is told their .env is out of date
    for n in ("core1", "edge1"):
        dev(conn, n, "librenms", last_seen=NOW, role="switch", status="up")
    dev(conn, "vmhost1", "vsphere", last_seen=NOW, role="hypervisor", status="up")
    dev(conn, "vmhost2", "vsphere", last_seen=NOW, role="hypervisor", status="up")
    pdb.upsert_link(conn, a_device="core1", a_interface="1/0/14",
                    b_device="vmhost2", b_interface="vmnic5", source="lldp")
    normalize(conn, declared_links=[("core1", "1/0/14", "vmhost1", "vmnic3")])
    ls = links(conn)
    assert ("core1", "1/0/14", "vmhost2", "vmnic5", "lldp") in ls
    assert not any(s == "declared" for *_, s in ls), "stale declaration survived"
    notes = json.loads(pdb.get_state(conn, "declaration_conflicts"))
    assert any("vmhost2" in n and "1/0/14" in n for n in notes), notes


def test_port_description_disagreement_is_reported_not_enforced(conn):
    # a description can be the stale half, so nothing is dropped — but say so
    sid = dev(conn, "core1", "librenms", last_seen=NOW, role="switch", status="up")
    dev(conn, "vmhost1", "vsphere", last_seen=NOW, role="hypervisor", status="up")
    dev(conn, "vmhost2", "vsphere", last_seen=NOW, role="hypervisor", status="up")
    pdb.upsert_interface(conn, device_id=sid, name="1/0/14",
                         description="vmhost2 vmnic5", oper_status="up")
    normalize(conn, declared_links=[("core1", "1/0/14", "vmhost1", "vmnic3")])
    assert ("core1", "1/0/14", "vmhost1", "vmnic3", "declared") in links(conn)
    notes = json.loads(pdb.get_state(conn, "declaration_conflicts"))
    assert any("port description" in n for n in notes), notes


def test_agreeing_declaration_and_description_are_quiet(conn):
    sid = dev(conn, "core1", "librenms", last_seen=NOW, role="switch", status="up")
    dev(conn, "vmhost1", "vsphere", last_seen=NOW, role="hypervisor", status="up")
    pdb.upsert_interface(conn, device_id=sid, name="1/0/3",
                         description="vmhost1 vmnic6", oper_status="up")
    normalize(conn, declared_links=[("core1", "1/0/3", "vmhost1", "vmnic6")])
    assert json.loads(pdb.get_state(conn, "declaration_conflicts")) == []


def test_mirror_destination_macs_are_not_evidence(conn):
    # a SPAN destination transmits copies of other ports' traffic. Every MAC
    # the switch reports there belongs somewhere else, so inferring from them
    # hangs a phantom switch off the probe port.
    sid = dev(conn, "sw1", "librenms", last_seen=NOW, role="switch", status="up")
    pdb.upsert_interface(conn, device_id=sid, name="1/0/14", oper_status="up")
    conn.execute("INSERT INTO port_roles (device, interface, role, detail, source) "
                 "VALUES ('sw1', '1/0/14', 'monitor-dst', 'session 1 mirrors vlan 1', "
                 "'oxidized')")
    for i in range(6):
        conn.execute("INSERT INTO fdb (device, interface, mac) VALUES "
                     "('sw1', '1/0/14', ?)", (f"02:00:00:00:00:0{i}",))
    normalize(conn)
    assert get_dev(conn, "unmanaged@sw1:1/0/14") is None
    assert links(conn) == []


def test_mirror_destination_still_takes_a_declaration(conn):
    # nothing can discover what a probe port is plugged into, so the operator's
    # declaration is the only possible answer and must survive
    sid = dev(conn, "sw1", "librenms", last_seen=NOW, role="switch", status="up")
    dev(conn, "hyp1", "vsphere", last_seen=NOW, role="hypervisor", status="up")
    pdb.upsert_interface(conn, device_id=sid, name="1/0/14", oper_status="up")
    conn.execute("INSERT INTO port_roles (device, interface, role, detail, source) "
                 "VALUES ('sw1', '1/0/14', 'monitor-dst', 'session 1', 'oxidized')")
    normalize(conn, declared_links=[("sw1", "1/0/14", "hyp1", "vmnic5")])
    assert links(conn) == [("hyp1", "vmnic5", "sw1", "1/0/14", "declared")]
    assert json.loads(pdb.get_state(conn, "declaration_conflicts")) == []


def test_portgroup_vlan_reaches_the_guest_interface(conn):
    # a firewall VM can't see its own 802.1Q tags — the vSwitch adds and strips
    # them. The hypervisor knows, and the MAC joins the two views.
    fid = dev(conn, "fw1", "opnsense", last_seen=NOW, role="firewall", status="up")
    pdb.upsert_interface(conn, device_id=fid, name="vmx1", mac="00:50:56:00:00:01")
    pdb.upsert_interface(conn, device_id=fid, name="vmx2", mac="00:50:56:00:00:02")
    conn.execute("INSERT INTO vnic_vlans (mac, vid, portgroup, source) VALUES "
                 "('00:50:56:00:00:01', 299, 'Xternal 299', 'vsphere')")
    conn.execute("INSERT INTO vnic_vlans (mac, vid, portgroup, source) VALUES "
                 "('00:50:56:00:00:02', 4095, 'Trunk', 'vsphere')")
    conn.execute("INSERT INTO vlans (vid, source) VALUES (13, 'x'), (24, 'x')")
    normalize(conn)
    rows = {(r["interface"], r["vid"], r["tagged"]) for r in conn.execute(
        "SELECT interface, vid, tagged FROM port_vlans WHERE device='fw1'")}
    assert ("vmx1", 299, 0) in rows           # VST: the vSwitch tags for it
    assert ("vmx2", 13, 1) in rows and ("vmx2", 24, 1) in rows   # VGT trunk


def test_config_vlans_outrank_the_hypervisor(conn):
    # where a switch's own config describes the port, that wins; the port group
    # only fills silence
    sid = dev(conn, "sw1", "librenms", last_seen=NOW, role="switch", status="up")
    pdb.upsert_interface(conn, device_id=sid, name="1/0/1", mac="00:50:56:00:00:09")
    conn.execute("INSERT INTO port_vlans (device, interface, vid, tagged, source) "
                 "VALUES ('sw1', '1/0/1', 73, 0, 'oxidized')")
    conn.execute("INSERT INTO vnic_vlans (mac, vid, portgroup, source) VALUES "
                 "('00:50:56:00:00:09', 299, 'pg', 'vsphere')")
    normalize(conn)
    assert [tuple(r) for r in conn.execute(
        "SELECT vid, source FROM port_vlans WHERE device='sw1'")] == [(73, "oxidized")]


def test_stale_portgroup_vlans_do_not_linger(conn):
    # moving a NIC to another port group must move its VLAN, not add one
    fid = dev(conn, "fw1", "opnsense", last_seen=NOW, role="firewall", status="up")
    pdb.upsert_interface(conn, device_id=fid, name="vmx1", mac="00:50:56:00:00:01")
    conn.execute("INSERT INTO vnic_vlans (mac, vid, portgroup, source) VALUES "
                 "('00:50:56:00:00:01', 299, 'old', 'vsphere')")
    normalize(conn)
    conn.execute("UPDATE vnic_vlans SET vid = 21, portgroup = 'new'")
    normalize(conn)
    assert [tuple(r) for r in conn.execute(
        "SELECT interface, vid FROM port_vlans WHERE device='fw1'")] == [("vmx1", 21)]


def test_first_poll_of_a_fresh_db_matches_the_second(conn):
    # A declaration and an fdb-uplink can describe the same cable. Inference
    # used to be superseded BEFORE declarations were applied, so on a fresh
    # database nothing had been declared yet and both rows survived the first
    # poll -- two cables out of one port until the next cycle tidied up.
    # Anyone migrating an existing DB never saw it.
    sid = dev(conn, "edge1", "librenms", last_seen=NOW, role="switch", status="up")
    hid = dev(conn, "vmhost1", "vsphere", last_seen=NOW, role="hypervisor", status="up")
    pdb.upsert_interface(conn, device_id=sid, name="e1/1/26", oper_status="up")
    pdb.upsert_interface(conn, device_id=hid, name="vmnic4", mac="00:25:90:00:00:01")
    conn.execute("INSERT INTO fdb (device, interface, mac) VALUES "
                 "('edge1', 'e1/1/26', '00:25:90:00:00:01')")
    decl = [("edge1", "e1/1/26", "vmhost1", "vmnic4")]

    normalize(conn, declared_links=decl)
    first = links(conn)
    assert first == [("edge1", "e1/1/26", "vmhost1", "vmnic4", "declared")], first

    normalize(conn, declared_links=decl)
    assert links(conn) == first, "normalize is not idempotent from a clean DB"


def test_a_known_interface_mac_stops_being_an_endpoint(conn):
    # Adding an integration to a working install must retract what it
    # supersedes. Before the owning collector existed this MAC was just an
    # anonymous thing on a switch port; once its device is known, the endpoint
    # row is a duplicate of a device patchbay already draws. Found by adding
    # vSphere to an install that had been running without it.
    sid = dev(conn, "sw1", "librenms", last_seen=NOW, role="switch", status="up")
    pdb.upsert_interface(conn, device_id=sid, name="p1", oper_status="up")
    pdb.upsert_endpoint(conn, mac="00:50:56:00:00:04", source="fdb",
                        device="sw1", interface="p1")
    # ...now the hypervisor collector is configured and claims that MAC
    hid = dev(conn, "hyp1", "vsphere", last_seen=NOW, role="hypervisor", status="up")
    pdb.upsert_interface(conn, device_id=hid, name="vmnic0",
                         mac="00:50:56:00:00:04")
    normalize(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM endpoints WHERE mac='00:50:56:00:00:04'"
    ).fetchone()[0] == 0

    # an address-book row for the same MAC is NOT withdrawn: it carries a
    # hostname and IP that the drift report needs
    pdb.upsert_endpoint(conn, mac="00:50:56:00:00:04", source="phpipam",
                        hostname="hyp1", ip="192.0.2.9")
    normalize(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM endpoints WHERE mac='00:50:56:00:00:04'"
    ).fetchone()[0] == 1


def test_a_declared_port_yields_no_anonymous_endpoint(conn):
    # A MAC learned on a port the operator has declared belongs to the device
    # at the far end -- it is not an unknown thing sitting behind the port.
    # Declarations used to be applied after endpoint placement, so the first
    # poll of a fresh DB scattered these and nothing ever withdrew them.
    sid = dev(conn, "edge1", "librenms", last_seen=NOW, role="switch", status="up")
    pdb.upsert_interface(conn, device_id=sid, name="e1/1/30", oper_status="up")
    conn.execute("INSERT INTO fdb (device, interface, mac) VALUES "
                 "('edge1', 'e1/1/30', '00:25:90:00:00:02')")
    normalize(conn, declared_links=[("edge1", "e1/1/30", "nas1", "lan2")])
    assert conn.execute(
        "SELECT COUNT(*) FROM endpoints WHERE device='edge1'").fetchone()[0] == 0


def test_endpoints_on_a_port_are_withdrawn_when_a_link_appears(conn):
    # the retroactive half: the endpoint existed before the cable was known
    sid = dev(conn, "edge1", "librenms", last_seen=NOW, role="switch", status="up")
    pdb.upsert_interface(conn, device_id=sid, name="e1/1/30", oper_status="up")
    pdb.upsert_endpoint(conn, mac="00:25:90:00:00:02", source="fdb",
                        device="edge1", interface="e1/1/30")
    normalize(conn)   # no declaration yet: the endpoint is the best answer
    assert conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0] == 1
    normalize(conn, declared_links=[("edge1", "e1/1/30", "nas1", "lan2")])
    assert conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0] == 0


def test_retiring_a_device_takes_its_interfaces_with_it(conn):
    """Interfaces are keyed by device id and the schema cascades on delete --
    but only while PRAGMA foreign_keys is ON, which is per-connection. The web
    app's connection did not set it, so every device retired from a /ops poll
    left its ports behind, while the same poll from the CLI cleaned up. Found
    by comparing a long-running database against a fresh install of the same
    network: the interface counts disagreed by one.
    """
    keep = dev(conn, "core1", "librenms", last_seen=NOW, role="switch")
    doomed = dev(conn, "vm-gone", "vsphere", last_seen=NOW, role="vm")
    pdb.upsert_interface(conn, device_id=keep, name="1/0/1")
    pdb.upsert_interface(conn, device_id=doomed, name="vmx0")

    # exactly what the ops-page connection used to do: retire a guest with
    # the constraint unenforced
    conn.commit()   # SQLite ignores the pragma inside a transaction
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM devices WHERE name = 'vm-gone'")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    assert conn.execute("SELECT COUNT(*) FROM interfaces").fetchone()[0] == 2

    normalize(conn)
    left = [r[0] for r in conn.execute(
        "SELECT name FROM interfaces WHERE device_id NOT IN (SELECT id FROM devices)")]
    assert left == [], left
    assert [r[0] for r in conn.execute("SELECT name FROM interfaces")] == ["1/0/1"]


def test_fdb_inference_skipped_on_uplink_port_with_alias_mismatch(conn):
    # LLDP stores the local port by its ifAlias description; FDB uses ifName.
    # When these differ, the uplink port must still be excluded from unmanaged
    # inference — the FDB MACs crossing it belong to the far switch, not a
    # phantom switch on this side.
    sw_a = dev(conn, "sw-a", "librenms", last_seen=NOW, role="switch")
    dev(conn, "sw-b", "librenms", last_seen=NOW, role="switch")
    # port 0/11 is the uplink; its ifAlias is what LLDP stores in the link
    pdb.upsert_interface(conn, device_id=sw_a, name="0/11",
                         description="SFP+1 - sw-b - uplink")
    # LLDP link uses the description (as stored by the librenms collector)
    pdb.upsert_link(conn, a_device="sw-a", a_interface="SFP+1 - sw-b - uplink",
                    b_device="sw-b", b_interface="0/1", source="lldp")
    # FDB records remote MACs on the ifName (0/11), not the description
    conn.execute("INSERT INTO fdb (device, interface, mac) VALUES ('sw-a', '0/11', ?)",
                 ("aa:bb:cc:dd:ee:01",))
    conn.execute("INSERT INTO fdb (device, interface, mac) VALUES ('sw-a', '0/11', ?)",
                 ("aa:bb:cc:dd:ee:02",))
    conn.execute("INSERT INTO fdb (device, interface, mac) VALUES ('sw-a', '0/11', ?)",
                 ("aa:bb:cc:dd:ee:03",))
    normalize(conn)
    # no phantom unmanaged switch should appear on the uplink port
    assert get_dev(conn, "unmanaged@sw-a:0/11") is None

# ---------------------------------------------------------------------------
# PATCHBAY_UNMANAGED label format
# ---------------------------------------------------------------------------

def test_declared_unmanaged_label_creates_named_device(conn):
    # "label=dev:iface" format: the node takes the custom label, not "unmanaged@"
    dev(conn, "sw1", "librenms", last_seen=NOW, role="switch")
    normalize(conn, declared_unmanaged=[("basement-switch", "sw1", "1/0/5")])
    assert get_dev(conn, "basement-switch") is not None
    assert get_dev(conn, "unmanaged@sw1:1/0/5") is None


def test_declared_unmanaged_legacy_format_still_works(conn):
    # legacy "(None, dev, iface)" tuple: node falls back to "unmanaged@dev:iface"
    dev(conn, "sw1", "librenms", last_seen=NOW, role="switch")
    normalize(conn, declared_unmanaged=[(None, "sw1", "1/0/8")])
    assert get_dev(conn, "unmanaged@sw1:1/0/8") is not None


def test_declared_unmanaged_prune_removes_declared_node_on_undeclare(conn):
    # when a port is removed from PATCHBAY_UNMANAGED, its declared node and
    # link must be deleted so the topology doesn't accumulate phantom switches
    dev(conn, "sw1", "librenms", last_seen=NOW, role="switch")
    # first poll — port declared, node created
    normalize(conn, declared_unmanaged=[(None, "sw1", "1/0/5")])
    assert get_dev(conn, "unmanaged@sw1:1/0/5") is not None
    # second poll — port removed from declaration
    normalize(conn, declared_unmanaged=[])
    assert get_dev(conn, "unmanaged@sw1:1/0/5") is None
    assert conn.execute(
        "SELECT COUNT(*) FROM links WHERE source='fdb-inference'").fetchone()[0] == 0


def test_declared_unmanaged_prune_evicts_inference_node_when_port_declared(conn):
    # when FDB inference has already created an unmanaged node for a port, and
    # the operator then adds it to PATCHBAY_UNMANAGED, the inferred node must
    # be replaced by the declared one (not coexist)
    dev(conn, "sw1", "librenms", last_seen=NOW, role="switch")
    # seed an inference-sourced unmanaged node as FDB inference would write it
    pdb.upsert_device(conn, name="unmanaged@sw1:1/0/5",
                      source="inference", role="unmanaged-switch", status="up")
    pdb.upsert_link(conn, a_device="sw1", a_interface="1/0/5",
                    b_device="unmanaged@sw1:1/0/5", b_interface="?",
                    source="fdb-inference")
    # operator now declares the port
    normalize(conn, declared_unmanaged=[("k8s-switch", "sw1", "1/0/5")])
    assert get_dev(conn, "unmanaged@sw1:1/0/5") is None
    assert get_dev(conn, "k8s-switch") is not None


def test_declared_unmanaged_survives_unreadable_declarations(conn):
    # prune_declared=False means "the declarations could not be read this
    # run" — declared nodes must not be evicted on such a poll, or one bad
    # .env read wipes every declared unmanaged switch
    dev(conn, "sw1", "librenms", last_seen=NOW, role="switch")
    normalize(conn, declared_unmanaged=[("closet", "sw1", "1/0/5")])
    assert get_dev(conn, "closet") is not None
    normalize(conn, declared_unmanaged=[], prune_declared=False)
    assert get_dev(conn, "closet") is not None
    # a readable-but-empty declaration DOES evict
    normalize(conn, declared_unmanaged=[])
    assert get_dev(conn, "closet") is None


def test_declared_unmanaged_label_rename_evicts_old_node(conn):
    # declared nodes never age out, so a changed label must evict the node
    # under the old name or both live forever
    dev(conn, "sw1", "librenms", last_seen=NOW, role="switch")
    normalize(conn, declared_unmanaged=[("old-name", "sw1", "1/0/5")])
    assert get_dev(conn, "old-name") is not None
    normalize(conn, declared_unmanaged=[("new-name", "sw1", "1/0/5")])
    assert get_dev(conn, "old-name") is None
    assert get_dev(conn, "new-name") is not None


def test_declared_unmanaged_label_collision_falls_back_to_auto_name(conn):
    # a label naming a real device must not hijack it (upsert_device merges
    # by name — "hyp1=sw1:1/0/5" would rewrite the hypervisor's role)
    dev(conn, "sw1", "librenms", last_seen=NOW, role="switch")
    dev(conn, "hyp9", "vsphere", last_seen=NOW, role="hypervisor")
    normalize(conn, declared_unmanaged=[("hyp9", "sw1", "1/0/5")])
    hyp = get_dev(conn, "hyp9")
    assert hyp["role"] == "hypervisor" and hyp["source"] == "vsphere"
    assert get_dev(conn, "unmanaged@sw1:1/0/5") is not None


def test_lldp_supersedes_unifi_link_on_same_port(conn):
    # the controller and LibreNMS LLDP report the same cable — one cable per
    # port: the device-level protocol wins, the controller relay is dropped
    dev(conn, "sw-a", "librenms", last_seen=NOW, role="switch")
    dev(conn, "sw-b", "unifi", last_seen=NOW, role="switch")
    pdb.upsert_link(conn, a_device="sw-a", a_interface="0/3",
                    b_device="sw-b", b_interface="Port 1", source="unifi")
    pdb.upsert_link(conn, a_device="sw-a", a_interface="0/3",
                    b_device="sw-b", b_interface="Port 1", source="lldp")
    normalize(conn)
    rows = conn.execute(
        "SELECT source FROM links WHERE a_interface != '?'").fetchall()
    assert [r["source"] for r in rows] == ["lldp"]


def test_unifi_link_supersedes_fdb_uplink(conn):
    # a controller-reported cable outranks MAC-table inference for the port
    dev(conn, "sw-a", "librenms", last_seen=NOW, role="switch")
    dev(conn, "sw-b", "unifi", last_seen=NOW, role="switch")
    pdb.upsert_link(conn, a_device="sw-a", a_interface="0/3",
                    b_device="sw-b", b_interface="?", source="fdb-uplink")
    pdb.upsert_link(conn, a_device="sw-a", a_interface="0/3",
                    b_device="sw-b", b_interface="Port 1", source="unifi")
    normalize(conn)
    srcs = {r["source"] for r in conn.execute("SELECT source FROM links")}
    assert srcs == {"unifi"}


def test_orientation_restore_keeps_fresher_timestamp_on_collision(conn):
    # a source that names a device by its alias re-reports the link every
    # poll; the rename flips it out of sorted order and it collides with
    # the canonical twin. The survivor must inherit the fresh last_seen or
    # a live link expires at the TTL (#37)
    dev(conn, "aa1", "librenms", last_seen=NOW, role="ap")
    dev(conn, "bb1", "librenms", last_seen=NOW, role="switch")
    pdb.upsert_link(conn, a_device="aa1", a_interface="p1",
                    b_device="bb1", b_interface="p2", source="lldp")
    old = NOW - 7000  # stale enough that the TTL would expire it
    conn.execute("UPDATE links SET last_seen = ? WHERE a_device='aa1'", (old,))
    # the fresh sighting arrives under the alias (sorts after bb1, so the
    # row lands flipped relative to the canonical twin)
    pdb.upsert_link(conn, a_device="zz1", a_interface="p1",
                    b_device="bb1", b_interface="p2", source="lldp")
    normalize(conn, seed_aliases={"zz1": "aa1"})
    rows = conn.execute(
        "SELECT * FROM links WHERE source='lldp'").fetchall()
    assert len(rows) == 1, [dict(r) for r in rows]
    assert (rows[0]["a_device"], rows[0]["b_device"]) == ("aa1", "bb1")
    assert rows[0]["last_seen"] > old + 6000, rows[0]["last_seen"]


def test_merge_raw_model_code_yields_to_translated_name(conn):
    """A raw UniFi model code stored by a prior poll must not outrank a
    human-readable name written by the same source after MODEL_NAMES was added.
    The merge treats all-caps-alphanumeric codes as junk so the gap-fill
    logic can pick up the translated value from any source."""
    # librenms row carries a stale raw code (as if merged from an old unifi row)
    dev(conn, "ap1", "librenms", last_seen=NOW, vendor="UAP-AC-Pro", model="U7PG2")
    # unifi row now carries the translated name
    dev(conn, "ap1", "unifi", last_seen=NOW, vendor="Ubiquiti", model="AC Pro")
    normalize(conn)
    d = get_dev(conn, "ap1")
    assert d["model"] == "AC Pro", d["model"]
