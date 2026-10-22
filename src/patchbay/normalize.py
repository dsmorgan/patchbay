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

import ipaddress
import json
import re
import sqlite3
from collections import Counter

from . import alerting, db, known_cables


def _is_raw_model_code(value: str | None) -> bool:
    """A raw UniFi model code (e.g. "U7PG2") that MODEL_NAMES translates is
    junk in a merge — a row persisted before the translation existed must
    not outrank the translated name. Only EXACT known codes count: a shape
    test (all-caps alphanumeric) also matches real model strings, so an
    untranslated code stays visible as-is rather than being erased."""
    from .collectors.unifi import MODEL_NAMES
    return (value or "") in MODEL_NAMES

# When duplicates merge, the record from the earliest-listed source wins a
# field conflict; later sources only fill gaps. 'vm' role never overrides a
# network role (a firewall that happens to be a VM is still a firewall).
SOURCE_PRIORITY = ["librenms", "unifi", "opnsense", "vsphere", "phpipam"]
NETWORK_ROLES = {"switch", "router", "firewall", "ap", "hypervisor"}


_DNS_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?")


def _looks_like_domain(suffix: str) -> bool:
    """True when `suffix` reads as a DNS domain ("lan", "example.com",
    "lab1", "k8s.local"): hostname-shaped labels, the last with at least
    one letter. A version number ("04" in "ubuntu-22.04", "9" in
    "centos-7.9") or a logical-port unit ("0" in "ge-0/0/1.0") has none,
    so the name containing it stays whole."""
    labels = suffix.split(".")
    return (all(_DNS_LABEL.fullmatch(lb) for lb in labels)
            and any(c.isalpha() for c in labels[-1]))


def canonical_name(name: str) -> str:
    """One device, one key: a name lowercased, with its DNS domain dropped
    ("sw1.lan" and "SW1" are both "sw1"). Only a real domain suffix is
    dropped. An IP address stays whole, or every host added by address
    would merge into a device named after its first octet, and so does a
    dotted name that isn't an FQDN ("ubuntu-22.04" and "ubuntu-22.10" are
    two VMs, not "ubuntu-22")."""
    n = (name or "").strip().rstrip(".").lower()
    try:
        ipaddress.ip_address(n.split("%", 1)[0])
        return n
    except ValueError:
        pass
    head, dot, suffix = n.partition(".")
    if dot and head and _looks_like_domain(suffix):
        return head
    return n


def host_device_name(host: str) -> str:
    """The device name for a configured host setting ("fw1.lan",
    "https://192.0.2.1:8443/", "[2001:db8::1]"): the host part alone, by
    the same rule as every other source's names."""
    from urllib.parse import urlsplit
    host = (host or "").strip()
    try:
        hostname = urlsplit(host if "://" in host else f"//{host}").hostname
    except ValueError:
        hostname = None
    return canonical_name(hostname or host)


def first_label_of_host(host: str) -> str:
    """The name the firewall collectors derived from a host setting before
    #98 (cut at the first dot, case kept). Kept only to retire it."""
    return (host or "").split("://")[-1].split(":")[0].split(".")[0]


def _first_label(name: str) -> str:
    """The rule canonical_name used before #98: everything up to the first
    dot. Kept only to recognize the aliases that rule wrote."""
    return (name or "").strip().rstrip(".").split(".")[0].lower()


def _identity_key(row: sqlite3.Row) -> str:
    """The name a device row merges under. A switch normalize synthesized
    itself (an inferred `unmanaged@dev:port`, or a declared label) is
    already its own identity: canonicalizing it would lowercase a declared
    "Rack-Switch" into a name the next pass doesn't expect, and the
    declaration would re-create the original beside it every poll."""
    if row["role"] == "unmanaged-switch" and row["source"] in ("inference", "declared"):
        return row["name"]
    return canonical_name(row["name"])


# Name-keyed tables a retired name's rows are dropped from when it has no
# heir. config_revisions is not among them: a config history is a record,
# not a drawing, and it stays readable under the old name.
_NAME_KEYED = (("endpoints", "device"), ("positions", "name"),
               ("device_vlans", "device"), ("port_vlans", "device"),
               ("fdb", "device"), ("rate_history", "device"),
               ("routes", "device"), ("port_roles", "device"),
               ("tunnels", "device"))


def pick_heir(old_ip: str | None,
              candidates: list[tuple[str, str | None]]) -> str | None:
    """The one device a retired name most plausibly was, from (name,
    mgmt_ip) candidates: the only candidate, else the only one carrying
    the old name's address. None when that doesn't single one out."""
    if len(candidates) == 1:
        return candidates[0][0]
    same_ip = [n for n, ip in candidates if old_ip and ip == old_ip]
    return same_ip[0] if len(same_ip) == 1 else None


def retire_name(conn: sqlite3.Connection, old: str, heir: str | None) -> None:
    """A device name the identity rule no longer produces goes away.

    With an heir, an alias old -> heir hands everything keyed by the old
    name (links, pins, VLANs, tunnels, routes, config history, known
    cables) to the heir through _rename_refs, which runs later in the same
    normalize; expected-tunnel declarations, which don't follow aliases,
    are moved here. Without one, the old name's rows are dropped, so
    nothing keeps drawing beside the devices that replaced it."""
    if heir == old:
        return
    if heir:
        conn.execute("INSERT OR REPLACE INTO aliases (alias, canonical) VALUES (?, ?)",
                     (old, heir))
        from . import expected_tunnels
        expected_tunnels.rename(conn, old, heir)
        return
    for table, col in _NAME_KEYED:
        conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (old,))
    conn.execute("DELETE FROM links WHERE a_device = ? OR b_device = ?", (old, old))
    conn.execute("DELETE FROM known_cables WHERE a_device = ? OR b_device = ?", (old, old))


def retire_source_names(conn: sqlite3.Connection, source: str,
                        renames: dict[str, set[str]]) -> None:
    """A collector's own names that #98 changed ({old: {new, ...}}): an
    OPNSENSE_HOST of 192.0.2.1 named the firewall "192", now "192.0.2.1".
    The collector's old device row goes, and the old name is retired with
    the one new name as heir. Two hosts that both cut to the old name had
    their rows mixed under it, so they get none and those rows are dropped.
    A name another source's device still holds, or one an alias already
    hands elsewhere, is left alone."""
    for old, news in renames.items():
        news = news - {old}
        if not old or not news:
            continue
        conn.execute("DELETE FROM devices WHERE name = ? AND source = ?", (old, source))
        if conn.execute("SELECT 1 FROM devices WHERE name = ?", (old,)).fetchone():
            continue
        heir = next(iter(news)) if len(news) == 1 else None
        prev = conn.execute("SELECT canonical FROM aliases WHERE alias = ?",
                            (old,)).fetchone()
        if prev and prev[0] != heir:
            continue
        retire_name(conn, old, heir)


def _retire_stale_aliases(conn: sqlite3.Connection, keys: dict[str, str],
                          seeds: set[str]) -> None:
    """Drop the aliases the identity rule no longer makes.

    Before #98, every name was cut at its first dot, and each merge wrote
    an alias from the full name to the cut one ("192.0.2.21" -> "192",
    "ubuntu-22.04" -> "ubuntu-22", "Rack-Switch" -> "rack-switch"). Those
    aliases outlive the rule: left in place, _rename_refs would keep
    rewriting the device's links, fdb, routes and pin onto the old merged
    name. An alias is retired when the old rule explains it and the current
    one doesn't, or when its name is a live device that is its own
    identity (an alias must never capture a device). Operator-declared
    (seed) aliases are never touched.

    A merged name left with no alias and no device is retired itself
    (retire_name), its heir picked by pick_heir from the names that were
    merged into it. A merged name a device still holds waits for
    _retire_merge_survivors, which can tell a leftover from a real device."""
    retired: dict[str, list[str]] = {}
    for r in conn.execute("SELECT alias, canonical FROM aliases").fetchall():
        alias, canon = r["alias"], r["canonical"]
        if alias in seeds or alias == canon:
            continue
        stale_rule = (_first_label(alias) == canon
                      and keys.get(alias, canonical_name(alias)) != canon)
        captures = keys.get(alias) == alias
        if not (stale_rule or captures):
            continue
        conn.execute("DELETE FROM aliases WHERE alias = ?", (alias,))
        if stale_rule:
            retired.setdefault(canon, []).append(alias)
    ips = dict(conn.execute("SELECT name, mgmt_ip FROM devices").fetchall())
    for canon, olds in retired.items():
        if canon in ips or conn.execute(
                "SELECT 1 FROM aliases WHERE canonical = ?", (canon,)).fetchone():
            continue
        retire_name(conn, canon,
                    pick_heir(None, [(a, ips.get(a)) for a in sorted(olds)]))


# How long a merge survivor may go unreported by its own source while that
# source keeps reporting the names it was merged from (three 5-minute polls)
SURVIVOR_GRACE = 15 * 60


def _retire_merge_survivors(conn: sqlite3.Connection) -> None:
    """Remove device rows that exist only because the first-dot rule merged
    other names into them.

    Two vSphere hosts added by address were one hypervisor "192" before
    #98. After the upgrade the source reports them under their own names
    again, but nothing deletes "192" (vSphere never retires hosts), so it
    stays on the map beside them. A row is such a survivor when no alias
    points at it any more, its name is the first label of live devices
    whose canonical name is something else, and its own source has kept
    reporting those devices for SURVIVOR_GRACE without reporting it. A
    real device that happens to share the first label is reported by its
    source every poll and never qualifies. What was keyed by its name goes
    to the device it most plausibly was (pick_heir: the only member, or the
    one with its address) or, with no such device, is dropped."""
    members: dict[str, list[sqlite3.Row]] = {}
    for r in conn.execute("SELECT * FROM devices").fetchall():
        first = _first_label(r["name"])
        if first != canonical_name(r["name"]):
            members.setdefault(first, []).append(r)
    if not members:
        return
    for c in conn.execute(
            f"SELECT * FROM devices WHERE name IN ({','.join('?' * len(members))})",
            sorted(members)).fetchall():
        if c["source"] in ("inference", "declared") or conn.execute(
                "SELECT 1 FROM aliases WHERE canonical = ?", (c["name"],)).fetchone():
            continue
        same = [m for m in members[c["name"]] if m["source"] == c["source"]]
        if not same or (c["last_seen"] or 0) > \
                min(m["last_seen"] or 0 for m in same) - SURVIVOR_GRACE:
            continue
        retire_name(conn, c["name"], pick_heir(
            c["mgmt_ip"], [(m["name"], m["mgmt_ip"]) for m in same]))
        conn.execute("DELETE FROM devices WHERE id = ?", (c["id"],))
        conn.execute("DELETE FROM interfaces WHERE device_id = ?", (c["id"],))


def _merge_group(conn: sqlite3.Connection, rows: list[sqlite3.Row],
                 canon: str) -> None:
    # freshest row within a source wins ties — a stable sort on priority alone
    # would make the OLDEST row primary (rowid order), freezing every field at
    # its first-seen value for devices that re-duplicate each poll
    rows = sorted(rows, key=lambda r: (
        SOURCE_PRIORITY.index(r["source"]) if r["source"] in SOURCE_PRIORITY else 99,
        -(r["last_seen"] or 0)))
    primary, rest = rows[0], rows[1:]

    merged = dict(primary)
    # placeholder values ("generic" OS on ping-only LibreNMS devices, CPU
    # arch as vendor) are not information — don't let them outrank a real
    # value from another source
    JUNK = {"os": {"generic"},
            "vendor": {"amd64", "x86_64", "i386", "generic"}}
    for field, junk in JUNK.items():
        if (merged.get(field) or "").lower() in junk:
            merged[field] = None
    if _is_raw_model_code(merged.get("model")):
        merged["model"] = None
    for r in rest:
        for field in ("mgmt_ip", "vendor", "model", "os", "parent", "serial", "status",
                      "cpus", "mem_bytes"):
            if str(r[field] or "").lower() in JUNK.get(field, set()):
                continue
            if field == "model" and _is_raw_model_code(r[field]):
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
    # temperature likewise: freshest reporter wins; a row without one has
    # no opinion, and the write below keeps the old value when nobody does
    fresh_t = max((r for r in [primary, *rest] if r["temperature"] is not None),
                  key=lambda r: r["last_seen"] or 0, default=None)
    merged["temperature"] = fresh_t["temperature"] if fresh_t is not None else None
    # role: prefer any network role over 'vm'/None
    for r in rest:
        if (merged.get("role") not in NETWORK_ROLES) and r["role"]:
            if r["role"] in NETWORK_ROLES or not merged.get("role"):
                merged["role"] = r["role"]

    # retire duplicates first — one of them may already hold the canonical name
    for r in rest:
        conn.execute("UPDATE OR IGNORE interfaces SET device_id=? WHERE device_id=?",
                     (primary["id"], r["id"]))
        # collisions (same port name on both rows) survive on the duplicate.
        # Identity facts (addresses, MAC, ifindex, description) fill any gap
        # in the primary whatever their age — a device that re-duplicates
        # every poll (LibreNMS naming it by FQDN) would otherwise lose the
        # addresses only another collector writes the moment that collector
        # is skipped or fails. Liveness (status, speed, rates) follows
        # freshness: the newer poll's word wins, an older one has no say.
        for dup in conn.execute(
                "SELECT * FROM interfaces WHERE device_id=?", (r["id"],)).fetchall():
            cur = conn.execute(
                "SELECT * FROM interfaces WHERE device_id=? AND name=?",
                (primary["id"], dup["name"])).fetchone()
            if cur is None:
                continue
            fresher = (dup["last_seen"] or 0) >= (cur["last_seen"] or 0)
            fields = {f: dup[f] for f in ("ifindex", "mac", "description", "ip", "ip6")
                      if dup[f] is not None and (fresher or cur[f] is None)}
            if fresher:
                fields.update({f: dup[f] for f in ("admin_status", "oper_status",
                                                   "speed_bps", "in_bps", "out_bps")
                               if dup[f] is not None})
            if fields:
                conn.execute(
                    "UPDATE interfaces SET " + ", ".join(f"{f}=?" for f in fields)
                    + " WHERE device_id=? AND name=?",
                    (*fields.values(), primary["id"], dup["name"]))
        conn.execute("DELETE FROM interfaces WHERE device_id=?", (r["id"],))
        conn.execute("DELETE FROM devices WHERE id=?", (r["id"],))
    conn.execute(
        "UPDATE devices SET name=?, mgmt_ip=?, vendor=?, model=?, os=?, role=?, "
        "status=?, parent=?, serial=?, cpus=?, mem_bytes=?, "
        "temperature=COALESCE(?, temperature), last_seen=? WHERE id=?",
        (canon, merged.get("mgmt_ip"), merged.get("vendor"), merged.get("model"),
         merged.get("os"), merged.get("role"), merged.get("status"),
         merged.get("parent"), merged.get("serial"), merged.get("cpus"),
         merged.get("mem_bytes"), merged.get("temperature"),
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
        # routes feed the routed view's WAN lookup, port_roles the mirror
        # labels, config_revisions the /configs history — a rename that
        # missed any of them silently broke that feature for the device
        conn.execute("UPDATE OR IGNORE routes SET device=? WHERE device=?", (canon, alias))
        conn.execute("DELETE FROM routes WHERE device=?", (alias,))
        conn.execute("UPDATE OR IGNORE port_roles SET device=? WHERE device=?", (canon, alias))
        conn.execute("DELETE FROM port_roles WHERE device=?", (alias,))
        conn.execute("UPDATE config_revisions SET device=? WHERE device=?", (canon, alias))
        conn.execute("UPDATE OR IGNORE tunnels SET device=? WHERE device=?", (canon, alias))
        conn.execute("DELETE FROM tunnels WHERE device=?", (alias,))
    # known cables are name-keyed too: a rename that missed them would hold
    # a link-down alert on a name no port answers to, until it aged out
    known_cables.rename(conn, lambda n: alias_map.get(n, n))
    # renames can flip a row out of upsert_link's sorted orientation, hiding
    # it from the UNIQUE constraint and from pair-keyed deletes — restore it.
    # A collision with an already-sorted twin merges timestamps (same rule
    # as _rewrite_link_field below): the flipped row is the FRESH sighting,
    # re-reported every poll under the alias, and discarding its last_seen
    # left the twin frozen until the TTL expired a live link (#37).
    for r in conn.execute("SELECT * FROM links").fetchall():
        a = (r["a_device"], r["a_interface"])
        b = (r["b_device"], r["b_interface"])
        if a > b:
            twin = conn.execute(
                "SELECT id FROM links WHERE a_device=? AND a_interface=? "
                "AND b_device=? AND b_interface=? AND source=? AND id != ?",
                (*b, *a, r["source"], r["id"])).fetchone()
            if twin:
                conn.execute(
                    "UPDATE links SET last_seen = MAX(COALESCE(last_seen, 0), ?) "
                    "WHERE id = ?", (r["last_seen"] or 0, twin["id"]))
                conn.execute("DELETE FROM links WHERE id = ?", (r["id"],))
            else:
                conn.execute(
                    "UPDATE links SET a_device=?, a_interface=?, "
                    "b_device=?, b_interface=? WHERE id=?", (*b, *a, r["id"]))


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


def _learned_behind(x: str, macs: set[str], toward_macs: set[str],
                    fdb_at: dict[tuple[str, str], set[str]],
                    clients_of: dict[str, set[str]]) -> bool:
    """Does device x put one of `macs` behind itself? Yes when x's own MAC
    table learned it on a port other than the one facing the asking switch
    (the port where x learned that switch's MACs), or x reports it as its
    own client (an AP's wireless station)."""
    toward = {p for m in toward_macs for p in fdb_at.get((x, m), ())}
    return any(m in clients_of.get(x, ())
               or any(p not in toward for p in fdb_at.get((x, m), ()))
               for m in macs)


def _hung_behind(conn: sqlite3.Connection, ghost: str) -> set[str]:
    """The known devices an inferred switch's links hang behind it."""
    return {r[0] for r in conn.execute(
        "SELECT CASE WHEN a_device = ? THEN b_device ELSE a_device END "
        "FROM links WHERE source = 'fdb-uplink' AND "
        "((a_device = ? AND a_interface = '?') OR (b_device = ? AND b_interface = '?'))",
        (ghost, ghost, ghost))}


def _retract_uplinks(conn: sqlite3.Connection, dev: str, iface: str,
                     keep: str | None) -> None:
    """Withdraw fdb-uplink rows on one switch port that lead anywhere but
    `keep`: the port's MAC table was just read and named its one cable."""
    conn.execute(
        "DELETE FROM links WHERE source = 'fdb-uplink' AND "
        "((a_device = ? AND a_interface = ? AND b_device IS NOT ?) OR "
        " (b_device = ? AND b_interface = ? AND a_device IS NOT ?))",
        (dev, iface, keep, dev, iface, keep))


def _place_endpoints_and_infer(conn: sqlite3.Connection,
                               declared: list[tuple[str | None, str, str]] | None = None,
                               prune_declared: bool = True) -> int:
    """FDB placement, in evidence order:
    1. a port carrying a MAC that belongs to a known device's interface links
       to that device on that interface (catches hypervisor multi-uplinks that
       don't speak LLDP). Several known devices on one port are one managed
       switch with the rest behind it, else an inferred unmanaged switch
       with all of them hung behind it — never one cable per device;
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

    # what each device's own MAC table says, for telling a managed switch
    # from the hosts behind it (see _learned_behind); a mirror port's table
    # describes the whole fabric and says nothing about where a MAC lives
    monitor = _monitor_ports(conn)
    fdb_at: dict[tuple[str, str], set[str]] = {}
    for r in conn.execute("SELECT DISTINCT device, interface, mac FROM fdb"):
        if (r["device"], r["interface"]) not in monitor:
            fdb_at.setdefault((r["device"], canon_mac(r["mac"])),
                              set()).add(r["interface"])
    dev_macs: dict[str, set[str]] = {}
    for m, (d, _) in if_by_mac.items():
        dev_macs.setdefault(d, set()).add(m)
    clients_of: dict[str, set[str]] = {}
    for r in conn.execute("SELECT device, mac FROM endpoints "
                          "WHERE source != 'fdb' AND device IS NOT NULL"):
        clients_of.setdefault(r["device"], set()).add(canon_mac(r["mac"]))

    edge: dict[str, list[tuple[str, str]]] = {}   # mac -> [(device, port)]
    per_port: dict[tuple[str, str], set[str]] = {}
    # DISTINCT: two sources can each own a row for the same triple, but one
    # observation must not count twice (a doubled edge[] entry would break
    # the uniquely-seen endpoint placement below)
    for r in conn.execute("SELECT DISTINCT device, interface, mac FROM fdb"):
        if (r["device"], r["interface"]) in linked or \
                (r["device"], r["interface"]) in down_ports:
            continue
        edge.setdefault(r["mac"], []).append((r["device"], r["interface"]))
        per_port.setdefault((r["device"], r["interface"]), set()).add(r["mac"])

    parent_of = {r["name"]: r["parent"] for r in
                 conn.execute("SELECT name, parent FROM devices").fetchall()}

    inferred = 0
    declared_set = {(dev, iface) for _, dev, iface in (declared or [])}
    declared_names: dict[tuple[str, str], str | None] = {}
    for label, ddev, diface in (declared or []):
        if label:
            # a label naming a real device would hijack it — upsert_device
            # merges by name, so "hyp1=sw1:1/0/5" would rewrite the
            # hypervisor's role. Fall back to the auto name instead.
            row = conn.execute("SELECT role FROM devices WHERE name = ?",
                               (label,)).fetchone()
            if row and row["role"] != "unmanaged-switch":
                label = None
        declared_names[(ddev, diface)] = label
    # Prune stale unmanaged nodes. upsert_link sorts ends, so '?' may be on
    # either side. Two cases:
    #   declared node  whose port left declared_set (or whose label changed)
    #     → no longer authoritative — but only when the declarations were
    #     actually readable this run (prune_declared), or one bad poll with
    #     an unreadable .env would wipe every declared node
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
        expected = (declared_names.get(port)
                    or f"unmanaged@{port[0]}:{port[1]}")
        stale = (prune_declared and r["dsrc"] == "declared"
                 and (port not in declared_set or r["node"] != expected)) or \
                (r["dsrc"] == "inference" and port in declared_set)
        if stale:
            # every link: an inferred switch also hangs known hosts behind it
            conn.execute("DELETE FROM links WHERE a_device = ? OR b_device = ?",
                         (r["node"], r["node"]))
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
            resolved: set[tuple[str, str]] = set()
            # far device -> its MACs here, by its own interfaces or a guest's
            direct: dict[str, set[str]] = {}
            guests: dict[str, set[str]] = {}
            for m in macs:
                hit = if_by_mac.get(canon_mac(m))
                if hit is None or hit[0] == dev:
                    continue
                od, oif = hit
                parent = parent_of.get(od)
                end = (parent, "?") if parent else \
                    (od, oif if port_kind(oif) == "physical" else "?")
                if end[0] != dev:
                    resolved.add(end)
                    (guests if parent else direct).setdefault(
                        end[0], set()).add(canon_mac(m))
            # A guest's MAC only says where the VM ran when it was read, and
            # a vMotion between the switch's table and vCenter's leaves it on
            # the old host's port. A device's own MAC on the port outranks
            # that: a host seen there only through a guest is no second
            # device and gets no cable. With no own MAC at all, guest
            # evidence links each host it names, as before.
            if direct:
                resolved = {e for e in resolved if e[0] in direct}
            seen_by = direct or guests
            ends = set(resolved)
            # A port has one cable. Several known devices behind it are
            # either one managed switch and what hangs off it (it learned
            # the others on its own downlinks: link to it alone), or a dumb
            # switch with all of them behind it — writing a link per owner
            # drew N cables out of one port (#98).
            ghost = f"unmanaged@{dev}:{iface}"
            if len(direct) > 1:
                near = [x for x in direct
                        if all(_learned_behind(x, ys, dev_macs.get(dev, set()),
                                               fdb_at, clients_of)
                               for y, ys in direct.items() if y != x)]
                if len(near) == 1:
                    resolved = {e for e in resolved if e[0] == near[0]}
                    _retract_uplinks(conn, dev, iface, keep=near[0])
                else:
                    resolved = set()
            elif len(seen_by) == 1:
                # an inferred dumb switch whose other hosts went quiet (their
                # MACs aged out of the table) stays until its evidence does
                hung = _hung_behind(conn, ghost)
                if len(hung) > 1 and hung >= set(seen_by):
                    resolved = set()
            if seen_by and not resolved:
                plain = [m for m in macs if canon_mac(m) not in self_macs]
                db.upsert_device(conn, name=ghost, source="inference",
                                 role="unmanaged-switch", status="up",
                                 model=f"~{len(macs)} MACs behind {dev} {iface}")
                db.upsert_link(conn, a_device=dev, a_interface=iface,
                               b_device=ghost, b_interface="?", source="fdb-inference")
                _retract_uplinks(conn, dev, iface, keep=None)
                for owner_dev, owner_if in ends:
                    db.upsert_link(conn, a_device=ghost, a_interface="?",
                                   b_device=owner_dev, b_interface=owner_if,
                                   source="fdb-uplink")
                for mac in plain:
                    db.upsert_endpoint(conn, mac=mac, source="fdb", device=ghost,
                                       interface="?")
                inferred += 1
                continue
            for owner_dev, owner_if in resolved:
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
            "SELECT * FROM links WHERE source IN ('lldp', 'unifi', 'vsphere-hint')").fetchall():
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

    # 'unifi' is the controller relaying its own LLDP discovery — stronger
    # than a hypervisor's intermittent CDP hint, weaker than the device-level
    # protocol (when LibreNMS hears the same cable over LLDP, one cable wins)
    drop_where("unifi", port_set(("lldp",)))

    drop_where("vsphere-hint", port_set(("lldp", "unifi")))
    drop_where("fdb-uplink", port_set(("lldp", "unifi", "vsphere-hint", "declared")))

    # an INFERRED unmanaged switch can't share a port with a real link: once
    # lldp/hint/declared/uplink claims the port (e.g. a hypervisor came up and
    # its VM MACs, briefly ownerless, had spawned a ghost switch), retire the
    # inferred device and its link. Operator-declared unmanaged switches are
    # exempt — the .env is their authority.
    real = port_set(("lldp", "unifi", "vsphere-hint", "declared", "fdb-uplink"))
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
            # with the hosts it hung behind itself (fdb-uplink from '?')
            conn.execute("DELETE FROM links WHERE a_device = ? OR b_device = ?",
                         (ghost, ghost))
            conn.execute("DELETE FROM devices WHERE name = ?", (ghost,))
            conn.execute("DELETE FROM endpoints WHERE device = ?", (ghost,))

    # Second pass, by device pair rather than by port, and last on purpose.
    # A controller stores the operator's port label ("SFP1 - core (uplink)")
    # where LibreNMS reads the SNMP ifName ("0/9"), so the port-set check
    # above can't tell that the two rows are one cable. When lldp reports at
    # least as many links between a pair as unifi does, the unifi rows are
    # that cable seen twice and go; two lldp plus two unifi is two real
    # cables seen by both, and stays two. This runs after the weaker-source
    # passes so the unifi row still claims its labeled port against
    # fdb-uplink inference and ghost switches before it is retired (#56).
    lldp_pairs = Counter(
        (r["a_device"], r["b_device"]) for r in conn.execute(
            "SELECT a_device, b_device FROM links WHERE source = 'lldp'"))
    unifi_by_pair: dict[tuple[str, str], list[int]] = {}
    for r in conn.execute(
            "SELECT id, a_device, b_device FROM links WHERE source = 'unifi'"):
        unifi_by_pair.setdefault((r["a_device"], r["b_device"]), []).append(r["id"])
    for pair, ids in unifi_by_pair.items():
        if lldp_pairs[pair] >= len(ids):
            conn.executemany("DELETE FROM links WHERE id = ?", [(i,) for i in ids])


EVIDENCE_TTL = 2 * 3600  # ~24 missed 5-minute polls
# ...but a source is only allowed to expire evidence it actually re-reports.
# An ESXi CDP hint is valid for ~60s after each advertisement, so vSphere
# legitimately returns nothing for a live uplink on most polls; holding it to
# the same clock as LLDP made real links blink out and back every couple of
# hours. Absence of an intermittent report is not evidence of absence.
SOURCE_TTL = {"vsphere-hint": 24 * 3600}


QUIET_STATUS = ("down", "notresponding", "disabled")

# Endpoints are observations (ARP, leases, controller clients, the IPAM
# address book), and every writer refreshes its rows each poll — so a row
# this old is a device that left, an address book entry deleted from IPAM,
# or a collector that was unconfigured. /drift must not treat it as a live
# sighting. Generous next to EVIDENCE_TTL because endpoints come and go by
# nature (a laptop's week off is not a topology change).
ENDPOINT_TTL = 7 * 86400


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
        # the ghost's placed endpoints too — the other retirement paths do
        # this, and nothing re-places a MAC that's absent from fdb
        conn.execute("DELETE FROM endpoints WHERE device = ?", (r["name"],))
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
                # ...and its link-down memory (#79): undeclaring is how a
                # declared cable is retired, unless discovery still states it
                ends = known_cables.ends_of(r["a_device"], r["a_interface"],
                                            r["b_device"], r["b_interface"])
                if not conn.execute(
                        "SELECT 1 FROM links WHERE source IN ('lldp', 'unifi') AND "
                        "((a_device=? AND a_interface=? AND b_device=? AND b_interface=?) "
                        "OR (a_device=? AND a_interface=? AND b_device=? AND b_interface=?))",
                        (*ends[0], *ends[1], *ends[1], *ends[0])).fetchone():
                    known_cables.forget(conn, ends)
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
            "AND l.source IN ('lldp', 'unifi', 'vsphere-hint'))", (*a, *b, *a, *b))


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
            "SELECT * FROM links WHERE source IN ('lldp', 'unifi', 'vsphere-hint')"):
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
    keys = {r["name"]: _identity_key(r)
            for r in conn.execute("SELECT * FROM devices").fetchall()}
    _retire_stale_aliases(conn, keys, set(seed_aliases or {}))
    _retire_merge_survivors(conn)
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in conn.execute("SELECT * FROM devices").fetchall():
        groups.setdefault(_identity_key(r), []).append(r)
    for key, rows in groups.items():
        _merge_group(conn, rows, key)
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
    inferred = _place_endpoints_and_infer(conn, declared_unmanaged,
                                          prune_declared=prune_declared)
    _drop_flooded_neighbors(conn)
    _drop_superseded_inference(conn)
    conflicts = _check_declarations(conn, declared_links or [])
    db.set_state(conn, "declaration_conflicts", json.dumps(conflicts))
    expired = _expire_stale_evidence(conn)
    # after expiry, so "stated now" means what the map draws now; the record
    # outlives an expired row, which is the point (#79)
    known_cables.record(conn, db.now())
    tagged = _apply_vnic_vlans(conn)
    _propagate_vlans(conn, vlan_filters)
    # housekeeping: raw payloads are a debugging window, not an archive
    conn.execute("DELETE FROM raw_payloads WHERE fetched_at < ?",
                 (db.now() - 7 * 86400,))
    # ...and rate samples keep a week (the load view's peak reads a day).
    # This is the cycle's housekeeping, not one source's: the prune lived
    # in the librenms collector until UniFi became a second rate writer
    # (#53), and a site fed by UniFi alone would have kept every sample.
    conn.execute("DELETE FROM rate_history WHERE ts < ?",
                 (db.now() - 7 * 86400,))
    # ...and alert history keeps a quarter, capped by count (ADR-0003)
    alerting.prune_history(conn)
    # ...and stale endpoint observations age out (see ENDPOINT_TTL): live
    # ones are re-upserted every poll by whichever source still sees them
    conn.execute("DELETE FROM endpoints WHERE COALESCE(last_seen, 0) < ?",
                 (db.now() - ENDPOINT_TTL,))
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
