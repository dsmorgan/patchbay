"""The shared check rules: what deserves attention, and since when.

This module is the one place the attention rules live — the Overview's
attention list, the /alerts page, and /drift all render what it computes,
and the alert engine (`alerting.py`, ADR-0003) evaluates the same items
rather than a rule set of its own. It deliberately imports no web
framework, so the poller can run the rules without dragging FastAPI in.

Every item carries a stable `key` (its identity across polls), the `rule`
that raised it (a row in `alerting.RULES`), a `category`, a `severity`,
and — once the poll path has seen it — `first_seen`. First-seen is the
alert's `raised_at`, recorded at poll time, not page-view time: "when did
patchbay first notice" is a fact about the polling, and a page that nobody
looked at for a week must not reset it.
"""
from __future__ import annotations

import ipaddress
import json
import re
import sqlite3

from . import db

STALE_MIN = 15  # same rule the top bar uses

# category -> the short label the summary strip and filters show. The
# Overview hides `device`: its cards are the device-state UI.
CATEGORIES = {"device": "devices", "link": "links", "gateway": "gateways",
              "ipam": "IPAM", "source": "sources", "config": "config changes",
              "tunnel": "tunnels"}

# Rule parameter defaults (ADR-0003 Decision 4). They live here, beside the
# rules that read them; alerting.RULES seeds them into alert_rules, and a
# site's edits there override them.
DEVICE_DOWN_ROLES = ["switch", "ap", "firewall", "router", "hypervisor"]
GATEWAY_LOSS_PCT = 5.0

# Link sources that state a cable rather than infer one. A port that an
# inference put on the map going down says nothing reliable about a cable.
STATED_LINK_SOURCES = ("lldp", "unifi", "declared")

# ifOperStatus values that mean the cable carries nothing. `notPresent`
# and `dormant` describe hardware and power states, not a pulled cable.
PORT_DOWN_STATES = {"down", "lowerlayerdown"}

# Explicit "this gateway is gone" states: pfSense's mapped `down` and
# OPNsense's translated `Offline`. Unknown and pending mean the monitor has
# no data yet, which the loss threshold and stale-source rule cover better
# than a critical alert would.
GATEWAY_DOWN_STATES = {"down", "offline"}

# How long an event (config changed, snapshot failed) stays on the attention
# list. The alert engine notifies it once; the list keeps it readable for a
# day so the person who missed the notification still sees it.
EVENT_WINDOW_S = 24 * 3600

# consecutive failed dispatch cycles before an alert transport is itself an
# item (ADR-0003 Decision 1): one blip is a retry, three is a dead receiver
TRANSPORT_FAILURES_ALERT = 3


def human_speed(bps) -> str:
    if not bps:
        return "-"
    return f"{bps / 1e9:g}G" if bps >= 1_000_000_000 else f"{bps / 1e6:g}M"


def human_age(mins) -> str:
    """Data age: minutes while they're readable, then hours, then days —
    "967 min ago" is arithmetic homework, "16 h ago" is a fact. The header,
    the stale-source item, and base.html's client-side ticker all apply the
    same breakpoints."""
    m = round(mins)
    if m < 60:
        return f"{m} min"
    if m < 2880:
        return f"{m / 60:.0f} h"
    return f"{m / 1440:.0f} d"


def source_ages(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute(
        "SELECT source, (strftime('%s','now') - MAX(fetched_at)) / 60.0 AS mins "
        "FROM raw_payloads GROUP BY source"
    ).fetchall()
    return {r["source"]: round(r["mins"], 1) for r in rows}


# a device nobody has reported for this long is stale whatever its last
# status said — the same window that ages out inferred links (normalize
# EVIDENCE_TTL), so "stale" means one thing across the model
DEVICE_STALE_S = 2 * 3600

# states that mean "not on the network right now" — the routed view's
# _INACTIVE set, spelled out here so the rail and the map count alike
DOWN_STATES = {"down", "disabled", "off", "poweredoff", "notresponding"}


def device_totals(conn: sqlite3.Connection) -> dict[str, int]:
    """The rail's device totals (#46): every device patchbay knows (inferred
    unmanaged switches excluded — they are a guess, not a device), split
    into up / down / stale. Stale outranks status: a box whose collector
    stopped reporting it two hours ago is not "up", whatever it said last.
    Unknown states count in the total only."""
    cutoff = db.now() - DEVICE_STALE_S
    tot = {"total": 0, "up": 0, "down": 0, "stale": 0}
    for r in conn.execute(
            "SELECT status, last_seen FROM devices "
            "WHERE role IS NULL OR role != 'unmanaged-switch'"):
        tot["total"] += 1
        if (r["last_seen"] or 0) < cutoff:
            tot["stale"] += 1
        elif (r["status"] or "").lower() == "up":
            tot["up"] += 1
        elif (r["status"] or "").lower() in DOWN_STATES:
            tot["down"] += 1
    return tot


def speed_tier(bps: int | None) -> str:
    """"" | "slow" (<=100M) | "vslow" (<=10M) — the one shared threshold the
    map's edge styling (`edge_speed`) and the slow-link check both apply, so
    a link that reads "slow" on the map reads the same way here."""
    if not bps:
        return ""
    return "vslow" if bps <= 10_000_000 else "slow" if bps <= 100_000_000 else ""


def rule_params(conn: sqlite3.Connection, name: str, defaults: dict) -> dict:
    """A rule's parameters: the catalog defaults under the site's stored
    edits (alert_rules.params). A missing row or unreadable JSON means the
    defaults, so a rule never stops checking because its row is damaged."""
    row = conn.execute("SELECT params FROM alert_rules WHERE name = ?",
                       (name,)).fetchone()
    try:
        stored = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        stored = {}
    return {**defaults, **(stored if isinstance(stored, dict) else {})}


def _roles(value) -> set[str]:
    if isinstance(value, str):
        value = value.split(",")
    return {str(r).strip().lower() for r in value or () if str(r).strip()}


def _loss_pct(value) -> float | None:
    """`0.0 %`, `12%`, or a bare number; None when the source said nothing
    parseable, which is no opinion rather than zero loss."""
    m = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(m.group()) if m else None


def ip_sort_key(ip: str):
    try:
        a = ipaddress.ip_address(ip)
        return (a.version, int(a))
    except ValueError:
        return (99, 0)


def ipam_link(settings, row) -> str | None:
    """Deep link into the IPAM's own UI, when it gave us its object ids.

    phpIPAM address pages want three internal ids; an IPAM that doesn't
    populate them (or a future NetBox collector) simply gets no link.
    """
    base = (settings.ipam_url or "").rstrip("/")
    base = base.removesuffix("/api")
    if base and row["ipam_id"] and row["ipam_subnet_id"] and row["ipam_section_id"]:
        return (f"{base}/index.php?page=subnets&section={row['ipam_section_id']}"
                f"&subnetId={row['ipam_subnet_id']}&sPage=address-details"
                f"&ipaddrid={row['ipam_id']}")
    return None


def drift_report(conn: sqlite3.Connection, settings) -> dict:
    """Shared by /drift and the IPAM check, so "is IPAM in sync?" is one
    query. /drift renders this dict unchanged (plus its own `ages`); the
    check uses only `len(conflicts)` and `have_ipam`."""
    from .normalize import canon_mac

    nets = []
    for r in conn.execute("SELECT cidr FROM subnets"):
        try:
            nets.append((ipaddress.ip_network(r["cidr"], strict=False), r["cidr"]))
        except ValueError:
            pass

    def subnet_of(ip: str) -> str | None:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return None
        best = None
        for net, cidr in nets:
            if a in net and (best is None or net.prefixlen > best[0].prefixlen):
                best = (net, cidr)
        return best[1] if best else None

    ipam = {r["ip"]: r for r in conn.execute("SELECT * FROM ipam_addresses")}
    # best live record per IP (prefer one that knows a hostname)
    observed: dict[str, sqlite3.Row] = {}
    for r in conn.execute(
            "SELECT * FROM endpoints WHERE ip IS NOT NULL ORDER BY last_seen"):
        cur = observed.get(r["ip"])
        if cur is None or (not cur["hostname"] and r["hostname"]):
            observed[r["ip"]] = r

    def short(h: str | None) -> str:
        return (h or "").split(".")[0].lower()

    undocumented, external, conflicts, in_sync = [], [], [], 0
    for ip in sorted(observed, key=ip_sort_key):
        e, doc = observed[ip], ipam.get(ip)
        if doc is None:
            entry = {
                "ip": ip, "hostname": e["hostname"], "mac": e["mac"],
                "seen_at": (f"{e['device']} {e['interface'] or ''}".strip()
                            if e["device"] else e["source"]),
                "subnet": subnet_of(ip),
            }
            # outside every documented subnet = WAN-side neighbors (dynamic
            # carrier addresses) — report as info, not as drift
            (undocumented if entry["subnet"] else external).append(entry)
            continue
        clean = True
        ipam_h, live_h = short(doc["hostname"]), short(e["hostname"])
        # prefix match = same name truncated somewhere (DHCP option 12 is
        # commonly clipped), not drift
        hostname_ok = (not ipam_h or not live_h
                       or ipam_h.startswith(live_h) or live_h.startswith(ipam_h))
        if not hostname_ok:
            conflicts.append({"ip": ip, "kind": "hostname", "link": ipam_link(settings, doc),
                              "ipam": doc["hostname"], "live": e["hostname"]})
            clean = False
        if doc["mac"] and e["mac"] and canon_mac(doc["mac"]) != canon_mac(e["mac"]):
            conflicts.append({"ip": ip, "kind": "mac", "link": ipam_link(settings, doc),
                              "ipam": doc["mac"], "live": e["mac"]})
            clean = False
        in_sync += clean
    # "documented but quiet" only matters for addresses IPAM claims are
    # fixed assets — DHCP-pool rows going quiet is normal, not drift
    unseen, n_dhcp_quiet = [], 0
    for ip in sorted(ipam, key=ip_sort_key):
        if ip in observed:
            continue
        if (ipam[ip]["state"] or "") == "dhcp":
            n_dhcp_quiet += 1
        else:
            unseen.append({**dict(ipam[ip]), "link": ipam_link(settings, ipam[ip])})
    have_ipam = bool(ipam)
    return {
        "undocumented": undocumented, "external": external, "unseen": unseen,
        "n_dhcp_quiet": n_dhcp_quiet, "conflicts": conflicts,
        "in_sync": in_sync, "have_ipam": have_ipam,
    }


def attention_items(conn: sqlite3.Connection, settings) -> tuple[list[dict], list[str]]:
    """One flat, ordered list of items worth a look, each linking to the page
    that owns the answer — not pre-categorized cards (issue #13). Rules only
    speak when they can actually check something, so `checked` names only
    the checks that ran and the all-clear line can only claim what it
    verified. Device state is category `device`, which the Overview filters
    out because its cards ARE the device-state UI; /alerts and the alert
    channel show it. Anything here can be silenced by declaring it expected
    (PATCHBAY_EXPECT)."""
    items: list[dict] = []
    checked: list[str] = []

    # slow-link: the better-known end's speed through speed_tier() — a link
    # with no known speed is not slow, same rule the map uses. A port (or a
    # whole device) declared expected keeps its legitimately-slow link quiet.
    links = conn.execute("SELECT * FROM links ORDER BY a_device, a_interface").fetchall()
    if links:
        checked.append("no unexpected slow links")
        speed_of: dict[tuple[str, str], int] = {}
        for r in conn.execute(
                "SELECT d.name AS dev, i.name AS iface, i.speed_bps FROM interfaces i "
                "JOIN devices d ON d.id = i.device_id WHERE i.speed_bps > 0"):
            speed_of[(r["dev"], r["iface"])] = r["speed_bps"]
        for l in links:
            bps = (speed_of.get((l["a_device"], l["a_interface"]))
                   or speed_of.get((l["b_device"], l["b_interface"])))
            tier = speed_tier(bps)
            if not tier:
                continue
            names = {l["a_device"], l["b_device"],
                     f"{l['a_device']}:{l['a_interface']}",
                     f"{l['b_device']}:{l['b_interface']}"}
            if names & settings.expected:
                continue
            items.append({
                "rule": "slow-link",
                "key": f"link:{l['a_device']}:{l['a_interface']}:"
                       f"{l['b_device']}:{l['b_interface']}",
                "category": "link",
                "severity": "crit" if tier == "vslow" else "warn",
                "text": f"{l['a_device']} {l['a_interface']} ↔ "
                        f"{l['b_device']} {l['b_interface']} runs at {human_speed(bps)}",
                "href": f"/topology?focus={l['a_device']}",
            })

    # drift: only when the site has IPAM at all — no IPAM, no claim. One
    # line; /drift owns the detail.
    report = drift_report(conn, settings)
    if report["have_ipam"]:
        checked.append("IPAM in sync")
        n = len(report["conflicts"])
        if n:
            items.append({
                "rule": "ipam-drift",
                "key": "ipam:conflicts",
                "category": "ipam",
                "severity": "warn",
                "text": f"{n} IPAM conflict{'' if n == 1 else 's'} — "
                        f"records and the network disagree",
                "href": "/drift",
            })

    # stale-source: any source whose newest payload is older than STALE_MIN —
    # same age the top bar already flags per-source. One line naming them.
    ages = source_ages(conn)
    if ages:
        checked.append("every source fresh")
        stale = sorted(((s, m) for s, m in ages.items() if m > STALE_MIN),
                       key=lambda x: -x[1])
        if stale:
            named = ", ".join(f"{s} ({human_age(m)})" for s, m in stale)
            items.append({
                "rule": "stale-source",
                "key": "source:stale",
                "category": "source",
                "severity": "warn",
                "text": f"stale source{'' if len(stale) == 1 else 's'}: {named}",
                "href": "/ops",
            })

    now = db.now()
    items += _device_down(conn, settings, now)
    items += _link_down(conn, settings, checked)
    items += _gateway_degraded(conn, settings, now, checked)
    items += _config_changed(conn, settings, now)
    items += _snapshot_failed(conn, now)
    items += _expected_tunnel_missing(conn, now, checked)

    # transport-failing: an alert transport whose last three dispatch cycles
    # failed. Silence about a dead receiver is the one failure the receiver
    # cannot report itself, so the attention list carries it. last_error is
    # redacted at the source (transports._redact); the URL never reaches it.
    transports = conn.execute(
        "SELECT id, name, failures, last_error FROM alert_transports "
        "WHERE enabled = 1 ORDER BY id").fetchall()
    if transports:
        checked.append("alert transports delivering")
        for t in transports:
            if t["failures"] >= TRANSPORT_FAILURES_ALERT:
                items.append({
                    "rule": "transport-failing",
                    "key": f"source:transport:{t['id']}",
                    "category": "source",
                    "severity": "warn",
                    "text": f"alert transport {t['name']} failing "
                            f"({t['failures']} polls): {t['last_error'] or 'no response'}",
                    "href": "/alerts?tab=transports",
                })

    items = _apply_rule_settings(conn, items)
    order = {"crit": 0, "warn": 1}
    items.sort(key=lambda i: order.get(i["severity"], 2))  # crit first, order kept
    return items, checked


def _apply_rule_settings(conn, items: list[dict]) -> list[dict]:
    """The site's Rules-tab edits, applied here so every surface agrees: a
    disabled rule shows nowhere, and a severity override is the severity
    the attention list, /alerts, and the alert channel all see."""
    rules = {r["name"]: r for r in conn.execute(
        "SELECT name, enabled, severity FROM alert_rules")}
    out = []
    for it in items:
        r = rules.get(it.get("rule"))
        if r is not None and not r["enabled"]:
            continue
        if r is not None and r["severity"]:
            it["severity"] = r["severity"]
        out.append(it)
    return out


def _device_down(conn, settings, now: float) -> list[dict]:
    """device-down: a device in a watched role reports a down state or has
    not been reported for DEVICE_STALE_S, the same definition the rail's
    totals and the alert engine's inhibition use. No `checked` line: the
    Overview hides this category, so its all-clear must not claim it."""
    roles = _roles(rule_params(conn, "device-down",
                               {"roles": DEVICE_DOWN_ROLES})["roles"])
    cutoff = now - DEVICE_STALE_S
    out = []
    for r in conn.execute("SELECT name, role, status, last_seen FROM devices "
                          "ORDER BY name"):
        if (r["role"] or "").lower() not in roles or r["name"] in settings.expected:
            continue
        status = (r["status"] or "").lower()
        # disabled in the NMS on purpose: a decision, not a fault. It stays
        # in DOWN_STATES, so the engine still inhibits its cables.
        if status == "disabled":
            continue
        if (r["last_seen"] or 0) < cutoff:
            what = (f"has not been reported for "
                    f"{human_age((now - (r['last_seen'] or 0)) / 60)}"
                    if r["last_seen"] else "has never been reported")
        elif status in DOWN_STATES:
            what = f"is {status}"
        else:
            continue
        out.append({
            "rule": "device-down", "key": f"device:{r['name']}",
            "category": "device", "severity": "crit",
            "text": f"{r['role']} {r['name']} {what}",
            "href": f"/device/{r['name']}",
        })
    return out


def _link_down(conn, settings, checked: list[str]) -> list[dict]:
    """link-down: a stated cable with a port reporting oper down. One item
    per cable, naming both ends in `ports`, so the alert engine's fixed
    inhibition holds it while either end's device is down: an unplugged
    switch is one device-down alert, not one per cable. The rule itself
    does not test the device, because the engine holds an open alert under
    inhibition rather than clearing it."""
    links = conn.execute(
        "SELECT * FROM links WHERE source IN (%s) ORDER BY a_device, a_interface"
        % ",".join("?" * len(STATED_LINK_SOURCES)), STATED_LINK_SOURCES).fetchall()
    if not links:
        return []
    checked.append("no stated link down")
    port = {(r["dev"], r["iface"]): r for r in conn.execute(
        "SELECT d.name AS dev, i.name AS iface, i.oper_status, i.admin_status "
        "FROM interfaces i JOIN devices d ON d.id = i.device_id")}
    out, seen = [], set()
    for l in links:
        ends = sorted([(l["a_device"], l["a_interface"]),
                       (l["b_device"], l["b_interface"])])
        # an admin-down port was shut on purpose; that is a change, not a fault
        down = [e for e in ends if e in port
                and (port[e]["oper_status"] or "").lower() in PORT_DOWN_STATES
                and (port[e]["admin_status"] or "").lower() != "down"]
        # sorted ends: lldp and declared rows for one cable share one key
        key = "link-down:" + ":".join(f"{d}:{i}" for d, i in ends)
        if not down or key in seen:
            continue
        names = {d for d, _ in ends} | {f"{d}:{i}" for d, i in ends}
        if names & settings.expected:
            continue
        seen.add(key)
        (ad, ai), (bd, bi) = ends
        out.append({
            "rule": "link-down", "key": key, "category": "link", "severity": "warn",
            "text": f"{ad} {ai} ↔ {bd} {bi} is down ("
                    + ", ".join(f"{d} {i}" for d, i in down) + " reports down)",
            "href": f"/topology?focus={down[0][0]}",
            "ports": ends,
        })
    return out


def _gateway_degraded(conn, settings, now: float, checked: list[str]) -> list[dict]:
    """gateway-degraded: crit when the firewall says a gateway is down, warn
    when its loss exceeds the threshold. One key per gateway, so a gateway
    that goes from lossy to down is one alert whose severity rises. A row
    the firewall stopped refreshing is the stale-source rule's to report."""
    gws = conn.execute("SELECT * FROM gateways WHERE last_seen >= ? ORDER BY name",
                       (now - DEVICE_STALE_S,)).fetchall()
    if not gws:
        return []
    checked.append("gateways up")
    try:
        limit = float(rule_params(conn, "gateway-degraded",
                                  {"loss": GATEWAY_LOSS_PCT})["loss"])
    except (TypeError, ValueError):
        limit = GATEWAY_LOSS_PCT
    out = []
    for g in gws:
        if g["name"] in settings.expected:
            continue
        status = (g["status"] or "").lower()
        loss = _loss_pct(g["loss"])
        if status in GATEWAY_DOWN_STATES:
            sev, what = "crit", f"is {g['status']}"
        elif loss is not None and loss > limit:
            sev, what = "warn", f"loses {loss:g} % (threshold {limit:g} %)"
        else:
            continue
        out.append({
            "rule": "gateway-degraded", "key": f"gateway:{g['name']}",
            "category": "gateway", "severity": sev,
            "text": f"gateway {g['name']} {what}", "href": "/",
        })
    return out


# --- expected-tunnel-missing (#63) -------------------------------------------
# A declared tunnel (expected_tunnels) is missing when its row is absent, its
# status is not "up" (WireGuard "idle" = stale handshake counts as not up),
# or the row was last refreshed over DEVICE_STALE_S ago. A stale row is
# missing: nothing has confirmed the tunnel since, and the firewall's own
# down or stale state is reported by its own rules. Undeclared tunnels never
# alert.
def _expected_tunnel_missing(conn, now: float, checked: list[str]) -> list[dict]:
    from . import expected_tunnels

    declared = expected_tunnels.load(conn)
    if not declared:
        return []
    checked.append("expected tunnels up")
    have = {(r["device"], r["type"], r["name"]): r
            for r in conn.execute("SELECT device, type, name, status, last_seen FROM tunnels")}
    out = []
    for e in declared:
        row = have.get((e["device"], e["type"], e["name"]))
        if row is None:
            why = "is not reported"
        elif (row["last_seen"] or 0) < now - DEVICE_STALE_S:
            why = "was last reported over two hours ago"
        elif (row["status"] or "").lower() != "up":
            why = f"is {row['status'] or 'not up'}"
        else:
            continue
        out.append({
            "rule": "expected-tunnel-missing",
            "key": f"tunnel:{e['device']}:{e['type']}:{e['name']}",
            "category": "tunnel", "severity": "warn",
            "text": f"expected {e['type']} tunnel {e['name']} on {e['device']} {why}",
            "href": "/routed",
        })
    return out


def _config_changed(conn, settings, now: float) -> list[dict]:
    """config-changed (event): a stored revision that has a predecessor, so
    a device's first capture is a baseline, not a change. The key carries
    the revision id and its hash: ids can be reused after a delete, and a
    config reverting to an earlier state repeats an earlier hash."""
    out = []
    for r in conn.execute(
            "SELECT id, device, fetched_at, sha, message, author "
            "FROM config_revisions cr WHERE fetched_at >= ? AND EXISTS ("
            "  SELECT 1 FROM config_revisions p WHERE p.device = cr.device "
            "  AND (p.fetched_at < cr.fetched_at "
            "       OR (p.fetched_at = cr.fetched_at AND p.id < cr.id))) "
            "ORDER BY fetched_at DESC, id DESC", (now - EVENT_WINDOW_S,)):
        if r["device"] in settings.expected:
            continue
        detail = " — ".join(x for x in (r["message"], r["author"]) if x)
        out.append({
            "rule": "config-changed",
            "key": f"config:{r['device']}:{r['id']}:{r['sha'][:12]}",
            "category": "config", "severity": "info",
            "text": f"{r['device']} config changed" + (f": {detail}" if detail else ""),
            "href": f"/configs/{r['device']}", "at": r["fetched_at"],
        })
    return out


def _snapshot_failed(conn, now: float) -> list[dict]:
    """snapshot-failed (event): each failure the snapshot paths recorded
    (db.record_snapshot_failure), keyed by its own timestamp and sequence
    number."""
    out = []
    for f in reversed(db.snapshot_failures(conn)):
        if f["ts"] < now - EVENT_WINDOW_S:
            continue
        what = "not delivered" if f.get("kind") == "undelivered" else "failed"
        out.append({
            "rule": "snapshot-failed", "key": f"snapshot:{f['ts']:.3f}:{f.get('n', 0)}",
            "category": "source", "severity": "warn",
            "text": f"{f.get('trigger') or 'snapshot'} snapshot {what}: "
                    f"{f.get('error') or 'no detail'}",
            "href": "/snapshots", "at": f["ts"],
        })
    return out


def stamp_first_seen(conn: sqlite3.Connection, items: list[dict]) -> None:
    """Read-only: annotate items with their open alert's raised_at and state
    (pending or active). An item the poll has not recorded yet reads as
    just-noticed: first_seen None, state None."""
    open_ = {r["key"]: r for r in conn.execute(
        "SELECT key, raised_at, state FROM alerts WHERE state != 'cleared'")}
    for it in items:
        row = open_.get(it["key"])
        # an event clears the cycle it fires, so it has no open row; its
        # own occurrence time is when patchbay noticed it
        it["first_seen"] = row["raised_at"] if row else it.get("at")
        it["alert_state"] = row["state"] if row else None
