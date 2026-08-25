"""patchbay web UI — Phase 2: health dashboard + device drill-down.

Server-rendered, reads the SQLite model the collectors maintain. Run with
`patchbay web` (or uvicorn patchbay.web:app).
"""

from __future__ import annotations

import ipaddress
import sqlite3
from importlib import resources
from pathlib import Path

import httpx

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from . import db
from .config import load_settings

TEMPLATES_DIR = Path(__file__).parent / "templates"

from .ports import port_kind

app = FastAPI(title="patchbay")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _build_version() -> str:
    """What's actually running, shown in the page header so 'which version is
    the NAS on?' has an answer.

    A tagged release is just its version: `0.3.0`. Anything else leads with
    the release number and appends the build that produced it —
    `0.3.0+01e851e`, plus `-dirty` when a checkout has uncommitted changes —
    because between tags the version alone can't distinguish two builds, and
    the commit alone doesn't say which release you're near. CI stamps release
    images with the bare version and branch images with the short SHA (via
    PATCHBAY_BUILD); a checkout asks its own git.
    """
    import os

    from . import __version__

    # 'dev' is the Dockerfile's ARG placeholder, not an answer. Rendering it
    # put the word "dev" beside the name for anyone who built the image
    # without passing GIT_SHA, which reads as unfinished software.
    build = os.environ.get("PATCHBAY_BUILD")
    if not build or build == "dev":
        try:
            import subprocess

            def _git(*args: str) -> str:
                return subprocess.run(
                    ["git", "-C", str(Path(__file__).parent), *args],
                    capture_output=True, text=True, timeout=3).stdout.strip()

            build = _git("rev-parse", "--short", "HEAD")
            if build:
                dirty = bool(_git("status", "--porcelain"))
                # a clean checkout sitting exactly on its own release tag IS
                # the release — same rule as a CI release image
                if not dirty and _git("describe", "--tags",
                                      "--exact-match") == f"v{__version__}":
                    return __version__
                if dirty:
                    build += "-dirty"
        except Exception:
            build = ""
    if not build:
        return __version__
    # A build stamp that already names the version replaces it rather than
    # doubling it: CI's release stamp PATCHBAY_BUILD=0.3.0 renders bare, and
    # an explicit 0.3.0+abc123 renders as given.
    return build if build.startswith(__version__) else f"{__version__}+{build}"


BUILD = _build_version()
templates.env.globals["BUILD"] = BUILD


def human_speed(bps) -> str:
    if not bps:
        return "-"
    return f"{bps / 1e9:g}G" if bps >= 1_000_000_000 else f"{bps / 1e6:g}M"


templates.env.filters["speed"] = human_speed


def hardware(d) -> str:
    """Best identity string for a device: LibreNMS puts the hardware name in
    `vendor` for switches (model empty) and arch junk there for servers."""
    junk = {"amd64", "x86_64", "i386", "generic"}
    parts = [v for v in (d["vendor"], d["model"])
             if v and v.lower() not in junk]
    return " ".join(dict.fromkeys(parts))


templates.env.filters["hw"] = hardware

# role icons: the standard diagram vocabulary, drawn as 18x18 stroke paths
# (opposing arrows = switch, bricks = firewall, layers = hypervisor,
#  radio arcs = AP, RJ45 jack = wired host) — shared by the topology map,
# its legend, and the device cards
ICONS = {
    "switch": "M3 6h11 M14 6l-2.5-2.5 M14 6l-2.5 2.5 M15 12H4 M4 12l2.5-2.5 M4 12l2.5 2.5",
    "unmanaged-switch": "M3 6h11 M14 6l-2.5-2.5 M14 6l-2.5 2.5 M15 12H4 M4 12l2.5-2.5 M4 12l2.5 2.5",
    "firewall": "M1 3h16v12H1z M1 9h16 M9 3v6 M5 9v6 M13 9v6",
    "hypervisor": "M9 2l7 3.5L9 9 2 5.5z M2 9l7 3.5 7-3.5 M2 12.5L9 16l7-3.5",
    "ap": "M9 13.2a1.2 1.2 0 100 .01 M5.5 10a5 5 0 017 0 M3 7a8.5 8.5 0 0112 0",
    "host": "M3 2h12v7h-2.5v3H10v2H8v-2H5.5V9H3z M6 2v3 M9 2v3 M12 2v3",
}
templates.env.globals["ICONS"] = ICONS

# The nav rail (base.html renders it). Groups are named for the question a
# page answers, not the mechanism behind it — a rule borrowed from EQDeeps'
# ADR-017: "Network" is what's out there and how it's wired; "Records" is
# what the documentation says and whether the network agrees. A new page
# lands in the group whose question it answers; one that fits neither is the
# signal for a third heading, not a longer list. Ops — how patchbay itself
# is fed and configured — is a utility, and sits in the rail's foot beside
# sign-out rather than among the views. Which entry is lit comes from the
# request path, so drill-downs keep their section lit.
NAV = [
    ("Network", [
        ("/", "Overview", "overview",
         "Every device, live: the fabric, access points, hypervisors and their guests"),
        ("/topology", "Topology", "topology",
         "How it's wired: the physical map, with VLAN and load overlays"),
        ("/vlans", "VLANs", "vlans",
         "Every VLAN and subnet — who routes it, who carries it, what's live in it"),
        ("/patchpanel", "Patch panels", "patchpanel",
         "Panel positions and the switch ports behind them"),
    ]),
    ("Records", [
        ("/drift", "Drift", "drift",
         "IPAM against what the network actually shows"),
        ("/configs", "Configs", "configs",
         "Device configuration history and diffs, from Oxidized"),
    ]),
]
templates.env.globals["NAV"] = NAV

# rail glyphs on the same 18x18 stroke grid as the role icons, so the two
# sets sit together; each is chosen for the question its page answers
# (a wired graph for topology, a tag for VLANs, a panel of ports, two arrows
# exchanged for drift, a file for configs, sliders for ops)
NAV_ICONS = {
    "overview": "M2 2h5.5v5.5H2z M10.5 2H16v5.5h-5.5z M2 10.5h5.5V16H2z M10.5 10.5H16V16h-5.5z",
    "topology": "M7.2 3.5a1.8 1.8 0 103.6 0 1.8 1.8 0 10-3.6 0 "
                "M1.7 14.5a1.8 1.8 0 103.6 0 1.8 1.8 0 10-3.6 0 "
                "M12.7 14.5a1.8 1.8 0 103.6 0 1.8 1.8 0 10-3.6 0 "
                "M8.2 5.1 4.3 12.9 M9.8 5.1l3.9 7.8 M5.3 14.5h7.4",
    "vlans": "M2 2.5h6.5l7.5 7.5-6.5 6.5L2 9z M4.6 5.8a.7.7 0 101.4 0 .7.7 0 10-1.4 0",
    "patchpanel": "M2.5 5.5h13a1 1 0 011 1v5a1 1 0 01-1 1h-13a1 1 0 01-1-1v-5a1 1 0 011-1z "
                  "M4.5 8v2 M7.5 8v2 M10.5 8v2 M13.5 8v2",
    "drift": "M3 5.5h12 M12 2.5l3 3-3 3 M15 12.5H3 M6 9.5l-3 3 3 3",
    "configs": "M4 1.5h7l4 4v11H4z M11 1.5v4h4 M6.5 9h5 M6.5 12h5",
    "ops": "M2.5 4.5h13 M2.5 9h13 M2.5 13.5h13 M6 2.5v4 M12 7v4 M8 11.5v4",
    "signout": "M7 2.5H3.5v13H7 M11 12.5 14.5 9 11 5.5 M6.5 9H14",
    "collapse": "M2 3h14v12H2z M7 3v12 M13 7l-2 2 2 2",
    "expand": "M2 3h14v12H2z M7 3v12 M10.5 7l2 2-2 2",
    "chevrons-left": "M10 4L5.5 9 10 14 M15 4l-4.5 5 4.5 5",
    "chevrons-right": "M8 4l4.5 5L8 14 M3 4l4.5 5L3 14",
}
templates.env.globals["NAV_ICONS"] = NAV_ICONS

from fastapi.staticfiles import StaticFiles

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

from . import auth


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    intercept = auth.gate(request, load_settings())
    return intercept if intercept else await call_next(request)


app.include_router(auth.build_router(templates, load_settings))


def _conn() -> sqlite3.Connection:
    settings = load_settings()
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    # Not optional, and not only for reads: /ops runs a full poll and normalize
    # on this connection, and the pragma is per-connection. Without it, a
    # device retired from the ops page left its interface rows behind while the
    # identical poll from the CLI cleaned up after itself -- two code paths
    # with different integrity guarantees, which is the worst kind.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _age(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute(
        "SELECT source, (strftime('%s','now') - MAX(fetched_at)) / 60.0 AS mins "
        "FROM raw_payloads GROUP BY source"
    ).fetchall()
    return {r["source"]: round(r["mins"], 1) for r in rows}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = _conn()
    try:
        db.init(conn)
        gateways = conn.execute("SELECT * FROM gateways ORDER BY name").fetchall()
        fabric = conn.execute(
            "SELECT * FROM devices WHERE role IN ('switch','firewall') ORDER BY role, name"
        ).fetchall()
        aps = conn.execute(
            "SELECT * FROM devices WHERE role = 'ap' ORDER BY status != 'up', name"
        ).fetchall()
        hypervisors = conn.execute(
            "SELECT * FROM devices WHERE role = 'hypervisor' ORDER BY name"
        ).fetchall()
        # anything with a parent lives on a hypervisor — including guests
        # whose role got promoted (a virtualized firewall is still a VM here)
        vms = conn.execute(
            "SELECT * FROM devices WHERE parent IS NOT NULL "
            "ORDER BY status != 'up', name"
        ).fetchall()
        links = conn.execute("SELECT * FROM links ORDER BY a_device, a_interface").fetchall()
        # every headline number says what it counted, so nobody has to guess
        # what "93 ports" is a count of
        per_device: dict[str, int] = {}
        for r in conn.execute(
                "SELECT d.name AS dev, i.name AS iface FROM interfaces i "
                "JOIN devices d ON d.id = i.device_id"):
            if port_kind(r["iface"]) == "physical":
                per_device[r["dev"]] = per_device.get(r["dev"], 0) + 1
        ep_by_source = {r["source"]: r["n"] for r in conn.execute(
            "SELECT source, COUNT(*) AS n FROM endpoints GROUP BY source")}
        ep_live = conn.execute(
            "SELECT COUNT(*) FROM endpoints WHERE source = 'fdb'").fetchone()[0]
        counts = {
            "ports": sum(per_device.values()),
            "endpoints": conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0],
            "subnets": conn.execute("SELECT COUNT(*) FROM subnets").fetchone()[0],
            "vlans": conn.execute("SELECT COUNT(*) FROM vlans").fetchone()[0],
        }
        detail = {
            "ports": (f"physical ports on {len(per_device)} devices, logical ones "
                      f"(VLAN, LAG, loopback, vmkernel) excluded — "
                      + ", ".join(f"{d} {n}" for d, n in
                                  sorted(per_device.items(), key=lambda x: -x[1]))),
            "endpoints": ("distinct MAC addresses known to any source — "
                          + ", ".join(f"{s} {n}" for s, n in sorted(
                              ep_by_source.items(), key=lambda x: -x[1]))),
            "subnets": "subnets from IPAM and the firewall's own interfaces",
            "vlans": "VLAN IDs seen in any switch config, VLAN database, or IPAM",
        }
        sub = {
            "ports": f"on {len(per_device)} devices",
            "endpoints": f"{ep_live} live on switch ports",
        }
        # A first run has nothing to show. Say why, and what to do about it —
        # a page of zeros with empty headings reads as broken software, and
        # the one useful message ("no collectors configured") is in the
        # poller's log, which is not where anyone looks first.
        import os

        from .collectors import available

        settings = load_settings()
        onboarding = None
        if not (fabric or aps or hypervisors):
            configured = sorted(available(settings))
            onboarding = {
                "configured": configured,
                "env_path": os.environ.get("PATCHBAY_ENV", ".env"),
                "polled": bool(db.get_state(conn, "last_poll")),
            }
        return templates.TemplateResponse(request, "dashboard.html", {
            "gateways": gateways, "fabric": fabric, "aps": aps,
            "hypervisors": hypervisors, "vms": vms, "links": links,
            "counts": counts, "detail": detail, "sub": sub, "ages": _age(conn),
            "onboarding": onboarding,
        })
    finally:
        conn.close()


TOPO_ROLES = ("firewall", "switch", "hypervisor", "ap", "unmanaged-switch")
RANK = {"cloud": -1, "firewall": 0, "switch": 1,
        "hypervisor": 2, "ap": 2, "unmanaged-switch": 2, "host": 2}


# the implicit patch panel when PATCHBAY_PANELS is unset: [n] tags, sized by
# the highest position seen (size 0 = no declared size)
DEFAULT_PANELS = [("panel", 0, r"\[(\d+)\]")]


def build_topology_graph(conn: sqlite3.Connection, settings) -> tuple[str, bool]:
    """Build the topology graph as script-safe JSON (plus whether any 24h
    peak samples exist). Shared by /topology and the snapshot generator."""
    devs = conn.execute(
        f"SELECT * FROM devices WHERE role IN ({','.join('?' * len(TOPO_ROLES))}) "
        "ORDER BY name", TOPO_ROLES).fetchall()
    names = {d["name"] for d in devs}
    raw_edges = [dict(r) for r in conn.execute("SELECT * FROM links").fetchall()
                 if r["a_device"] in names and r["b_device"] in names]
    # firewalls virtualized on a hypervisor hang off it (e.g. fw1 on vmhost2)
    for d in devs:
        if d["parent"] in names:
            raw_edges.append({"a_device": d["name"], "a_interface": "vm",
                              "b_device": d["parent"], "b_interface": "",
                              "source": "virtualized"})

    import json
    import re as _re

    # subtitles / badges
    subs: dict[str, str] = {}
    for d in devs:
        n = d["name"]
        if d["role"] == "ap":
            c = conn.execute("SELECT COUNT(*) FROM endpoints WHERE device=?", (n,)).fetchone()[0]
            subs[n] = f"{c} clients"
        elif d["role"] == "hypervisor":
            up, tot = conn.execute(
                "SELECT SUM(status='up'), COUNT(*) FROM devices WHERE parent=?", (n,)).fetchone()
            vm = f"{up or 0}/{tot or 0} VMs"
            subs[n] = f"{d['mgmt_ip']} · {vm}" if d["mgmt_ip"] else vm
        elif d["role"] == "unmanaged-switch":
            m = _re.search(r"(\d+) MACs", d["model"] or "")
            subs[n] = f"{m.group(1)} MACs" if m else "unknown MACs"
        else:
            subs[n] = d["mgmt_ip"] or d["role"]

    # port description tags mark patch-panel positions (PATCHBAY_PANELS
    # patterns, default "[4]") — carry them onto the map labels: "1/0/6 [4]"
    panel_pats = [_re.compile(p) for _, _, p in (settings.panels or DEFAULT_PANELS)]
    panel_tag: dict[tuple[str, str], str] = {}
    for r in conn.execute(
            "SELECT d.name AS dev, i.name AS iface, i.description AS descr "
            "FROM interfaces i JOIN devices d ON d.id = i.device_id "
            "WHERE i.description IS NOT NULL"):
        for rx in panel_pats:
            m = rx.search(r["descr"] or "")
            if m:
                panel_tag[(r["dev"], r["iface"])] = m.group(0)
                break

    def lab(dev: str, iface: str) -> str:
        if iface in ("?", ""):
            return ""
        tag = panel_tag.get((dev, iface))
        return f"{iface} {tag}" if tag else iface

    # utilization divides by the declared service capacity where one
    # exists (a 10G port carrying a 3G circuit), else the port speed;
    # in either case it's the busier direction — full duplex gives each
    # direction the whole capacity, so a link maxes at 100%, not 200%
    def cap_of(key: tuple[str, str], spd: int) -> int:
        return settings.capacities.get(key) or spd

    speed_of: dict[tuple[str, str], int] = {}
    util_of: dict[tuple[str, str], float] = {}
    for r in conn.execute(
            "SELECT d.name AS dev, i.name AS iface, i.speed_bps, i.in_bps, i.out_bps "
            "FROM interfaces i JOIN devices d ON d.id = i.device_id "
            "WHERE i.speed_bps > 0"):
        key = (r["dev"], r["iface"])
        speed_of[key] = r["speed_bps"]
        rates = [v for v in (r["in_bps"], r["out_bps"]) if v is not None]
        if rates:
            util_of[key] = round(max(rates) / cap_of(key, r["speed_bps"]) * 100, 1)
    # peak over the last 24h of poll samples (only as good as poll cadence)
    peak_of: dict[tuple[str, str], float] = {}
    for r in conn.execute(
            "SELECT device, interface, "
            "MAX(MAX(COALESCE(in_bps, 0), COALESCE(out_bps, 0))) AS pk "
            "FROM rate_history WHERE ts > ? GROUP BY device, interface",
            (db.now() - 86400,)):
        key = (r["device"], r["interface"])
        spd = speed_of.get(key)
        if spd:
            peak_of[key] = round(r["pk"] / cap_of(key, spd) * 100, 1)

    import math

    def edge_speed(e: dict) -> tuple[float, str, str]:
        """(stroke width, human label, slow-tier class) from either end's
        port speed. Sub-1G links get 1G's width — the *label color* tells
        the story (amber ≤100M, red ≤10M), not a hairline stroke."""
        a = (e["a_device"], e["a_interface"])
        b = (e["b_device"], e["b_interface"])
        bps = speed_of.get(a) or speed_of.get(b)
        if not bps:
            # a cable whose speed we can't read — a declared link to a
            # powered-off host, say — is still a cable: draw it at the same
            # floor as 1G and let the missing label say the link isn't
            # currently up. A hairline would read as "slow", not "off".
            return 4.2, "", ""
        label = human_speed(bps)
        cap = settings.capacities.get(a) or settings.capacities.get(b)
        if cap and cap != bps:
            label = f"{label} ({human_speed(cap)})"
        width = max(4.2, min(1.0 + math.log10(bps / 1e8) * 3.2, 9.0))
        tier = ("vslow" if bps <= 10_000_000
                else "slow" if bps <= 100_000_000 else "")
        return round(width, 1), label, tier

    # VLAN membership per node: Q-BRIDGE tables where a switch reports
    # them, else the VLAN of the subnet its shown IP lives in
    dv: dict[str, set[int]] = {}
    for r in conn.execute("SELECT device, vid FROM device_vlans"):
        dv.setdefault(r["device"], set()).add(r["vid"])
    subnet_nets = []
    for r in conn.execute("SELECT cidr, vlan FROM subnets WHERE vlan IS NOT NULL"):
        try:
            subnet_nets.append((ipaddress.ip_network(r["cidr"], strict=False), r["vlan"]))
        except ValueError:
            pass

    def vlans_of_ip(ip: str | None) -> set[int]:
        try:
            a = ipaddress.ip_address(ip or "")
        except ValueError:
            return set()
        return {vid for net, vid in subnet_nets if a in net}

    # unmanaged switches sit on access ports: their VLAN is whatever the
    # operator declared for the feeding port, else what the endpoints
    # behind them prove by their addresses
    unm_vlans: dict[str, set[int]] = {}
    for r in conn.execute(
            "SELECT device, ip FROM endpoints "
            "WHERE device LIKE 'unmanaged@%' AND ip IS NOT NULL"):
        unm_vlans.setdefault(r["device"], set()).update(vlans_of_ip(r["ip"]))
    # a firewall is a member of every VLAN it holds an address in — the
    # routed view of membership (its NICs are untagged; tagging happens
    # upstream on the vSwitch/switch)
    fw_if_vlans: dict[str, set[int]] = {}
    for r in conn.execute(
            "SELECT d.name AS dev, i.ip, i.ip6 FROM interfaces i "
            "JOIN devices d ON d.id = i.device_id "
            "WHERE d.role = 'firewall' AND (i.ip IS NOT NULL OR i.ip6 IS NOT NULL)"):
        for addr in (r["ip"], r["ip6"]):
            if addr:
                fw_if_vlans.setdefault(r["dev"], set()).update(
                    vlans_of_ip(addr.split("/")[0]))
    # per-port membership parsed out of backed-up configs — the strongest
    # evidence short of an operator declaration
    pv_all: dict[tuple[str, str], set[int]] = {}
    pv_untagged: dict[tuple[str, str], set[int]] = {}
    for r in conn.execute("SELECT device, interface, vid, tagged FROM port_vlans"):
        k = (r["device"], r["interface"])
        pv_all.setdefault(k, set()).add(r["vid"])
        if not r["tagged"]:
            pv_untagged.setdefault(k, set()).add(r["vid"])

    monitor_ports = {(r["device"], r["interface"]): r["detail"] for r in conn.execute(
        "SELECT device, interface, detail FROM port_roles WHERE role = 'monitor-dst'")}

    def port_mode(k: tuple[str, str]) -> str | None:
        """Normalized across vendors: any tagged membership = trunk (incl.
        M4300 'general' mode / FastIron dual), untagged-only = access. A
        mirror destination outranks both — it isn't carrying its VLANs, it's
        transmitting copies of someone else's."""
        if k in monitor_ports:
            return "monitor"
        if k not in pv_all:
            return None
        return "trunk" if pv_all[k] - pv_untagged.get(k, set()) else "access"

    saved = {r["name"]: (r["x"], r["y"])
             for r in conn.execute("SELECT * FROM positions").fetchall()}
    def mk_node(d) -> dict:
        # unmanaged switches get a compact label; full identity in tooltip
        label = "unmanaged" if d["role"] == "unmanaged-switch" else d["name"]
        if d["role"] == "unmanaged-switch" and "@" in d["name"]:
            # a switch behind this port sees everything the port passes,
            # tagged included (e.g. a closet switch fed native 1 + tagged 22)
            feed = tuple(d["name"].split("@", 1)[1].split(":", 1))
            vl = (settings.vlan_filters.get(feed) or pv_all.get(feed)
                  or unm_vlans.get(d["name"], set()))
        else:
            vl = dv.get(d["name"], set()) | vlans_of_ip(d["mgmt_ip"])
            vl |= fw_if_vlans.get(d["name"], set())
        return {
            "name": d["name"], "label": label,
            # 24px left inset (status dot) + text + 26px icon clearance
            # (the label shares its row with the role icon; the sub doesn't)
            "w": max(len(label) * 7.6 + 26, len(subs[d["name"]]) * 6.6) + 48,
            "sub": subs[d["name"]], "status": d["status"] or "unknown",
            "role": d["role"], "rank": RANK[d["role"]],
            "title": (f"{d['name']} · {d['model']}" if d["role"] == "unmanaged-switch"
                      else d["name"]),
            "vlans": sorted(vl),
            "px": saved.get(d["name"], (None, None))[0],
            "py": saved.get(d["name"], (None, None))[1],
        }

    nodes = [mk_node(d) for d in devs]
    # switches and hypervisors are the fabric tier — one shared width so
    # the core of the map reads as a tier, not a size comparison
    fabric = [n for n in nodes if n["role"] in ("switch", "hypervisor")]
    for n in fabric:
        n["w"] = max(f["w"] for f in fabric)
    # internet clouds, hung off the firewall's WAN gateway state. One node per
    # named provider; every declared landing port becomes its own edge, so a
    # provider delivered over two circuits shows two cables to one cloud.
    gws = conn.execute(
        "SELECT * FROM gateways WHERE name LIKE 'WAN%' AND address NOT LIKE 'fe80%' "
        "ORDER BY name").fetchall()
    fw = next((d for d in devs if d["role"] == "firewall"), None)
    on_map = {d["name"] for d in devs}
    # provider index -> the ports it lands on
    landings: dict[int, list[tuple[str, str]]] = {}
    for port_i, name_i in enumerate(settings.wan_pairing):
        port = settings.wan_ports[port_i]
        if port[0] in on_map:
            landings.setdefault(name_i, []).append(port)
    if not landings and fw and gws:
        # nothing declared, but the firewall reports a live WAN gateway: hang
        # the cloud off the firewall itself, right for the common "modem
        # straight into the WAN NIC" case. With neither, patchbay has no
        # evidence the internet exists and draws no cloud.
        landings = {0: [(fw["name"], "WAN")]}
    for name_i, ports in sorted(landings.items()):
        wan_name = settings.wan_names[name_i]
        # gateways pair with providers in order; a single-gateway firewall
        # lends its status to the first provider only, and the rest report
        # what patchbay actually knows, which is nothing
        gw = gws[name_i] if name_i < len(gws) else None
        sub = f"{gw['name']} {gw['status']}" if gw else "no gateway reported"
        nodes.append({
            "name": wan_name, "role": "cloud", "rank": -1,
            "w": max(len(wan_name), len(sub)) * 7.2 + 34,
            "sub": sub,
            "status": ("up" if gw["status"] == "Online" else "down") if gw else "unknown",
            # the internet lives on whatever VLAN its landing ports carry
            "vlans": sorted(set().union(set(), *(
                settings.vlan_filters.get(k) or pv_all.get(k) or set() for k in ports))),
            "px": saved.get(wan_name, (None, None))[0],
            "py": saved.get(wan_name, (None, None))[1],
        })
        for dev_name, iface in ports:
            raw_edges.append({"a_device": dev_name, "a_interface": iface,
                              "b_device": wan_name, "b_interface": "",
                              "source": "wan"})

    # named wired hosts on switch ports (behind the 'wired hosts' toggle).
    # A host is keyed by canonical short hostname — aliases fold interface
    # hostnames (e.g. nas1ten24 -> nas1) into one box, and every distinct
    # switch port it's seen on becomes its own edge (multi-homed NAS etc.)
    switch_names = {d["name"] for d in devs if d["role"] in ("switch",)}
    alias_map = dict(conn.execute("SELECT alias, canonical FROM aliases").fetchall())
    dev_names = {n["name"] for n in nodes}
    hosts: dict[str, dict] = {}

    def host_port(short: str, dev: str, iface: str) -> dict:
        h = hosts.setdefault(short, {"ips": [], "ports": {}, "direct": False})
        if dev in switch_names:
            h["direct"] = True  # vs. only reachable via an unmanaged switch
        return h["ports"].setdefault((dev, iface), {"blab": "", "ips": []})

    unmanaged_names = {d["name"] for d in devs if d["role"] == "unmanaged-switch"}
    for r in conn.execute(
            "SELECT hostname, ip, device, interface FROM endpoints "
            "WHERE hostname IS NOT NULL ORDER BY hostname"):
        on_switch = (r["device"] in switch_names
                     and r["interface"] and r["interface"] != "?")
        # named devices behind an unmanaged switch hang off its box
        # (their port there is unknowable — it's unmanaged)
        behind_unmanaged = r["device"] in unmanaged_names
        if not (on_switch or behind_unmanaged):
            continue
        short = r["hostname"].split(".")[0].lower()
        short = alias_map.get(short) or alias_map.get(r["hostname"]) or short
        if short in dev_names:
            continue  # already a first-class device on the map
        port = host_port(short, r["device"], r["interface"] if on_switch else "?")
        if r["ip"]:
            if r["ip"] not in hosts[short]["ips"]:
                hosts[short]["ips"].append(r["ip"])
            if r["ip"] not in port["ips"]:
                port["ips"].append(r["ip"])
    # operator-declared cables to hosts: quiet bond members (Synology
    # balance-alb secondaries etc.) never show in FDB, but the operator
    # knows they're plugged — PATCHBAY_LINKS="edge1:ethernet1/1/30=nas1:lan2"
    for r in conn.execute("SELECT * FROM links WHERE source = 'declared'"):
        for dv, di, hv, hi in ((r["a_device"], r["a_interface"], r["b_device"], r["b_interface"]),
                               (r["b_device"], r["b_interface"], r["a_device"], r["a_interface"])):
            if dv in switch_names and hv not in dev_names:
                short = alias_map.get(hv, hv)
                if short in dev_names:
                    continue
                port = host_port(short, dv, di)
                if hi and hi != "?":
                    port["blab"] = hi
    for short, h in sorted(hosts.items()):
        # per-port VLAN reach: a declared filter is the answer; otherwise
        # the subnets of the IPs actually seen on that port
        port_vlans: dict[tuple[str, str], set[int]] = {}
        vl: set[int] = set()
        for pkey, p in h["ports"].items():
            pv = settings.vlan_filters.get(pkey) or pv_all.get(pkey) or set().union(
                set(), *(vlans_of_ip(ip) for ip in p["ips"]))
            port_vlans[pkey] = pv
            vl |= pv
        for ip in h["ips"]:
            vl |= vlans_of_ip(ip)
        sub = (h["ips"][0] if len(h["ips"]) == 1
               else f"{len(h['ips'])} addresses" if h["ips"] else "wired host")
        nodes.append({
            "name": short, "role": "host", "rank": 2,
            "viaUn": not h["direct"],
            "w": max(len(short) * 7.6 + 26, len(sub) * 6.6) + 48,
            "sub": sub, "status": "up", "vlans": sorted(vl),
            "px": saved.get(short, (None, None))[0],
            "py": saved.get(short, (None, None))[1],
        })
        for (hdev, hiface), p in sorted(h["ports"].items()):
            raw_edges.append({"a_device": hdev, "a_interface": hiface,
                              "b_device": short, "b_interface": p["blab"],
                              "source": "fdb-host",
                              "vlans": sorted(port_vlans[(hdev, hiface)]) or None})

    # declared component pairs (BMC <-> server) become dotted oob edges;
    # both must be nodes on the map for the tie to render
    node_names_final = {n["name"] for n in nodes}
    for comp, owner in settings.related:
        comp = alias_map.get(comp, comp)
        owner = alias_map.get(owner, owner)
        if comp in node_names_final and owner in node_names_final:
            raw_edges.append({"a_device": comp, "a_interface": "",
                              "b_device": owner, "b_interface": "",
                              "source": "oob"})

    node_vlanset = {n["name"]: set(n.get("vlans", [])) for n in nodes}
    edges = []
    for e in raw_edges:
        sw, slabel, slowtier = edge_speed(e)
        if e["source"] == "virtualized":
            sw = 2.5  # containment must stay readable
        elif e["source"] == "oob":
            sw, slabel = 2.0, "oob"
        a_key, b_key = (e["a_device"], e["a_interface"]), (e["b_device"], e["b_interface"])
        util = util_of.get(a_key) if a_key in util_of else util_of.get(b_key)
        putil = peak_of.get(a_key) if a_key in peak_of else peak_of.get(b_key)
        if "vlans" in e:  # host edges carry their own per-port answer
            evlans = e["vlans"]
        else:
            # a trunk passes whatever either side carries, minus any
            # declared per-port filter
            sa, sb = node_vlanset.get(e["a_device"]), node_vlanset.get(e["b_device"])
            ev = (sa or set()) | (sb or set())
            for k in (a_key, b_key):
                f = settings.vlan_filters.get(k) or pv_all.get(k)
                if f is not None:
                    ev &= f
            evlans = sorted(ev) or None
        mon = monitor_ports.get(a_key) or monitor_ports.get(b_key)
        edges.append({
            "source": e["a_device"], "target": e["b_device"],
            "alab": lab(e["a_device"], e["a_interface"]),
            "blab": lab(e["b_device"], e["b_interface"]),
            "amode": port_mode(a_key), "bmode": port_mode(b_key),
            "cls": e["source"].replace("-", "") + (f" {slowtier}" if slowtier else "")
                   + (" monitor" if mon else ""),
            "sw": sw, "speed": slabel, "util": util, "putil": putil,
            "vlans": evlans,
            "note": f"port mirror · {mon}" if mon else None,
        })
    vlan_names = {r["vid"]: r["name"] for r in conn.execute("SELECT vid, name FROM vlans")}
    present_vlans = sorted({v for n in nodes for v in n.get("vlans", [])})
    vlan_opts = [{"vid": v, "name": vlan_names.get(v)} for v in present_vlans]
    # names/labels come from the network (LLDP sysnames, client names) —
    # escape script-breaking chars so a hostile advertisement can't XSS
    graph_json = (json.dumps({"nodes": nodes, "links": edges, "vlans": vlan_opts})
                  .replace("&", "\\u0026").replace("<", "\\u003c")
                  .replace(">", "\\u003e")
                  .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    return graph_json, bool(peak_of)


@app.get("/topology", response_class=HTMLResponse)
def topology(request: Request):
    settings = load_settings()
    conn = _conn()
    try:
        db.init(conn)
        graph_json, peak_ready = build_topology_graph(conn, settings)
        return templates.TemplateResponse(request, "topology.html", {
            "graph_json": graph_json,
            "peak_ready": peak_ready,
            "ages": _age(conn),
        })
    finally:
        conn.close()


@app.post("/api/positions")
async def save_position(request: Request):
    try:
        body = await request.json()
        name, x, y = body.get("name"), body.get("x"), body.get("y")
        if x is not None:
            x, y = float(x), float(y)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "bad body")
    if not isinstance(name, str) or len(name) > 200:
        raise HTTPException(400, "bad name")

    def write() -> None:  # sqlite work off the event loop — this is the one
        conn = _conn()    # async route, and a busy DB would block the server
        try:
            db.init(conn)
            if x is None or y is None:
                conn.execute("DELETE FROM positions WHERE name = ?", (name,))
            else:
                conn.execute("INSERT OR REPLACE INTO positions (name, x, y) VALUES (?, ?, ?)",
                             (name, x, y))
            conn.commit()
        finally:
            conn.close()

    import anyio
    await anyio.to_thread.run_sync(write)
    return {"ok": True}


@app.post("/api/positions/reset")
def reset_positions():
    conn = _conn()
    try:
        db.init(conn)
        conn.execute("DELETE FROM positions")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/patchpanel", response_class=HTMLResponse)
def patchpanel(request: Request, panel: str | None = None):
    import re as _re

    panels = load_settings().panels or DEFAULT_PANELS
    sel = panel if panel in {p[0] for p in panels} else panels[0][0]
    _, size, pattern = next(p for p in panels if p[0] == sel)
    rx = _re.compile(pattern)
    conn = _conn()
    try:
        db.init(conn)
        rows = []
        for r in conn.execute(
                "SELECT d.name AS dev, i.name AS iface, i.description AS descr, "
                "i.oper_status, i.speed_bps FROM interfaces i "
                "JOIN devices d ON d.id = i.device_id WHERE i.description IS NOT NULL"):
            m = rx.search(r["descr"] or "")
            # a pattern without a numeric capture group matches nothing useful
            if m and m.groups() and (m.group(1) or "").isdigit():
                rows.append({"port": int(m.group(1)), "dev": r["dev"],
                             "iface": r["iface"], "descr": r["descr"],
                             "oper": r["oper_status"], "speed": r["speed_bps"]})
        # surface gaps: a panel position no switch port claims is a fact worth
        # seeing (unpatched jack, dead slot, or a run whose description lost
        # its tag). A declared size also surfaces trailing empty positions.
        have = {r["port"] for r in rows}
        top = max([size, *have]) if (have or size) else 0
        rows += [{"port": n, "dev": None, "iface": None, "descr": None,
                  "oper": None, "speed": None}
                 for n in range(1, top + 1) if n not in have]
        rows.sort(key=lambda r: (r["port"], r["dev"] or ""))
        return templates.TemplateResponse(request, "patchpanel.html",
                                          {"rows": rows, "ages": _age(conn),
                                           "panels": [p[0] for p in panels],
                                           "sel": sel, "size": size})
    finally:
        conn.close()


def _ip_key(ip: str):
    try:
        a = ipaddress.ip_address(ip)
        return (a.version, int(a))
    except ValueError:
        return (99, 0)


@app.get("/vlans", response_class=HTMLResponse)
def vlans(request: Request):
    conn = _conn()
    try:
        db.init(conn)
        live_ips = [r["ip"] for r in conn.execute(
            "SELECT DISTINCT ip FROM endpoints WHERE ip IS NOT NULL")]
        ipam_ips = [r["ip"] for r in conn.execute("SELECT ip FROM ipam_addresses")]

        def in_net(cidr: str, ips: list[str]) -> int:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                return 0
            n = 0
            for ip in ips:
                try:
                    if ipaddress.ip_address(ip) in net:
                        n += 1
                except ValueError:
                    pass
            return n

        carriers: dict[int, list[dict]] = {}
        for r in conn.execute(
                "SELECT device, vid, source FROM device_vlans ORDER BY source != 'trunk' DESC, device"):
            carriers.setdefault(r["vid"], []).append(
                {"name": r["device"], "assumed": r["source"] == "trunk"})

        # a subnet is routed if a firewall owns an address inside it
        fw_addrs = []
        for r in conn.execute(
                "SELECT d.name dev, i.description descr, i.ip, i.ip6 "
                "FROM interfaces i JOIN devices d ON d.id = i.device_id "
                "WHERE d.role = 'firewall' AND (i.ip IS NOT NULL OR i.ip6 IS NOT NULL)"):
            for addr in (r["ip"], r["ip6"]):
                if not addr:
                    continue
                try:
                    fw_addrs.append((ipaddress.ip_address(addr.split("/")[0]),
                                     f"{addr.split('/')[0]} ({r['descr'] or r['dev']})"))
                except ValueError:
                    pass

        # ...and routed if a router carries a route covering it, even when no
        # local address sits inside (delegated IPv6 prefixes reach via a
        # next-hop downstream — "isolated" was always wrong for those)
        route_nets = []
        for r in conn.execute(
                "SELECT destination, gateway, interface, flags FROM routes"):
            # B/R flags are discard routes — the null route covering the unused
            # part of a delegated prefix says traffic is DROPPED, not delivered
            if any(f in (r["flags"] or "") for f in "BR"):
                continue
            try:
                route_nets.append((ipaddress.ip_network(r["destination"], strict=False),
                                   r["gateway"], r["interface"]))
            except ValueError:
                pass

        def routed_by(cidr: str) -> str | None:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                return None
            own = next((label for a, label in fw_addrs if a in net), None)
            if own:
                return own
            # longest-prefix match wins: the specific route for a delegated
            # prefix is a better answer than the aggregate that contains it
            covering = [(rn, gw, iface) for rn, gw, iface in route_nets
                        if rn.version == net.version and rn.prefixlen <= net.prefixlen
                        and net.subnet_of(rn)]
            if not covering:
                return None
            rn, gw, iface = max(covering, key=lambda x: x[0].prefixlen)
            if gw and not gw.startswith("link#"):
                return f"via {gw.split('%')[0]} ({iface})" if iface else f"via {gw}"
            return f"on {iface}" if iface else "routed"
        subnets_by_vlan: dict[int | None, list] = {}
        for r in conn.execute("SELECT * FROM subnets ORDER BY cidr"):
            subnets_by_vlan.setdefault(r["vlan"], []).append(r)

        rows = []
        mapped_nets = []
        for v in conn.execute("SELECT * FROM vlans ORDER BY vid").fetchall():
            subs = subnets_by_vlan.pop(v["vid"], [])
            for s in subs:
                try:
                    mapped_nets.append(ipaddress.ip_network(s["cidr"], strict=False))
                except ValueError:
                    pass
            rows.append({
                "vid": v["vid"], "name": v["name"], "devices": carriers.get(v["vid"], []),
                "subnets": [{"cidr": s["cidr"], "descr": s["description"],
                             "live": in_net(s["cidr"], live_ips),
                             "ipam": in_net(s["cidr"], ipam_ips),
                             "routed": routed_by(s["cidr"])} for s in subs],
            })
        # subnets whose VLAN is unmapped (or maps to an unknown VLAN id).
        # A supernet that contains mapped subnets is an aggregate — it has no
        # VLAN of its own by design, so it's a footnote, not a finding.
        def n_mapped_inside(cidr: str) -> int:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                return 0
            return sum(1 for m in mapped_nets
                       if m.version == net.version and m != net and m.subnet_of(net))

        orphans, aggregates = [], []
        for vid, group in subnets_by_vlan.items():
            for s in group:
                inside = n_mapped_inside(s["cidr"])
                if inside:
                    aggregates.append({"cidr": s["cidr"], "descr": s["description"],
                                       "n": inside})
                else:
                    orphans.append({"cidr": s["cidr"], "descr": s["description"], "vlan": vid,
                                    "live": in_net(s["cidr"], live_ips),
                                    "ipam": in_net(s["cidr"], ipam_ips),
                                    "routed": routed_by(s["cidr"])})
        return templates.TemplateResponse(request, "vlans.html", {
            "vlans": rows, "orphans": orphans, "aggregates": aggregates,
            "ages": _age(conn),
        })
    finally:
        conn.close()


def _ipam_link(settings, row) -> str | None:
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


@app.get("/drift", response_class=HTMLResponse)
def drift(request: Request):
    from .normalize import canon_mac

    settings = load_settings()
    conn = _conn()
    try:
        db.init(conn)
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
        for ip in sorted(observed, key=_ip_key):
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
                conflicts.append({"ip": ip, "kind": "hostname", "link": _ipam_link(settings, doc),
                                  "ipam": doc["hostname"], "live": e["hostname"]})
                clean = False
            if doc["mac"] and e["mac"] and canon_mac(doc["mac"]) != canon_mac(e["mac"]):
                conflicts.append({"ip": ip, "kind": "mac", "link": _ipam_link(settings, doc),
                                  "ipam": doc["mac"], "live": e["mac"]})
                clean = False
            in_sync += clean
        # "documented but quiet" only matters for addresses IPAM claims are
        # fixed assets — DHCP-pool rows going quiet is normal, not drift
        unseen, n_dhcp_quiet = [], 0
        for ip in sorted(ipam, key=_ip_key):
            if ip in observed:
                continue
            if (ipam[ip]["state"] or "") == "dhcp":
                n_dhcp_quiet += 1
            else:
                unseen.append({**dict(ipam[ip]), "link": _ipam_link(settings, ipam[ip])})
        have_ipam = bool(ipam)
        return templates.TemplateResponse(request, "drift.html", {
            "undocumented": undocumented, "external": external, "unseen": unseen,
            "n_dhcp_quiet": n_dhcp_quiet, "conflicts": conflicts,
            "in_sync": in_sync, "have_ipam": have_ipam, "ages": _age(conn),
        })
    finally:
        conn.close()


# --- operator actions: on-demand poll + LibreNMS rediscovery ----------------

def _json_state(conn, key: str):
    import json as _json

    raw = db.get_state(conn, key)
    try:
        return _json.loads(raw) if raw else None
    except ValueError:
        return None


def _effective_config(s) -> list[tuple[str, list[tuple[str, str]]]]:
    """The running config as patchbay understood it — values redacted where
    secret, declarations shown parsed (what the code will act on, not what
    the file says). This is the read half of config visibility; the env file
    stays the source of truth."""
    def onoff(v) -> str:
        return "set" if v else "—"

    def port(t: tuple[str, str]) -> str:
        return f"{t[0]}:{t[1]}"

    src = [("LibreNMS", s.librenms_url, s.librenms_token),
           ("Oxidized", s.oxidized_url, True),
           ("phpIPAM", s.ipam_url, s.ipam_token),
           ("UniFi", s.unifi_url, s.unifi_pass),
           ("OPNsense", s.opnsense_host, s.opnsense_api_secret),
           ("vSphere", s.vsphere_host, s.vsphere_pass)]
    return [
        ("serving", [
            ("PATCHBAY_DB", s.db_path),
            ("PATCHBAY_TLS_VERIFY (outbound)", str(s.tls_verify)),
            ("PATCHBAY_TLS", s.tls_mode +
             (f" · cert {s.tls_cert} · key {s.tls_key}" if s.tls_mode == "direct" else "")),
            ("PATCHBAY_AUTH", s.auth_mode +
             (f" · session {s.session_hours:g}h" if s.auth_mode != "none" else "")),
        ]),
        ("sources", [
            (name, (url or "—") + (" · credentials " + onoff(cred) if url else ""))
            for name, url, cred in src
        ]),
        ("declarations", [
            ("PATCHBAY_ALIASES", ", ".join(f"{a}={c}" for a, c in s.aliases.items()) or "—"),
            ("PATCHBAY_UNMANAGED", ", ".join(port(u) for u in s.unmanaged) or "—"),
            ("PATCHBAY_LINKS", ", ".join(
                f"{a}:{ai}={b}" + (f":{bi}" if bi != "?" else "")
                for a, ai, b, bi in s.links) or "—"),
            ("PATCHBAY_RELATED", ", ".join(f"{a}={b}" for a, b in s.related) or "—"),
            ("PATCHBAY_WAN_NAME / _PORT", ", ".join(
                name + (" @ " + " + ".join(
                    port(s.wan_ports[i]) for i, n in enumerate(s.wan_pairing) if n == ni)
                    if ni in s.wan_pairing else " @ firewall")
                for ni, name in enumerate(s.wan_names))),
            ("PATCHBAY_VLAN_FILTER", ", ".join(
                f"{port(p)}={'+'.join(str(v) for v in sorted(vs))}"
                for p, vs in s.vlan_filters.items()) or "—"),
            ("PATCHBAY_CAPACITY", ", ".join(
                f"{port(p)}={human_speed(c)}" for p, c in s.capacities.items()) or "—"),
            ("PATCHBAY_PANELS", ", ".join(
                f"{n} (size {sz or 'auto'}, /{rx}/)" for n, sz, rx in s.panels) or "—"),
        ]),
    ]


@app.get("/ops", response_class=HTMLResponse)
def ops(request: Request):
    import os

    from .collectors import available
    from .config import DECLARATION_VARS, _db_declarations

    settings = load_settings()
    stored = _db_declarations(settings.db_path)
    decls = []
    for var in DECLARATION_VARS:
        src = settings.declaration_sources.get(var)
        decls.append({"var": var, "source": src, "editable": src != "env",
                      "value": (os.environ.get(var) if src == "env"
                                else stored.get(var)) or ""})
    conn = _conn()
    try:
        db.init(conn)
        last_poll = None
        raw_lp = db.get_state(conn, "last_poll")
        if raw_lp:
            import json as _json
            try:
                last_poll = _json.loads(raw_lp)
                last_poll["ago"] = round((db.now() - last_poll["ts"]) / 60)
                last_poll["failed"] = any(l.startswith(("[fail]", "[warn]"))
                                          for l in last_poll["lines"])
            except (ValueError, KeyError, TypeError, AttributeError):
                last_poll = None
        return templates.TemplateResponse(request, "ops.html", {
            "sources": sorted(available(settings)), "ages": _age(conn),
            "config": _effective_config(settings),
            "warnings": settings.parse_warnings,
            "decls": decls,
            "export": "\n".join(f'{v}="{stored[v]}"' for v in DECLARATION_VARS
                                if stored.get(v)),
            "last_poll": last_poll,
            "conflicts": _json_state(conn, "declaration_conflicts") or [],
            "has_snapshot": (Path(settings.snapshot_dir) / "patchbay-latest.html").exists(),
        })
    finally:
        conn.close()


@app.post("/ops/snapshot")
def ops_snapshot():
    """Generate a break-glass snapshot on demand (phase 5)."""
    from .snapshot import write_snapshot

    settings = load_settings()
    if not _ops_lock.acquire(blocking=False):
        raise HTTPException(409, "an ops action is already running — wait for it")
    from .snapshot import DeliveryError

    try:
        t0 = db.now()
        delivered = None
        try:
            path = write_snapshot(settings)
        except DeliveryError as e:
            delivered = f"[warn] written locally, delivery failed: {e}"
            path = max(Path(settings.snapshot_dir).glob("patchbay-2*.html"),
                       key=lambda p: p.stat().st_mtime)
        lines = [f"[ok]   snapshot: {path} "
                 f"({path.stat().st_size / 1e6:.1f} MB in {db.now() - t0:.0f}s)"]
        if delivered:
            lines.append(delivered)
        elif settings.snapshot_deliver_dir:
            lines.append(f"[ok]   delivered to {settings.snapshot_deliver_dir}")
        lines.append("[ok]   patchbay-latest.html updated — download from the link below")
        return {"lines": lines}
    except Exception as e:
        raise HTTPException(500, f"snapshot failed: {e}")
    finally:
        _ops_lock.release()


@app.get("/ops/snapshot/latest")
def ops_snapshot_latest():
    settings = load_settings()
    p = Path(settings.snapshot_dir) / "patchbay-latest.html"
    if not p.exists():
        raise HTTPException(404, "no snapshot generated yet")
    return Response(p.read_bytes(), media_type="text/html", headers={
        "Content-Disposition": 'attachment; filename="patchbay-snapshot.html"'})


@app.post("/ops/config")
async def ops_config(request: Request):
    """Store one declaration in the DB (the UI half of env-wins-else-DB).
    Same syntax as the .env file; an env-set variable is refused — the file
    owns it. Takes effect on the next poll (declarations act at normalize)."""
    import os

    from .config import DECLARATION_VARS

    body = await request.json()
    var, value = body.get("var"), body.get("value", "")
    if var not in DECLARATION_VARS:
        raise HTTPException(400, "not an editable declaration")
    if os.environ.get(var) is not None:
        raise HTTPException(409, f"{var} is set in the env file — edit it there")
    if not isinstance(value, str) or len(value) > 10000:
        raise HTTPException(400, "bad value")
    conn = _conn()
    try:
        db.init(conn)
        if value.strip():
            conn.execute(
                "INSERT INTO app_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"cfg:{var}", value.strip()))
        else:
            conn.execute("DELETE FROM app_state WHERE key = ?", (f"cfg:{var}",))
        conn.commit()
    finally:
        conn.close()
    fresh = load_settings()  # re-parse so typos surface immediately
    return {"ok": True,
            "warnings": [w for w in fresh.parse_warnings if w.startswith(var)]}


# one operator action at a time: repeated clicks (or a scripted hammering)
# must not stack full poll cycles onto the threadpool and the SQLite file
import threading

_ops_lock = threading.Lock()


@app.post("/ops/poll")
def ops_poll(source: str | None = None):
    from .collectors import available
    from .normalize import normalize

    settings = load_settings()
    collectors = available(settings)
    if source:
        collectors = {n: c for n, c in collectors.items() if n == source}
        if not collectors:
            raise HTTPException(404, f"unknown or unconfigured source: {source}")
    if not _ops_lock.acquire(blocking=False):
        raise HTTPException(409, "an ops action is already running — wait for it")
    lines = [f"[warn] {w}" for w in settings.parse_warnings]
    conn = _conn()
    try:
        db.init(conn)
        for name, collector in sorted(collectors.items()):
            try:
                lines.append(f"[ok]   {name}: {collector.collect(settings, conn)}")
                conn.commit()
            except Exception as e:  # one failing source must not block the others
                conn.rollback()
                lines.append(f"[fail] {name}: {e}")
        try:
            lines.append(f"[ok]   normalize: {normalize(conn, settings.aliases, settings.unmanaged, settings.links, settings.vlan_filters, prune_declared=settings.declarations_readable)}")
            conn.commit()
        except Exception as e:
            conn.rollback()
            lines.append(f"[fail] normalize: {e}")
        db.save_last_poll(conn, lines)
        conn.commit()
    finally:
        conn.close()
        _ops_lock.release()
    return {"lines": lines}


@app.post("/ops/rediscover")
def ops_rediscover():
    """Queue a LibreNMS rediscovery for every device it monitors — the slow
    evidence (FDB snapshots, xDP neighbor tables) refreshes on discovery,
    not on the 5-minute poll."""
    from urllib.parse import quote

    settings = load_settings()
    if not (settings.librenms_url and settings.librenms_token):
        raise HTTPException(404, "librenms not configured")
    if not _ops_lock.acquire(blocking=False):
        raise HTTPException(409, "an ops action is already running — wait for it")
    lines = []
    try:
        with httpx.Client(verify=settings.tls_verify, timeout=30,
                          headers={"X-Auth-Token": settings.librenms_token}) as client:
            base = settings.librenms_url.rstrip("/")
            r = client.get(f"{base}/api/v0/devices")
            r.raise_for_status()
            try:
                devs = r.json().get("devices", [])
            except ValueError:
                raise HTTPException(502, "librenms answered with non-JSON — proxy or auth issue?")
            for d in devs:
                h = d.get("hostname")
                if not h:
                    continue
                try:
                    r = client.get(f"{base}/api/v0/devices/{quote(str(h), safe='')}/discover")
                    ok = r.status_code == 200
                    lines.append(f"[{'ok' if ok else 'fail'}] {h}: "
                                 f"{'rediscovery queued' if ok else r.text[:80]}")
                except httpx.HTTPError as e:  # one device must not abort the rest
                    lines.append(f"[fail] {h}: {e}")
    finally:
        _ops_lock.release()
    return {"lines": lines}


# --- LibreNMS graph proxy (phase 4.5): the token stays server-side, so ------
# --- LibreNMS itself never needs to be visited or exposed --------------------

GRAPH_TYPES = {"port_bits", "port_upkts", "port_errors"}
DEVICE_GRAPH_TYPES = {"device_processor", "device_mempool",
                      "device_storage", "device_temperature"}

# LibreNMS hardcodes the classic RRD palette regardless of the colours=
# scheme, and ignores grid-color overrides. The graphs are SVG, so the proxy
# recolors on the way through: In = teal, Out = hypervisor blue — one tone
# per direction so the plot and its legend square match exactly — the 95th-
# percentile line goes amber (so grey grid can't bury it), and rrdtool's
# red major grid joins the theme's greys.
# all three rrd slots per series (pale area, mid line, dark border) map to
# ONE tone: the legend square is painted with the dark slot, so anything
# less makes the legend disagree with the plot
GRAPH_RECOLOR = {
    "#d7ffc7": "#58b9c3", "#90b040": "#58b9c3", "#608720": "#58b9c3",  # In
    "#e0e0ff": "#6d83c4", "#8080c0": "#6d83c4", "#606090": "#6d83c4",  # Out
    "#aa0000": "#cfa049",   # 95th-percentile line
    "#ff9999": "#46565c",   # major grid (rrd default red)
    "#a5a5a5": "#2f3b40",   # minor grid
    "#5e5e5e": "#556468",   # axes / ticks
    "#1f1f1f": "#2b383c",   # frame
    # health-graph series (LibreNMS's stock reds/oranges/mixed): remap onto
    # theme-adjacent hues, keeping every within-graph pairing distinct
    "#cc0000": "#58b9c3", "#008c00": "#6d83c4", "#4096ee": "#82aaff",
    "#aaaaaa": "#8fa0a5", "#008fea": "#82aaff", "#3cb371": "#7ec9a2",
    "#dc7201": "#cfa049", "#ffbe55": "#e0c084", "#36393d": "#556468",
    "#73880a": "#9aab4f", "#d01f3c": "#c76c8a", "#ff0084": "#d16ba5",
    # CPU-core gradient (oranges) -> teal-to-purple ramp
    "#e43c00": "#4db8c2", "#e74b00": "#5aa9cd", "#eb5b00": "#6d97d9",
    "#ef6a00": "#8285dd", "#f37900": "#9877d6", "#f78800": "#ad68c6",
}


def _recolor_svg(svg: str) -> str:
    import re

    def sub(m: "re.Match[str]") -> str:
        try:
            hx = "#" + "".join(
                f"{round(float(p.strip().rstrip('%')) * 2.55):02x}"
                for p in m.group(1).split(","))
        except ValueError:
            return m.group(0)
        return GRAPH_RECOLOR.get(hx, m.group(0))

    return re.sub(r"rgb\(([\d.%, ]+)\)", sub, svg)


def _add_zero_line(svg: str) -> str:
    """rrdtool's zero grid line gets painted over by the area fills. The In
    area's polygon sits on zero, so its lowest edge IS the zero pixel row —
    redraw a line there, on top."""
    import re
    area = re.search(r'fill="#58b9c3" fill-opacity="1" d="([^"]+)"', svg)
    if not area:
        return svg
    # path data is strictly (cmd x y)*: odd-indexed numbers are the ys
    nums = re.findall(r"[-\d.]+", area.group(1))
    y0 = max(float(v) for v in nums[1::2])
    # the x-extent comes from the horizontal frame line (the one whose xs differ)
    for m in re.finditer(r'stroke="#2b383c"[^/]*?d="M ([\d.]+) [\d.]+ L ([\d.]+) [\d.]+', svg):
        x1, x2 = sorted(float(v) for v in m.groups())
        if x1 != x2:
            line = (f'<path fill="none" stroke-width="1" stroke="#6c7f85" '
                    f'd="M {x1} {y0} L {x2} {y0}"/>')
            return svg.replace("</svg>", line + "</svg>")
    return svg


def fetch_graph_image(settings, device: str, iface: str | None, gtype: str,
                      hours: int = 24, w: int = 700) -> tuple[bytes | str, str] | None:
    """(body, content-type) for a LibreNMS-rendered graph, recolored to the
    patchbay palette. None when LibreNMS is unconfigured or has no such graph.
    Shared by the /graph proxy and the snapshot generator."""
    if not (settings.librenms_url and settings.librenms_token):
        return None
    conn = _conn()
    try:
        db.init(conn)
        # patchbay names are canonical; LibreNMS knows devices by their own
        # hostname (usually the FQDN). The collector caches the device list —
        # match on the name it normalizes to.
        cands = [device] + [r["alias"] for r in conn.execute(
            "SELECT alias FROM aliases WHERE canonical = ?", (device,))]
        raw = conn.execute(
            "SELECT payload FROM raw_payloads WHERE source='librenms' AND "
            "endpoint='devices' ORDER BY fetched_at DESC LIMIT 1").fetchone()
        if raw:
            import json as _json
            from .normalize import canonical_name
            for d in _json.loads(raw["payload"]):
                h = d.get("hostname") or ""
                if h and canonical_name(h) == device and h not in cands:
                    cands.append(h)
    finally:
        conn.close()
    from urllib.parse import quote
    with httpx.Client(verify=settings.tls_verify, timeout=20) as client:
        for cand in cands:
            if iface is None:
                # only render health graphs backed by actual sensors — the
                # image endpoint happily draws empty axes for absent data
                hr = client.get(
                    f"{settings.librenms_url.rstrip('/')}/api/v0/devices/"
                    f"{quote(cand, safe='')}/health/{gtype}",
                    headers={"X-Auth-Token": settings.librenms_token})
                try:
                    if hr.status_code != 200 or not hr.json().get("graphs"):
                        continue
                except ValueError:  # 200 + non-JSON (proxy error page)
                    continue
            path = (f"devices/{quote(cand, safe='')}/{gtype}" if iface is None
                    else f"devices/{quote(cand, safe='')}/ports/"
                         f"{quote(iface, safe='')}/{gtype}")
            r = client.get(
                f"{settings.librenms_url.rstrip('/')}/api/v0/{path}",
                params={"from": f"-{max(1, min(int(hours), 8760))}h",
                        "width": max(200, min(int(w), 1000)), "height": 220,
                        # LibreNMS forwards rrdtool color overrides — restyle
                        # the graph to the patchbay palette (dark card, light
                        # text) instead of rrdtool's white default
                        "bg": "1a2326ff", "canvas": "12191bff",
                        "font": "dde6e8ff", "frame": "2b383cff",
                        "arrow": "8fa0a5ff", "grid": "8fa0a540",
                        "mgrid": "8fa0a580",
                        "shadea": "1a2326ff", "shadeb": "1a2326ff"},
                headers={"X-Auth-Token": settings.librenms_token})
            if (r.status_code == 200
                    and r.headers.get("content-type", "").startswith("image")):
                body: bytes | str = r.content
                if "svg" in r.headers["content-type"]:
                    body = _add_zero_line(_recolor_svg(r.text))
                return body, r.headers["content-type"]
    return None


@app.get("/graph")
def graph(device: str, iface: str | None = None, gtype: str = "port_bits",
          hours: int = 24, w: int = 700):
    if iface is None:
        if gtype not in DEVICE_GRAPH_TYPES:
            raise HTTPException(400, f"device graph type must be one of {sorted(DEVICE_GRAPH_TYPES)}")
    elif gtype not in GRAPH_TYPES:
        raise HTTPException(400, f"graph type must be one of {sorted(GRAPH_TYPES)}")
    settings = load_settings()
    got = fetch_graph_image(settings, device, iface, gtype, hours, w)
    if got is None:
        raise HTTPException(404, "no graph for this port")
    body, ctype = got
    return Response(body, media_type=ctype, headers={"Cache-Control": "max-age=120"})


# --- Oxidized config history (live proxy — configs are never stored here) ----

def _ox_client(settings) -> httpx.Client:
    return httpx.Client(base_url=settings.oxidized_url.rstrip("/"),
                        verify=settings.tls_verify, timeout=15)


def _ox_nodes(client: httpx.Client) -> list[dict]:
    r = client.get("/nodes.json")
    r.raise_for_status()
    return r.json()


def _ox_version_text(client: httpx.Client, node: str, v: dict, num: int) -> str:
    """Config text at a given version. oxidized-web's view endpoint has varied
    a little across releases, so accept any of the shapes it's known to emit."""
    r = client.get("/node/version/view.json", params={
        "node": node, "group": "", "oid": v.get("oid", ""),
        "date": str(v.get("date", "")), "num": num,
    })
    r.raise_for_status()
    data = r.json()
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return "\n".join(str(x) for x in data)
    if isinstance(data, dict):
        for k in ("output", "config", "data", "blob", "text"):
            if isinstance(data.get(k), str):
                return data[k]
    raise RuntimeError(f"unexpected version/view response shape: {type(data).__name__}")


@app.get("/configs", response_class=HTMLResponse)
def configs(request: Request):
    settings = load_settings()
    nodes, error = [], None
    if not settings.oxidized_url:
        error = "unconfigured"
    else:
        try:
            with _ox_client(settings) as client:
                for n in _ox_nodes(client):
                    last = n.get("last") or {}
                    nodes.append({
                        "name": n.get("name"), "ip": n.get("ip"),
                        "model": n.get("model"), "group": n.get("group"),
                        "status": last.get("status") or n.get("status") or "never",
                        "time": last.get("end") or n.get("time") or n.get("mtime"),
                    })
        except Exception as exc:  # unreachable oxidized is a state, not a crash
            error = str(exc)
    return templates.TemplateResponse(request, "configs.html", {
        "nodes": nodes, "error": error, "oxidized_url": settings.oxidized_url,
    })


@app.get("/configs/{node}", response_class=HTMLResponse)
def config_node(request: Request, node: str):
    import difflib

    settings = load_settings()
    if not settings.oxidized_url:
        raise HTTPException(503, "OXIDIZED_URL is not configured")
    want = request.query_params.get("v")        # oid to view ("current" = live)
    prev = request.query_params.get("prev")     # older oid => show a diff
    versions, text, diff_lines, error = [], None, None, None
    try:
        with _ox_client(settings) as client:
            # resolve against oxidized's own node list — the path segment never
            # reaches their API unvalidated
            known = {n.get("name") for n in _ox_nodes(client)}
            if node not in known:
                raise HTTPException(404, f"unknown oxidized node: {node}")
            r = client.get("/node/version.json", params={"node_full": node})
            r.raise_for_status()
            raw = r.json() or []
            for i, v in enumerate(raw):
                author = v.get("author")
                if isinstance(author, dict):
                    author = author.get("name") or author.get("email")
                versions.append({
                    "oid": v.get("oid", ""), "num": i + 1,
                    "date": v.get("date") or v.get("time"),
                    "message": (v.get("message") or v.get("subject") or "").strip(),
                    "author": author,
                    "prev": raw[i + 1].get("oid", "") if i + 1 < len(raw) else None,
                })
            by_oid = {v["oid"]: v for v in versions}
            if want == "current":
                r = client.get(f"/node/fetch/{node}")
                r.raise_for_status()
                text = r.text
            elif want and want in by_oid:
                new = _ox_version_text(client, node, by_oid[want], by_oid[want]["num"])
                if prev and prev in by_oid:
                    old = _ox_version_text(client, node, by_oid[prev], by_oid[prev]["num"])
                    diff_lines = []
                    for ln in difflib.unified_diff(
                            old.splitlines(), new.splitlines(),
                            fromfile=prev[:10], tofile=want[:10], lineterm=""):
                        cls = ("add" if ln.startswith("+") and not ln.startswith("+++") else
                               "del" if ln.startswith("-") and not ln.startswith("---") else
                               "hunk" if ln.startswith("@@") else "ctx")
                        diff_lines.append((cls, ln))
                else:
                    text = new
    except HTTPException:
        raise
    except Exception as exc:
        error = str(exc)
    return templates.TemplateResponse(request, "confignode.html", {
        "node": node, "versions": versions, "text": text,
        "diff_lines": diff_lines, "want": want, "prev": prev, "error": error,
    })


@app.get("/device/{name:path}", response_class=HTMLResponse)
def device(request: Request, name: str):
    # :path converter — inferred switch names embed interface names ("1/0/8")
    conn = _conn()
    try:
        db.init(conn)  # a fresh DB must 404 cleanly, not "no such table"
        dev = conn.execute("SELECT * FROM devices WHERE name = ?", (name,)).fetchone()
        if dev is None:
            row = conn.execute("SELECT canonical FROM aliases WHERE alias = ?", (name,)).fetchone()
            if row:
                from fastapi.responses import RedirectResponse
                return RedirectResponse(f"/device/{row['canonical']}")
            raise HTTPException(404, f"unknown device: {name}")
        all_ports = conn.execute(
            "SELECT * FROM interfaces WHERE device_id = ? ORDER BY ifindex, name",
            (dev["id"],),
        ).fetchall()
        show_all = request.query_params.get("all") == "1"
        ports = []
        hidden: dict[str, int] = {}
        for p in all_ports:
            kind = port_kind(p["name"])
            # physical and hypervisor kernel ports always shown (the latter
            # carry the management addresses); other logical ports only while up
            if show_all or kind in ("physical", "kernel") or p["oper_status"] == "up":
                ports.append(p)
            else:
                hidden[kind] = hidden.get(kind, 0) + 1
        # per-port VLAN membership, and where the answer came from — a switch's
        # own config, the hypervisor's port group, or the subnet an address
        # sits in. Naming the source is the difference between a number and a
        # number you can act on.
        WHERE = {
            "oxidized": "from the device's backed-up running-config",
            "vsphere": "from the hypervisor port group carrying this NIC — "
                       "the vSwitch adds the tag, so the guest OS can't see it",
        }
        pvrows: dict[str, dict] = {}
        for r in conn.execute(
                "SELECT interface, vid, tagged, source FROM port_vlans WHERE device = ?",
                (dev["name"],)):
            e = pvrows.setdefault(r["interface"], {"u": [], "t": [], "src": r["source"]})
            (e["t"] if r["tagged"] else e["u"]).append(r["vid"])
        port_vlans = {}
        for iface, e in pvrows.items():
            u = ",".join(map(str, sorted(e["u"])))
            t = ",".join(map(str, sorted(e["t"])))
            where = WHERE.get(e["src"], f"reported by {e['src']}")
            if e["t"]:
                port_vlans[iface] = {"mode": "trunk", "why": where,
                                     "text": (f"{u}u {t}" if u else t)}
            else:
                port_vlans[iface] = {"mode": "access", "text": u, "why": where}
        port_roles: dict[str, str] = {
            r["interface"]: (r["role"], r["detail"]) for r in conn.execute(
                "SELECT interface, role, detail FROM port_roles WHERE device = ?",
                (dev["name"],))}
        # L3 ports with no switching config (firewall NICs, mgmt interfaces):
        # the interface's own address places it in a subnet, and the subnet
        # maps to a VLAN — untagged from this port's point of view, so it
        # reads as access. Parsed config, where present, still wins above.
        subnet_vl = []
        for r in conn.execute("SELECT cidr, vlan FROM subnets WHERE vlan IS NOT NULL"):
            try:
                subnet_vl.append((ipaddress.ip_network(r["cidr"], strict=False), r["vlan"]))
            except ValueError:
                pass
        for p in ports:
            if p["ip"] and p["name"] not in port_vlans:
                try:
                    a = ipaddress.ip_address(p["ip"].split("/")[0])
                except ValueError:
                    continue
                vids = {v for net, v in subnet_vl if a in net}
                if vids:
                    port_vlans[p["name"]] = {
                        "mode": "access", "text": ",".join(map(str, sorted(vids))),
                        "why": "inferred from the subnet this port's address "
                               "sits in, not read from a device"}
        endpoints = conn.execute(
            "SELECT * FROM endpoints WHERE device = ? ORDER BY interface, hostname",
            (name,),
        ).fetchall()
        links = conn.execute(
            "SELECT * FROM links WHERE a_device = ? OR b_device = ? "
            "ORDER BY a_device, a_interface", (name, name),
        ).fetchall()
        children = conn.execute(
            "SELECT * FROM devices WHERE parent = ? ORDER BY status != 'up', name", (name,),
        ).fetchall()
        caps = {i: c for (d, i), c in load_settings().capacities.items() if d == name}
        return templates.TemplateResponse(request, "device.html", {
            "d": dev, "ports": ports, "hidden": hidden, "show_all": show_all,
            "port_vlans": port_vlans, "caps": caps, "port_roles": port_roles,
            "endpoints": endpoints, "links": links, "children": children,
        })
    finally:
        conn.close()
