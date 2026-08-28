"""The shared check rules: what deserves attention, and since when.

This module is the one place the attention rules live — the Overview's
attention list, the /alerts page, and /drift all render what it computes,
and the phase-6 alerting engine (#22) is meant to grow out of it rather
than beside it. It deliberately imports no web framework, so the poller
can call `record_first_seen` without dragging FastAPI in.

Every item carries a stable `key` (its identity across polls), a
`category`, a `severity`, and — once the poll path has seen it —
`first_seen`. First-seen is recorded at poll time, not page-view time:
"when did patchbay first notice" is a fact about the polling, and a page
that nobody looked at for a week must not reset it.
"""
from __future__ import annotations

import ipaddress
import json
import sqlite3

from . import db

STALE_MIN = 15  # same rule the top bar uses

# category -> the short label the summary strip and filters show
CATEGORIES = {"link": "slow links", "ipam": "IPAM", "source": "sources"}

_FIRST_SEEN_KEY = "alert_first_seen"


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


def speed_tier(bps: int | None) -> str:
    """"" | "slow" (<=100M) | "vslow" (<=10M) — the one shared threshold the
    map's edge styling (`edge_speed`) and the slow-link check both apply, so
    a link that reads "slow" on the map reads the same way here."""
    if not bps:
        return ""
    return "vslow" if bps <= 10_000_000 else "slow" if bps <= 100_000_000 else ""


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
    verified. Device state is deliberately absent: the Overview's cards ARE
    the device-state UI. Anything here can be silenced by declaring it
    expected (PATCHBAY_EXPECT)."""
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
                "key": "source:stale",
                "category": "source",
                "severity": "warn",
                "text": f"stale source{'' if len(stale) == 1 else 's'}: {named}",
                "href": "/ops",
            })

    items.sort(key=lambda i: i["severity"] != "crit")   # crit first, order kept
    return items, checked


def _stored_first_seen(conn: sqlite3.Connection) -> dict[str, float]:
    raw = db.get_state(conn, _FIRST_SEEN_KEY)
    if not raw:
        return {}
    try:
        seen = json.loads(raw)
        return seen if isinstance(seen, dict) else {}
    except ValueError:
        return {}


def stamp_first_seen(conn: sqlite3.Connection, items: list[dict]) -> None:
    """Read-only: annotate items with the poll-recorded first-seen time.
    An item the poll has not recorded yet reads as just-noticed (None)."""
    seen = _stored_first_seen(conn)
    for it in items:
        it["first_seen"] = seen.get(it["key"])


def record_first_seen(conn: sqlite3.Connection, settings) -> None:
    """Poll-path bookkeeping: a new item gets first_seen = now, a still-firing
    one keeps its timestamp, and a cleared one is forgotten — so a condition
    that clears and returns reads as new, which it is. The caller owns the
    transaction, same contract as the collectors."""
    items, _ = attention_items(conn, settings)
    seen = _stored_first_seen(conn)
    now = db.now()
    seen = {it["key"]: seen.get(it["key"], now) for it in items}
    db.set_state(conn, _FIRST_SEEN_KEY, json.dumps(seen))
