"""Break-glass snapshot generator (phase 5).

One fully self-contained HTML file: the interactive topology map (d3 inlined),
every device with its ports, links, VLANs, subnets, endpoints (ARP/leases),
gateways, 24h traffic graphs for linked ports (data URIs), and the latest
device configs — redacted. Zero external requests; openable from a laptop
with no connectivity. A snapshot ends up on cloud-synced storage: configs are
scrubbed on the way in, and the whole file is still treated as leakable.
"""

from __future__ import annotations

import base64
import re
import shutil
import time
from pathlib import Path

import httpx

from . import db
from .config import Settings
from .ports import port_kind

# a line whose remainder follows one of these introduces a secret — keep the
# context up to the keyword, redact the rest. Overzealous by design: this
# file lands on cloud storage, so err toward redacting too much.
SECRET_PAT = re.compile(
    r"(password|passwd|secret|community|pre-shared-key|psk\b|wpa\S*|"
    r"auth(?:entication)?(?:-| )?(?:key|password|md5|sha)|"
    r"priv(?:acy)?(?:-| )?(?:key|password|protocol)|"
    r"encrypted|private-key|snmp-server user \S+|tacacs|radius[- ]server)",
    re.I)
HASH_PAT = re.compile(r"\$\d+\w*\$\S+")  # $1$salt$hash and friends


def scrub_config(text: str) -> tuple[str, int]:
    """Redact secrets from a device config; returns (scrubbed text, count)."""
    out: list[str] = []
    redacted = 0
    in_key_block = False
    for line in text.splitlines():
        if "PRIVATE KEY" in line and "BEGIN" in line.upper():
            in_key_block = True
            redacted += 1
            out.append("<redacted: private key block>")
            continue
        if in_key_block:
            if "PRIVATE KEY" in line and "END" in line.upper():
                in_key_block = False
            continue
        if HASH_PAT.search(line):
            out.append(HASH_PAT.sub("<redacted>", line))
            redacted += 1
            continue
        m = SECRET_PAT.search(line)
        if m and line[m.end():].strip():
            out.append(line[:m.end()] + " <redacted>")
            redacted += 1
        else:
            out.append(line)
    return "\n".join(out), redacted


def _data_uri(body: bytes | str, ctype: str) -> str:
    raw = body.encode() if isinstance(body, str) else body
    return f"data:{ctype.split(';')[0]};base64,{base64.b64encode(raw).decode()}"


def generate(settings: Settings) -> str:
    """Render the snapshot HTML. Requires the web extra (jinja2/fastapi) —
    the generator reuses the live UI's graph builder, templates, and proxies."""
    from . import web  # deferred: pulls fastapi

    conn = web._conn()
    try:
        db.init(conn)
        # a demo-seeded model gets a shareable banner instead of the
        # treat-as-sensitive one — nothing in it is real
        is_demo = db.get_state(conn, "demo_seed") == "1"
        graph_json, peak_ready = web.build_topology_graph(conn, settings)
        ages = web._age(conn)
        devices = [dict(r) for r in conn.execute(
            "SELECT * FROM devices ORDER BY CASE role "
            "WHEN 'firewall' THEN 0 WHEN 'switch' THEN 1 WHEN 'hypervisor' THEN 2 "
            "WHEN 'ap' THEN 3 WHEN 'unmanaged-switch' THEN 4 ELSE 5 END, name")]
        ports_by_dev: dict[str, list[dict]] = {}
        for r in conn.execute(
                "SELECT d.name AS dev, i.* FROM interfaces i "
                "JOIN devices d ON d.id = i.device_id ORDER BY i.ifindex, i.name"):
            # same visibility rule as the device page: physical and kernel
            # ports always, other logical ones only while up
            if port_kind(r["name"]) in ("physical", "kernel") or r["oper_status"] == "up":
                ports_by_dev.setdefault(r["dev"], []).append(dict(r))
        links = [dict(r) for r in conn.execute(
            "SELECT * FROM links ORDER BY a_device, a_interface")]
        vlans = [dict(r) for r in conn.execute(
            "SELECT v.vid, v.name, COUNT(dv.device) AS devices FROM vlans v "
            "LEFT JOIN device_vlans dv ON dv.vid = v.vid "
            "GROUP BY v.vid ORDER BY v.vid")]
        subnets = [dict(r) for r in conn.execute("SELECT * FROM subnets ORDER BY cidr")]
        endpoints = [dict(r) for r in conn.execute(
            "SELECT * FROM endpoints ORDER BY hostname IS NULL, hostname, mac")]
        gateways = [dict(r) for r in conn.execute("SELECT * FROM gateways ORDER BY name")]
        n_ipam = conn.execute("SELECT COUNT(*) FROM ipam_addresses").fetchone()[0]
    finally:
        conn.close()

    # 24h traffic graphs for every port that carries a known link — the ports
    # you actually reach for during an outage
    linked_ports: set[tuple[str, str]] = set()
    for l in links:
        for dev, iface in ((l["a_device"], l["a_interface"]),
                           (l["b_device"], l["b_interface"])):
            if iface and iface not in ("?", "") and not dev.startswith("unmanaged@"):
                linked_ports.add((dev, iface))
    graphs_by_dev: dict[str, list[dict]] = {}
    for dev, iface in sorted(linked_ports):
        got = web.fetch_graph_image(settings, dev, iface, "port_bits", 24, 620)
        if got:
            graphs_by_dev.setdefault(dev, []).append(
                {"iface": iface, "uri": _data_uri(*got)})

    # latest configs via Oxidized, scrubbed; an unreachable Oxidized means a
    # snapshot without configs, never no snapshot
    configs: list[dict] = []
    if settings.oxidized_url:
        try:
            with web._ox_client(settings) as client:
                for node in web._ox_nodes(client):
                    status = (node.get("last") or {}).get("status") or node.get("status")
                    if status != "success":
                        continue
                    full = node.get("full_name") or node.get("name")
                    cr = client.get(f"/node/fetch/{full}")
                    if cr.status_code != 200:
                        continue
                    text, redacted = scrub_config(cr.text)
                    configs.append({"name": node.get("name") or full, "text": text,
                                    "redacted": redacted,
                                    "lines": text.count("\n") + 1})
        except httpx.HTTPError:
            pass

    for d in devices:
        d["ports"] = ports_by_dev.get(d["name"], [])
        d["graphs"] = graphs_by_dev.get(d["name"], [])

    d3_js = (Path(__file__).parent / "static" / "d3.v7.min.js").read_text(encoding="utf-8")
    # the UI typeface rides along as a data URI, so the snapshot is set in
    # the same face as the live pages with the network down
    font = (Path(__file__).parent / "static" / "fonts" / "ibm-plex-sans-latin-var.woff2").read_bytes()
    font_url = "data:font/woff2;base64," + base64.b64encode(font).decode()
    return web.templates.env.get_template("snapshot.html").render(
        graph_json=graph_json, peak_ready=peak_ready, d3_js=d3_js,
        generated=time.strftime("%Y-%m-%d %H:%M %Z"), ages=ages,
        devices=devices, links=links, vlans=vlans, subnets=subnets,
        endpoints=endpoints, gateways=gateways, configs=configs,
        n_ipam=n_ipam, is_demo=is_demo, font_url=font_url)


class DeliveryError(Exception):
    """The snapshot was written locally but couldn't be copied off-box. Raised
    only after the local file is safe, so callers report it without implying
    the snapshot was lost."""


def write_snapshot(settings: Settings, out: str | None = None) -> Path:
    """Generate and write. With no explicit path: timestamped file in
    PATCHBAY_SNAPSHOT_DIR, plus a stable patchbay-latest.html copy (a fixed
    name is what a sync target or reverse proxy wants to point at), pruning
    timestamped snapshots beyond PATCHBAY_SNAPSHOT_KEEP, then delivering to
    PATCHBAY_SNAPSHOT_DELIVER_DIR when one is configured."""
    html = generate(settings)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8", newline="\n")
        return path
    d = Path(settings.snapshot_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / time.strftime("patchbay-%Y%m%d-%H%M%S.html")
    path.write_text(html, encoding="utf-8", newline="\n")
    (d / "patchbay-latest.html").write_text(html, encoding="utf-8", newline="\n")
    if settings.snapshot_keep > 0:
        for old in sorted(d.glob("patchbay-2*.html"))[:-settings.snapshot_keep]:
            old.unlink()
    if settings.snapshot_deliver_dir:
        deliver(settings, path)
    return path


def deliver(settings: Settings, path: Path) -> None:
    """Copy a finished snapshot to the off-box destination. Writes to a
    temporary name first and renames, so a half-copied 4 MB file is never
    what a sync client picks up."""
    dest = Path(settings.snapshot_deliver_dir or "")
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for name in (path.name, "patchbay-latest.html"):
            tmp = dest / f".{name}.part"
            shutil.copyfile(path, tmp)
            tmp.replace(dest / name)
        if settings.snapshot_keep > 0:
            for old in sorted(dest.glob("patchbay-2*.html"))[:-settings.snapshot_keep]:
                old.unlink()
    except OSError as e:
        raise DeliveryError(f"{dest}: {e}") from e


def due_today(conn, settings: Settings) -> bool:
    """True when the daily snapshot time has passed and today's hasn't run.
    The poller is a fresh process each cycle, so 'has it run' lives in the DB."""
    if not settings.snapshot_at:
        return False
    try:
        hh, mm = (int(x) for x in settings.snapshot_at.split(":", 1))
    except ValueError:
        return False
    now = time.localtime()
    today = time.strftime("%Y-%m-%d", now)
    if db.get_state(conn, "snapshot_day") == today:
        return False
    return (now.tm_hour, now.tm_min) >= (hh, mm)


def mark_done(conn) -> None:
    db.set_state(conn, "snapshot_day", time.strftime("%Y-%m-%d"))
