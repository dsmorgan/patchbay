"""Entity normalization: one real device = one record.

Sources identify the same box inconsistently — shortname vs FQDN, hostname vs
chassis serial, per-source duplicates. After every poll this pass:

1. canonicalizes device names (domain stripped, lowercased) and merges
   duplicate records, coalescing fields by source priority;
2. rewrites link endpoints that match a device's serial to its name;
3. fuses half-links (two rows for one device pair, each knowing only its
   own port) into a single row with both ports.

Merges are recorded in `aliases` so old names still resolve in the UI.
"""

from __future__ import annotations

import json
import sqlite3

from . import db

# When duplicates merge, the record from the earliest-listed source wins a
# field conflict; later sources only fill gaps. 'vm' role never overrides a
# network role (a firewall that happens to be a VM is still a firewall).
SOURCE_PRIORITY = ["librenms", "unifi", "opnsense", "vsphere", "phpipam"]
NETWORK_ROLES = {"switch", "router", "firewall", "ap", "hypervisor"}


def canonical_name(name: str) -> str:
    return (name or "").strip().rstrip(".").split(".")[0].lower()


def _merge_group(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> None:
    # freshest row within a source wins ties — a stable sort on priority alone
    # would make the OLDEST row primary (rowid order), freezing every field at
    # its first-seen value for devices that re-duplicate each poll
    rows = sorted(rows, key=lambda r: (
        SOURCE_PRIORITY.index(r["source"]) if r["source"] in SOURCE_PRIORITY else 99,
        -(r["last_seen"] or 0)))
    primary, rest = rows[0], rows[1:]
    canon = canonical_name(primary["name"])

    merged = dict(primary)
    # placeholder values ("generic" OS on ping-only LibreNMS devices, CPU
    # arch as vendor) are not information — don't let them outrank a real
    # value from another source
    JUNK = {"os": {"generic"},
            "vendor": {"amd64", "x86_64", "i386", "generic"}}
    for field, junk in JUNK.items():
        if (merged.get(field) or "").lower() in junk:
            merged[field] = None
    for r in rest:
        for field in ("mgmt_ip", "vendor", "model", "os", "parent", "serial", "status"):
            if (r[field] or "").lower() in JUNK.get(field, set()):
                continue
            if not merged.get(field) and r[field]:
                merged[field] = r[field]
    # a self-reported OS with a version ("opnsense 26.1.11") beats a bare
    # SNMP fingerprint ("freebsd") from a higher-priority source
    if merged.get("os") and not any(c.isdigit() for c in merged["os"]):
        for r in rest:
            v = r["os"]
            if v and any(c.isdigit() for c in v) and v.lower() not in JUNK["os"]:
                merged["os"] = v
                break
    # status is a live fact, not an identity fact: the freshest report wins
    # (a powered-back-on host must not stay 'notResponding' because the
    # stale primary row said so)
    fresh = max((r for r in [primary, *rest] if r["status"]),
                key=lambda r: r["last_seen"] or 0, default=None)
    if fresh is not None:
        merged["status"] = fresh["status"]
    # role: prefer any network role over 'vm'/None
    for r in rest:
        if (merged.get("role") not in NETWORK_ROLES) and r["role"]:
            if r["role"] in NETWORK_ROLES or not merged.get("role"):
                merged["role"] = r["role"]

    # retire duplicates first — one of them may already hold the canonical name
    for r in rest:
        conn.execute("UPDATE OR IGNORE interfaces SET device_id=? WHERE device_id=?",
                     (primary["id"], r["id"]))
        # collisions (same port name on both rows) survive on the duplicate:
        # fold the fresher row's fields onto the primary instead of silently
        # discarding a newer poll's data with the duplicate
        for dup in conn.execute(
                "SELECT * FROM interfaces WHERE device_id=?", (r["id"],)).fetchall():
            cur = conn.execute(
                "SELECT last_seen FROM interfaces WHERE device_id=? AND name=?",
                (primary["id"], dup["name"])).fetchone()
            if cur and (dup["last_seen"] or 0) >= (cur["last_seen"] or 0):
                db.upsert_interface(
                    conn, device_id=primary["id"], name=dup["name"],
                    **{f: dup[f] for f in ("ifindex", "admin_status", "oper_status",
                                           "speed_bps", "mac", "description",
                                           "ip", "ip6", "in_bps", "out_bps")
                       if dup[f] is not None})
        conn.execute("DELETE FROM interfaces WHERE device_id=?", (r["id"],))
        conn.execute("DELETE FROM devices WHERE id=?", (r["id"],))
    conn.execute(
        "UPDATE devices SET name=?, mgmt_ip=?, vendor=?, model=?, os=?, role=?, "
        "status=?, parent=?, serial=?, last_seen=? WHERE id=?",
        (canon, merged.get("mgmt_ip"), merged.get("vendor"), merged.get("model"),
         merged.get("os"), merged.get("role"), merged.get("status"),
         merged.get("parent"), merged.get("serial"),
         max((r["last_seen"] or 0) for r in rows), primary["id"]),
    )
    for r in [primary, *rest]:
        if r["name"] != canon:
            conn.execute("INSERT OR REPLACE INTO aliases (alias, canonical) VALUES (?, ?)",
                         (r["name"], canon))


def _rename_refs(conn: sqlite3.Connection) -> None:
    """Point name-based references (links, endpoints, parents) at canonical names."""
    alias_map = dict(conn.execute("SELECT alias, canonical FROM aliases").fetchall())
    # serials are aliases too
    for serial, name in conn.execute(
            "SELECT serial, name FROM devices WHERE serial IS NOT NULL").fetchall():
        alias_map[serial] = name
        conn.execute("INSERT OR REPLACE INTO aliases (alias, canonical) VALUES (?, ?)",
                     (serial, name))
    # collapse chains (old -> mid, mid -> new) so a rewrite can't depend on
    # dict iteration order and strand references at an intermediate name
    def resolve(name: str) -> str:
        seen: set[str] = set()
        while name in alias_map and name not in seen:
            seen.add(name)
            name = alias_map[name]
        return name

    alias_map = {a: resolve(c) for a, c in alias_map.items()}
    for alias, canon in alias_map.items():
        for col in ("a_device", "b_device"):
            for r in conn.execute(f"SELECT id FROM links WHERE {col} = ?",
                                  (alias,)).fetchall():
                _rewrite_link_field(conn, r["id"], col, canon)
        conn.execute("UPDATE endpoints SET device=? WHERE device=?", (canon, alias))
        conn.execute("UPDATE devices SET parent=? WHERE parent=?", (canon, alias))
        conn.execute("UPDATE OR IGNORE device_vlans SET device=? WHERE device=?", (canon, alias))
        conn.execute("DELETE FROM device_vlans WHERE device=?", (alias,))
        conn.execute("UPDATE OR IGNORE port_vlans SET device=? WHERE device=?", (canon, alias))
        conn.execute("DELETE FROM port_vlans WHERE device=?", (alias,))
        # fdb feeds the entire inference pass — a raw sysName here poisons
        # unmanaged-switch inference and invents alias<->canonical self-links
        conn.execute("UPDATE OR IGNORE fdb SET device=? WHERE device=?", (canon, alias))
        conn.execute("DELETE FROM fdb WHERE device=?", (alias,))
        conn.execute("UPDATE rate_history SET device=? WHERE device=?", (canon, alias))
        conn.execute("UPDATE OR IGNORE positions SET name=? WHERE name=?", (canon, alias))
        conn.execute("DELETE FROM positions WHERE name=?", (alias,))
    # renames can flip a row out of upsert_link's sorted orientation, hiding
    # it from the UNIQUE constraint and from pair-keyed deletes — restore it
    for r in conn.execute("SELECT * FROM links").fetchall():
        a = (r["a_device"], r["a_interface"])
        b = (r["b_device"], r["b_interface"])
        if a > b:
            conn.execute(
                "UPDATE OR IGNORE links SET a_device=?, a_interface=?, "
                "b_device=?, b_interface=? WHERE id=?", (*b, *a, r["id"]))
            # if the sorted twin already exists the UPDATE was ignored —
            # this row is a duplicate
            conn.execute(
                "DELETE FROM links WHERE id=? AND a_device=? AND a_interface=?",
                (r["id"], *a))


def _rewrite_link_field(conn: sqlite3.Connection, link_id: int,
                        column: str, value: str) -> None:
    """Set one column on one link row, folding into an existing twin instead
    of colliding with it.

    Freshness has to survive the rewrite. A source that names a device by FQDN
    re-inserts its row every poll; renaming it to the canonical name hits the
    already-present canonical row, so a plain UPDATE OR IGNORE + DELETE threw
    the fresh sighting away and left the survivor's last_seen frozen at its
    creation — until the TTL expired a link that was being reported the whole
    time. The survivor must inherit the newer timestamp."""
    row = conn.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
    if row is None or row[column] == value:
        return
    ends = {k: row[k] for k in ("a_device", "a_interface", "b_device", "b_interface")}
    ends[column] = value
    twin = conn.execute(
        "SELECT id FROM links WHERE a_device=? AND a_interface=? AND b_device=? "
        "AND b_interface=? AND source=? AND id != ?",
        (*ends.values(), row["source"], link_id)).fetchone()
    if twin:
        conn.execute(
            "UPDATE links SET last_seen = MAX(COALESCE(last_seen, 0), ?) WHERE id = ?",
            (row["last_seen"] or 0, twin["id"]))
        conn.execute("DELETE FROM links WHERE id = ?", (link_id,))
    else:
        conn.execute(f"UPDATE links SET {column} = ? WHERE id = ?", (value, link_id))


def _demote_kernel_link_ends(conn: sqlite3.Connection) -> None:
    """No cable ends on a vSwitch-backed kernel port (vmk0, vswif0, a VM's
    'Network adapter 1'): a switch does learn those MACs, but the physical far
    end is the host's pnic, which this evidence doesn't identify. Rewriting
    such an end to '?' keeps the map honest and collapses several kernel ports
    riding one uplink into a single cable."""
    from .ports import port_kind

    for r in conn.execute("SELECT * FROM links").fetchall():
        for ifcol in ("a_interface", "b_interface"):
            if port_kind(r[ifcol]) != "kernel":
                continue
            _rewrite_link_field(conn, r["id"], ifcol, "?")


def _fuse_links(conn: sqlite3.Connection) -> None:
    """Fuse rows that describe the SAME cable (each side seen from one end,
    the other end '?'). Rows whose known ports differ on either side are
    parallel links between the same pair — keep them apart."""
    rows = conn.execute("SELECT * FROM links").fetchall()
    by_pair: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for r in rows:
        key = (*sorted([r["a_device"], r["b_device"]]), r["source"])
        by_pair.setdefault(key, []).append(r)

    def compat(x: str, y: str) -> bool:
        return x == "?" or y == "?" or x == y

    for (da, db_, source), group in by_pair.items():
        if len(group) < 2 or da == db_:
            continue
        merged: list[dict] = []
        for r in group:
            ifs = {r["a_device"]: r["a_interface"], r["b_device"]: r["b_interface"]}
            for m in merged:
                if compat(m["da_if"], ifs[da]) and compat(m["db_if"], ifs[db_]):
                    m["da_if"] = ifs[da] if m["da_if"] == "?" else m["da_if"]
                    m["db_if"] = ifs[db_] if m["db_if"] == "?" else m["db_if"]
                    m["fresh"] = max(m["fresh"], r["last_seen"] or 0)
                    m["drop"].append(r["id"])
                    break
            else:
                merged.append({"da_if": ifs[da], "db_if": ifs[db_],
                               "fresh": r["last_seen"] or 0,
                               "keep": r["id"], "drop": []})
        for m in merged:
            # delete the fused-away rows first: one of them may already sit
            # in the exact canonical orientation the UPDATE is about to claim
            for rid in m["drop"]:
                conn.execute("DELETE FROM links WHERE id=?", (rid,))
            # the fused row inherits the group's freshest sighting — evidence
            # for this cable was just seen, whichever row carried it
            conn.execute(
                "UPDATE links SET a_device=?, a_interface=?, b_device=?, b_interface=?, "
                "last_seen=MAX(COALESCE(last_seen, 0), ?) "
                "WHERE id=?", (da, m["da_if"], db_, m["db_if"], m["fresh"], m["keep"]))


def _fill_single_port_links(conn: sqlite3.Connection) -> None:
    """A '?' link end on a device with exactly one physical port is that port."""
    from .ports import port_kind

    for side_dev, side_if in (("a_device", "a_interface"), ("b_device", "b_interface")):
        rows = conn.execute(
            f"SELECT id, {side_dev} AS dev FROM links WHERE {side_if} = '?'").fetchall()
        for r in rows:
            names = [x[0] for x in conn.execute(
                "SELECT i.name FROM interfaces i JOIN devices d ON d.id = i.device_id "
                "WHERE d.name = ?", (r["dev"],)).fetchall()]
            physical = [n for n in names if port_kind(n) == "physical"]
            if len(physical) == 1:
                _rewrite_link_field(conn, r["id"], side_if, physical[0])


UNMANAGED_MAC_THRESHOLD = 3  # >=N MACs on a non-LLDP port => something's hanging there


def canon_mac(mac: str) -> str:
    return "".join(c for c in (mac or "").lower() if c in "0123456789abcdef")


def _monitor_ports(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Ports configured as a mirror/SPAN destination."""
    return {(r["device"], r["interface"]) for r in conn.execute(
        "SELECT device, interface FROM port_roles WHERE role = 'monitor-dst'")}


def _place_endpoints_and_infer(conn: sqlite3.Connection,
                               declared: list[tuple[str | None, str, str]] | None = None) -> int:
    """FDB placement, in evidence order:
    1. a port carrying a MAC that belongs to a known device's interface links
       to that device on that interface (catches hypervisor multi-uplinks that
       don't speak LLDP);
    2. otherwise, many MACs on a non-linked port => inferred unmanaged switch;
    3. otherwise, a uniquely-seen MAC is a wired endpoint on that port."""
    from .ports import port_kind

    linked = set()
    # fdb-inference and fdb-uplink links are DERIVED from these same MACs —
    # counting them as "linked" would hide the MACs on the next run, so their
    # evidence could never refresh (and would wrongly age out)
    for r in conn.execute(
            "SELECT a_device, a_interface, b_device, b_interface FROM links "
            "WHERE source NOT IN ('fdb-inference', 'fdb-uplink')"):
        linked.add((r["a_device"], r["a_interface"]))
        linked.add((r["b_device"], r["b_interface"]))
    # LLDP links store the port by its ifAlias description (the user-visible
    # name); FDB entries use the SNMP ifName. Bridge the gap: if a description
    # is in linked, also mark the corresponding ifName as linked so FDB
    # inference doesn't fire on an inter-switch uplink just because the two
    # sources use different identifiers for the same physical port.
    for r in conn.execute(
            "SELECT d.name AS dev, i.name AS ifname, i.description AS desc "
            "FROM interfaces i JOIN devices d ON d.id = i.device_id "
            "WHERE i.description IS NOT NULL AND i.description != ''"):
        if (r["dev"], r["desc"]) in linked:
            linked.add((r["dev"], r["ifname"]))
    # interface MAC -> (owner device, interface name)
    if_by_mac: dict[str, tuple[str, str]] = {}
    self_macs: set[str] = set()
    for r in conn.execute(
            "SELECT d.name AS dev, i.name AS iface, i.mac FROM interfaces i "
            "JOIN devices d ON d.id = i.device_id WHERE i.mac IS NOT NULL"):
        m = canon_mac(r["mac"])
        if not m:
            continue
        # several interfaces can share one MAC (ESXi gives vmk0 its backing
        # pnic's address). Prefer the physical one: it's the actual cable end,
        # and without this the winner would depend on row order.
        prev = if_by_mac.get(m)
        if prev is None or (port_kind(prev[1]) != "physical"
                            and port_kind(r["iface"]) == "physical"):
            if_by_mac[m] = (r["dev"], r["iface"])
        self_macs.add(m)

    # An endpoint on a port that a real link claims is superseded too: the
    # port leads to a known device, so the MAC belongs to it rather than
    # floating free behind the port. Skipping linked ports below stops new
    # ones; this retracts any written before the link was known.
    if linked:
        conn.executemany(
            "DELETE FROM endpoints WHERE source = 'fdb' AND device = ? "
            "AND interface = ?", sorted(linked))

    # A MAC belonging to a known device's interface IS that device, not an
    # anonymous endpoint sitting behind a port. New endpoints already respect
    # that (see `plain` below), but rows written before the owning collector
    # was configured had no way to know, and nothing ever retracted them —
    # so adding an integration to a working install left the hosts it
    # identifies duplicated as endpoints. Only 'fdb' rows are withdrawn:
    # address-book sources carry hostnames and IPs worth keeping.
    stale = [(r["id"],) for r in conn.execute(
        "SELECT id, mac FROM endpoints WHERE source = 'fdb'")
        if canon_mac(r["mac"]) in self_macs]
    if stale:
        conn.executemany("DELETE FROM endpoints WHERE id = ?", stale)

    # a MAC table is only evidence while the port is up: entries on a down
    # port are residue that hasn't aged out yet, and inferring from them makes
    # the map inconsistent — one powered-off host's cable shows up because its
    # residue happens to resolve while an identical cable next to it doesn't
    down_ports = {(r["dev"], r["iface"]) for r in conn.execute(
        "SELECT d.name AS dev, i.name AS iface FROM interfaces i "
        "JOIN devices d ON d.id = i.device_id WHERE LOWER(i.oper_status) = 'down'")}
    # a SPAN destination transmits copies of OTHER ports' traffic. Any MAC that
    # shows up there belongs somewhere else in the fabric, so inferring from it
    # would hang a phantom switch (or a wrong uplink) off the probe port.
    down_ports |= _monitor_ports(conn)

    edge: dict[str, list[tuple[str, str]]] = {}   # mac -> [(device, port)]
    per_port: dict[tuple[str, str], set[str]] = {}
    for r in conn.execute("SELECT device, interface, mac FROM fdb"):
        if (r["device"], r["interface"]) in linked or \
                (r["device"], r["interface"]) in down_ports:
            continue
        edge.setdefault(r["mac"], []).append((r["device"], r["interface"]))
        per_port.setdefault((r["device"], r["interface"]), set()).add(r["mac"])

    parent_of = {r["name"]: r["parent"] for r in
                 conn.execute("SELECT name, parent FROM devices").fetchall()}

    inferred = 0
    declared_set = {(dev, iface) for _, dev, iface in (declared or [])}
    declared_names = {(dev, iface): label for label, dev, iface in (declared or [])}
    # Prune stale unmanaged nodes. upsert_link sorts ends, so '?' may be on
    # either side. Two cases:
    #   declared node  whose port left  declared_set → no longer authoritative
    #   inference node whose port joined declared_set → declared entry wins
    for r in conn.execute(
            "SELECT l.id AS lid, d.source AS dsrc, "
            "  CASE WHEN l.a_interface='?' THEN l.b_device ELSE l.a_device END AS dev, "
            "  CASE WHEN l.a_interface='?' THEN l.b_interface ELSE l.a_interface END AS iface, "
            "  CASE WHEN l.a_interface='?' THEN l.a_device ELSE l.b_device END AS node "
            "FROM links l "
            "JOIN devices d ON d.name = CASE WHEN l.a_interface='?' THEN l.a_device ELSE l.b_device END "
            "WHERE l.source='fdb-inference' AND (l.a_interface='?' OR l.b_interface='?') "
            "AND d.role='unmanaged-switch'").fetchall():
        port = (r["dev"], r["iface"])
        stale = (r["dsrc"] == "declared" and port not in declared_set) or \
                (r["dsrc"] == "inference" and port in declared_set)
        if stale:
            conn.execute("DELETE FROM links WHERE id = ?", (r["lid"],))
            conn.execute("DELETE FROM devices WHERE name = ?", (r["node"],))
            conn.execute("DELETE FROM endpoints WHERE device = ?", (r["node"],))
    for dev, iface in declared_set:
        label = declared_names.get((dev, iface))
        name = label if label else f"unmanaged@{dev}:{iface}"
        macs = per_port.get((dev, iface), set())
        db.upsert_device(conn, name=name, source="declared",
                         role="unmanaged-switch", status="up",
                         model=f"declared · {len(macs)} MACs live @ {dev} {iface}")
        db.upsert_link(conn, a_device=dev, a_interface=iface,
                       b_device=name, b_interface="?", source="fdb-inference")
        for mac in macs:
            if canon_mac(mac) not in self_macs:
                db.upsert_endpoint(conn, mac=mac, source="fdb", device=name, interface="?")
        inferred += 1
    for (dev, iface), macs in per_port.items():
        if (dev, iface) in declared_set:
            continue
        owners = {if_by_mac[canon_mac(m)] for m in macs
                  if canon_mac(m) in if_by_mac and if_by_mac[canon_mac(m)][0] != dev}
        if owners:
            # the port leads to known equipment (e.g. an ESXi uplink that
            # doesn't advertise LLDP) — link it, don't invent a switch.
            # A match on a VM's virtual NIC means the port physically goes to
            # the VM's host: collapse to the hypervisor. A match on a kernel
            # port (vmk0…) means the same for the host itself — and since no
            # cable ends on a vSwitch-backed port, the far end is '?', which
            # also collapses several vmks on one uplink into ONE cable.
            resolved = set()
            for od, oif in owners:
                parent = parent_of.get(od)
                if parent:
                    resolved.add((parent, "?"))
                else:
                    resolved.add((od, oif if port_kind(oif) == "physical" else "?"))
            for owner_dev, owner_if in resolved:
                if owner_dev != dev:
                    db.upsert_link(conn, a_device=dev, a_interface=iface,
                                   b_device=owner_dev, b_interface=owner_if,
                                   source="fdb-uplink")
            continue
        plain = [m for m in macs if canon_mac(m) not in self_macs]
        if len(plain) >= UNMANAGED_MAC_THRESHOLD:
            name = f"unmanaged@{dev}:{iface}"
            db.upsert_device(conn, name=name, source="inference",
                             role="unmanaged-switch", status="up",
                             model=f"~{len(plain)} MACs behind {dev} {iface}")
            db.upsert_link(conn, a_device=dev, a_interface=iface,
                           b_device=name, b_interface="?", source="fdb-inference")
            inferred += 1
            for mac in plain:
                db.upsert_endpoint(conn, mac=mac, source="fdb", device=name, interface="?")
        else:
            for mac in plain:
                if len(edge.get(mac, [])) == 1:
                    db.upsert_endpoint(conn, mac=mac, source="fdb",
                                       device=dev, interface=iface)
    return inferred


def _apply_vnic_vlans(conn: sqlite3.Connection) -> int:
    """Turn hypervisor port group VLANs into per-port membership.

    A guest OS reports its interfaces but never their VLAN tags — the vSwitch
    adds and strips those, so from inside the VM the frames arrive untagged.
    The hypervisor knows, and MAC addresses join the two views: vCenter's
    'Network adapter 1' and the firewall's 'vmx1' are one NIC.

    Config-derived membership always wins; this only fills silence.
    """
    from .collectors.vsphere import VGT_TRUNK

    have = {(r["device"], r["interface"]) for r in conn.execute(
        "SELECT device, interface FROM port_vlans WHERE source != 'vsphere'")}
    conn.execute("DELETE FROM port_vlans WHERE source = 'vsphere'")
    by_mac: dict[str, tuple[str, str]] = {}
    for r in conn.execute(
            "SELECT d.name AS dev, i.name AS iface, i.mac FROM interfaces i "
            "JOIN devices d ON d.id = i.device_id WHERE i.mac IS NOT NULL"):
        m = canon_mac(r["mac"])
        if m and m not in by_mac:
            by_mac[m] = (r["dev"], r["iface"])
    known = {r["vid"] for r in conn.execute("SELECT vid FROM vlans")}
    n = 0
    for r in conn.execute("SELECT mac, vid FROM vnic_vlans"):
        target = by_mac.get(canon_mac(r["mac"]))
        if target is None or target in have:
            continue
        # VGT: the guest does its own tagging, so every VLAN the fabric knows
        # can pass — a trunk, exactly as a switch config would describe it
        vids = sorted(known) if r["vid"] == VGT_TRUNK else [r["vid"]]
        for vid in vids:
            conn.execute(
                "INSERT OR REPLACE INTO port_vlans (device, interface, vid, tagged, source) "
                "VALUES (?, ?, ?, ?, 'vsphere')",
                (*target, vid, int(r["vid"] == VGT_TRUNK)))
        n += 1
    return n


def _propagate_vlans(conn: sqlite3.Connection,
                     filters: dict[tuple[str, str], set[int]] | None = None) -> None:
    """Trunks flood by default: a VLAN reported anywhere in the fabric is
    assumed to reach every device connected to it, EXCEPT through ports the
    operator declared filtered (PATCHBAY_VLAN_FILTER). Written back with
    source='trunk' so the UI can distinguish reported from assumed."""
    # per-port membership parsed from configs restricts flooding the same way
    # operator filters do; explicit declarations win on conflict
    parsed: dict[tuple[str, str], set[int]] = {}
    for r in conn.execute("SELECT device, interface, vid FROM port_vlans"):
        parsed.setdefault((r["device"], r["interface"]), set()).add(r["vid"])
    filters = {**parsed, **(filters or {})}
    conn.execute("DELETE FROM device_vlans WHERE source = 'trunk'")
    fabric = {r["name"] for r in conn.execute(
        "SELECT name FROM devices "
        "WHERE role IN ('switch','router','firewall','hypervisor','ap')")}
    carried: dict[str, set[int]] = {}
    for r in conn.execute("SELECT device, vid FROM device_vlans"):
        carried.setdefault(r["device"], set()).add(r["vid"])
    links = [tuple(r) for r in conn.execute(
        "SELECT a_device, a_interface, b_device, b_interface FROM links")
        if r["a_device"] in fabric and r["b_device"] in fabric]
    # VM containment counts as a trunk: a virtualized firewall/appliance sees
    # whatever VLANs reach its hypervisor's uplinks
    links += [(r["name"], "vm", r["parent"], "") for r in conn.execute(
        "SELECT name, parent FROM devices WHERE parent IS NOT NULL")
        if r["name"] in fabric and r["parent"] in fabric]
    changed = True
    while changed:
        changed = False
        for ad, ai, bd, bi in links:
            passable = carried.get(ad, set()) | carried.get(bd, set())
            for end in ((ad, ai), (bd, bi)):
                if end in filters:
                    passable &= filters[end]
            for dev in (ad, bd):
                new = passable - carried.get(dev, set())
                if new:
                    carried.setdefault(dev, set()).update(new)
                    changed = True
    for dev, vids in carried.items():
        for vid in vids:  # PK keeps reported rows; only genuinely new ones land
            conn.execute("INSERT OR IGNORE INTO device_vlans (device, vid, source) "
                         "VALUES (?, ?, 'trunk')", (dev, vid))


def _drop_flooded_neighbors(conn: sqlite3.Connection) -> None:
    """CDP multicast flooded through a non-consuming switch makes a port
    'hear' devices that are hops away — a switch trunk hears a far vmnic,
    and a vmnic hears another host's vmnic. If a port has a discovery link
    to a managed switch, additional discovery neighbors on that same port
    are flood artifacts, not cables. Applies to switch-side (lldp) and
    hypervisor-side (vsphere-hint) discovery alike."""
    switches = {r["name"] for r in conn.execute(
        "SELECT name FROM devices WHERE role = 'switch'")}
    by_port: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in conn.execute(
            "SELECT * FROM links WHERE source IN ('lldp', 'vsphere-hint')").fetchall():
        by_port.setdefault((r["a_device"], r["a_interface"]), []).append(r)
        by_port.setdefault((r["b_device"], r["b_interface"]), []).append(r)
    for (dev, iface), group in by_port.items():
        if len(group) < 2 or iface == "?":
            continue

        def far(r: sqlite3.Row) -> str:
            return r["b_device"] if r["a_device"] == dev else r["a_device"]

        if any(far(r) in switches for r in group):
            for r in group:
                if far(r) not in switches:
                    conn.execute("DELETE FROM links WHERE id=?", (r["id"],))


def _drop_superseded_inference(conn: sqlite3.Connection) -> None:
    """A port has one cable: keep the strongest report of it. A discovery-
    protocol link supersedes a hypervisor hint for the same port, and any
    stated link supersedes FDB inference."""
    def port_set(sources: tuple[str, ...]) -> set[tuple[str, str]]:
        s = set()
        for r in conn.execute(
                f"SELECT * FROM links WHERE source IN ({','.join('?' * len(sources))})",
                sources):
            # a '?' end is an unknown port, not a port identity — letting it
            # into the set would make one unknown-port link on a device
            # suppress every other unknown-port link on that device
            if r["a_interface"] != "?":
                s.add((r["a_device"], r["a_interface"]))
            if r["b_interface"] != "?":
                s.add((r["b_device"], r["b_interface"]))
        return s

    def drop_where(source: str, stronger: set[tuple[str, str]]) -> None:
        for r in conn.execute("SELECT * FROM links WHERE source = ?", (source,)).fetchall():
            if (r["a_interface"] != "?" and
                    (r["a_device"], r["a_interface"]) in stronger) or \
               (r["b_interface"] != "?" and
                    (r["b_device"], r["b_interface"]) in stronger):
                conn.execute("DELETE FROM links WHERE id=?", (r["id"],))

    drop_where("vsphere-hint", port_set(("lldp",)))
    drop_where("fdb-uplink", port_set(("lldp", "vsphere-hint", "declared")))

    # an INFERRED unmanaged switch can't share a port with a real link: once
    # lldp/hint/declared/uplink claims the port (e.g. a hypervisor came up and
    # its VM MACs, briefly ownerless, had spawned a ghost switch), retire the
    # inferred device and its link. Operator-declared unmanaged switches are
    # exempt — the .env is their authority.
    real = port_set(("lldp", "vsphere-hint", "declared", "fdb-uplink"))
    for r in conn.execute(
            "SELECT * FROM links WHERE source = 'fdb-inference'").fetchall():
        a, b = (r["a_device"], r["a_interface"]), (r["b_device"], r["b_interface"])
        if a not in real and b not in real:
            continue
        # the far end (interface '?') is the inferred switch. Only retire ones
        # WE invented (source='inference'); an operator-declared unmanaged
        # switch is authoritative and keeps both its device and its link.
        ghost = r["b_device"] if r["b_interface"] == "?" else r["a_device"]
        row = conn.execute("SELECT source FROM devices WHERE name = ?",
                           (ghost,)).fetchone()
        if row and row["source"] == "inference":
            conn.execute("DELETE FROM links WHERE id = ?", (r["id"],))
            conn.execute("DELETE FROM devices WHERE name = ?", (ghost,))
            conn.execute("DELETE FROM endpoints WHERE device = ?", (ghost,))


EVIDENCE_TTL = 2 * 3600  # ~24 missed 5-minute polls
# ...but a source is only allowed to expire evidence it actually re-reports.
# An ESXi CDP hint is valid for ~60s after each advertisement, so vSphere
# legitimately returns nothing for a live uplink on most polls; holding it to
# the same clock as LLDP made real links blink out and back every couple of
# hours. Absence of an intermittent report is not evidence of absence.
SOURCE_TTL = {"vsphere-hint": 24 * 3600}


QUIET_STATUS = ("down", "notresponding", "disabled")


def _expire_stale_evidence(conn: sqlite3.Connection) -> int:
    """Discovered evidence that stops being reported ages out: a moved cable's
    old lldp row, an inferred switch whose MACs vanished. Live rows are
    refreshed every poll (collector upserts and the inference pass), so
    anything older than the TTL is genuinely gone.

    Two exemptions. Declared rows never expire — the operator's declaration is
    their lifecycle. And evidence touching a device that is *down* is kept:
    silence from a powered-off box explains itself, so its cabling is
    remembered rather than forgotten. A homelab host that is normally off
    (a noisy backup server, say) must not lose its place on the map — and if
    a cable really did move meanwhile, the next poll after it powers on
    replaces the evidence anyway."""
    cutoff = db.now() - EVIDENCE_TTL
    quiet = {r["name"] for r in conn.execute(
        f"SELECT name FROM devices WHERE LOWER(status) IN "
        f"({','.join('?' * len(QUIET_STATUS))})", QUIET_STATUS)}
    dropped = 0
    for r in conn.execute(
            "SELECT id, a_device, b_device, source, last_seen FROM links "
            "WHERE source != 'declared' AND COALESCE(last_seen, 0) < ?",
            (db.now() - min(EVIDENCE_TTL, *SOURCE_TTL.values()),)).fetchall():
        if r["a_device"] in quiet or r["b_device"] in quiet:
            continue
        if (r["last_seen"] or 0) >= db.now() - SOURCE_TTL.get(r["source"], EVIDENCE_TTL):
            continue
        conn.execute("DELETE FROM links WHERE id = ?", (r["id"],))
        dropped += 1
    # inferred unmanaged switches whose supporting evidence expired — unless
    # the switch that feeds them is itself quiet (no FDB, no MACs, no proof)
    for r in conn.execute(
            "SELECT name FROM devices WHERE source = 'inference' "
            "AND COALESCE(last_seen, 0) < ?", (cutoff,)).fetchall():
        feeder = r["name"].split("@", 1)[-1].split(":", 1)[0]
        if feeder in quiet:
            continue
        conn.execute("DELETE FROM devices WHERE name = ?", (r["name"],))
        conn.execute("DELETE FROM links WHERE a_device = ? OR b_device = ?",
                     (r["name"], r["name"]))
        dropped += 1
    return dropped


def _apply_declared_links(conn: sqlite3.Connection,
                          declared: list[tuple[str, str, str, str]],
                          prune: bool = True) -> None:
    """Operator-stated cabling wins: insert it, and drop weaker evidence rows
    ('?' ends) for the same device pair that it supersedes."""
    # undeclaring must actually undeclare: retire rows whose declaration
    # has been removed from the site .env — but only when we're sure we have
    # the full declaration set (prune=False when the DB read failed, so a
    # false empty can't delete real cabling)
    if prune:
        keep = set()
        for ad, ai, bd, bi in declared:
            keep.add((ad, ai, bd, bi))
            keep.add((bd, bi, ad, ai))
        for r in conn.execute(
                "SELECT id, a_device, a_interface, b_device, b_interface "
                "FROM links WHERE source='declared'").fetchall():
            if (r["a_device"], r["a_interface"],
                    r["b_device"], r["b_interface"]) not in keep:
                conn.execute("DELETE FROM links WHERE id=?", (r["id"],))
    for ad, ai, bd, bi in declared:
        db.upsert_link(conn, a_device=ad, a_interface=ai,
                       b_device=bd, b_interface=bi, source="declared")
        pa, pb = sorted([ad, bd])
        conn.execute(
            "DELETE FROM links WHERE a_device=? AND b_device=? AND source != 'declared' "
            "AND (a_interface='?' OR b_interface='?')", (pa, pb))
        # a declaration and a live report of the SAME cable are one cable, not
        # two parallel edges. Live discovery wins the draw — it confirms the
        # declaration and says more (the map upgrades from "operator says" to
        # "confirmed" when the host is up). The declaration is re-applied every
        # normalize, so it reappears the moment discovery goes quiet again.
        a, b = sorted([(ad, ai), (bd, bi)])
        conn.execute(
            "DELETE FROM links WHERE a_device=? AND a_interface=? AND b_device=? "
            "AND b_interface=? AND source='declared' AND EXISTS (SELECT 1 FROM links l "
            "WHERE l.a_device=? AND l.a_interface=? AND l.b_device=? AND l.b_interface=? "
            "AND l.source IN ('lldp', 'vsphere-hint'))", (*a, *b, *a, *b))


def _check_declarations(conn: sqlite3.Connection,
                        declared: list[tuple[str, str, str, str]]) -> list[str]:
    """Declarations are documentation, and documentation goes stale. Two ways
    to catch it, both reported on /ops:

    1. A port a declaration claims is reported by live discovery as going
       somewhere else. A port has one cable, so the observation wins and the
       declaration is dropped — silently keeping both would draw two cables
       out of one port with no clue which is real.
    2. The switch's own port description names a different device. Nothing is
       dropped for this one (a description can be the stale half), but the
       disagreement is worth an operator's attention."""
    import re

    notes: list[str] = []
    observed: dict[tuple[str, str], tuple[str, str]] = {}
    for r in conn.execute(
            "SELECT * FROM links WHERE source IN ('lldp', 'vsphere-hint')"):
        observed[(r["a_device"], r["a_interface"])] = (r["b_device"], r["b_interface"])
        observed[(r["b_device"], r["b_interface"])] = (r["a_device"], r["a_interface"])
    descr: dict[tuple[str, str], str] = {
        (r["dev"], r["iface"]): r["descr"] for r in conn.execute(
            "SELECT d.name AS dev, i.name AS iface, i.description AS descr "
            "FROM interfaces i JOIN devices d ON d.id = i.device_id "
            "WHERE i.description IS NOT NULL AND i.description != ''")}
    known = {r["name"] for r in conn.execute("SELECT name FROM devices")}

    for ad, ai, bd, bi in declared:
        for near, far in (((ad, ai), (bd, bi)), ((bd, bi), (ad, ai))):
            seen = observed.get(near)
            if seen and seen[0] != far[0]:
                notes.append(
                    f"{near[0]}:{near[1]} is declared to {far[0]}, but is "
                    f"reported connected to {seen[0]}:{seen[1]} — the "
                    f"observation wins; update PATCHBAY_LINKS")
                conn.execute(
                    "DELETE FROM links WHERE source = 'declared' AND "
                    "((a_device=? AND a_interface=?) OR (b_device=? AND b_interface=?))",
                    (*near, *near))
                break
            text = descr.get(near)
            if text and far[0] not in known:
                continue
            if text and not re.search(rf"\b{re.escape(far[0])}\b", text, re.I):
                named = [n for n in known
                         if n != near[0] and re.search(rf"\b{re.escape(n)}\b", text, re.I)]
                if named:
                    notes.append(
                        f"{near[0]}:{near[1]} is declared to {far[0]}, but its "
                        f"port description says {named[0]} ({text!r}) — one of "
                        f"the two is out of date")
    return notes


def normalize(conn: sqlite3.Connection, seed_aliases: dict[str, str] | None = None,
              declared_unmanaged: list[tuple[str | None, str, str]] | None = None,
              declared_links: list[tuple[str, str, str, str]] | None = None,
              vlan_filters: dict[tuple[str, str], set[int]] | None = None,
              prune_declared: bool = True) -> str:
    for alias, canonical in (seed_aliases or {}).items():
        conn.execute("INSERT OR REPLACE INTO aliases (alias, canonical) VALUES (?, ?)",
                     (alias, canonical))
    before = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in conn.execute("SELECT * FROM devices").fetchall():
        groups.setdefault(canonical_name(r["name"]), []).append(r)
    for rows in groups.values():
        _merge_group(conn, rows)
    _rename_refs(conn)
    _demote_kernel_link_ends(conn)
    _fuse_links(conn)
    _fill_single_port_links(conn)
    # Declarations are applied FIRST, because everything downstream reasons
    # about which ports are already claimed. Run afterwards, they arrived too
    # late for the first poll of a fresh database: an fdb-uplink duplicating a
    # declared cable survived (two edges from one port), and MACs learned on a
    # declared port became anonymous endpoints instead of belonging to the
    # device at the far end. Both stuck, because later polls found the
    # declarations already on disk and looked correct — so only a clean
    # install ever showed them.
    _apply_declared_links(conn, declared_links or [], prune=prune_declared)
    inferred = _place_endpoints_and_infer(conn, declared_unmanaged)
    _drop_flooded_neighbors(conn)
    _drop_superseded_inference(conn)
    conflicts = _check_declarations(conn, declared_links or [])
    db.set_state(conn, "declaration_conflicts", json.dumps(conflicts))
    expired = _expire_stale_evidence(conn)
    tagged = _apply_vnic_vlans(conn)
    _propagate_vlans(conn, vlan_filters)
    # housekeeping: raw payloads are a debugging window, not an archive
    conn.execute("DELETE FROM raw_payloads WHERE fetched_at < ?",
                 (db.now() - 7 * 86400,))
    # Interfaces are keyed by device *id*, so retiring a device (a deleted VM,
    # a merged duplicate) leaves its ports behind with nothing to join to.
    # They are invisible on every page and harmless until something counts
    # rows, but they never leave on their own. Name-keyed tables (fdb,
    # port_vlans, endpoints) don't need this: their writers refresh them.
    conn.execute("DELETE FROM interfaces "
                 "WHERE device_id NOT IN (SELECT id FROM devices)")
    after = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    n_links = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    return (f"{before} -> {after} devices, {n_links} links, "
            f"{inferred} unmanaged inferred"
            + (f", {tagged} vnics tagged from port groups" if tagged else "")
            + (f", {expired} stale evidence expired" if expired else ""))
