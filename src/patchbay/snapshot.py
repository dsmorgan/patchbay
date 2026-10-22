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
import json
import re
import shutil
import time
from pathlib import Path

import httpx

from . import db
from . import routed
from .config import Settings
from .redact import redact_xml, strip_stamps
from .retention import alert_prunable, classify
from .ports import port_kind

# a line whose remainder follows one of these introduces a secret — keep the
# context up to the keyword, redact the rest. Overzealous by design: this
# file lands on cloud storage, so err toward redacting too much.
SECRET_PAT = re.compile(
    # keyword suffixes stop at '=': in a `wpa2-pre-shared-key=...` pair the
    # value is the secret, and \S* swallowed it as part of the keyword
    r"(password|passwd|secret|community|pre-?shared-key|psk\b|wpa[\w-]*|"
    r"passphrase|tcp-md5-key|"
    r"auth(?:entication)?(?:-| )?(?:key|password|md5|sha|text)|"
    # HSRP/VRRP: `standby 1 authentication SECRET`, no keyword before it
    r"(?:standby|vrrp)(?: \d+)? authentication|"
    r"priv(?:acy)?(?:-| )?(?:key|password|protocol)|"
    r"encrypted|private-key|otp[\w-]*|totp[\w-]*|\bseed\b|"
    r"key-string|message-digest-key|(?:isakmp|tunnel) key\b|"
    # a bare `key 7 ...` inside an IOS-XE `tacacs server`/`radius server`
    # block, and the per-group `server-private ADDR ... key ...`
    r"^\s+key(?=\s)|server-private \S+|"
    # OpenWrt UCI: `option key '...'`, WEP key1-4, WireGuard private_key and
    # preshared_key, any psk option; Aruba `mgmt-user NAME [ROLE] PASSWORD`
    r"\boption \w*(?:key|psk)\w*|mgmt-user \S+|"
    # the community string of `snmp-server host` follows the address and
    # an optional notification type and version, with no keyword before it
    r"snmp-server host \S+(?: (?:traps|informs))?(?: version (?:1|2c|3 \S+))?|"
    r"snmp-server user \S+|tacacs|radius[- ]server)",
    re.I)
HASH_PAT = re.compile(r"\$\d+\w*\$\S+")  # $1$salt$hash and friends
# any PEM key block, private or not: RSA/EC/ENCRYPTED PRIVATE KEY, an
# OpenVPN static key, a PGP key block. Every gap is bounded, so a marker
# search is linear in the line: an unbounded `.*?` after each BEGIN/END word
# rescanned the rest of the line per word, and a long one-line config full
# of "end" took seconds. The bare form (no dashes) is only tried on a line
# that says PRIVATE KEY at all.
_PEM_MARK = re.compile(r"-----(BEGIN|END) [^-\n]{0,80}?\bKEY\b[^-\n]{0,40}-----", re.I)
_BARE_MARK = re.compile(r"\b(BEGIN|END)\b[^\n]{0,64}?(?-i:PRIVATE KEY)", re.I)


def _key_markers(line: str) -> tuple[bool, str | None]:
    """(whether the line opens a key block anywhere, the kind of its last
    marker: 'BEGIN', 'END', or None)."""
    found = list(_PEM_MARK.finditer(line))
    if "PRIVATE KEY" in line:
        found += _BARE_MARK.finditer(line)
    if not found:
        return False, None
    kinds = [m.group(1).upper() for m in found]
    last = max(found, key=lambda m: m.start()).group(1).upper()
    return "BEGIN" in kinds, last


def scrub_config(text: str) -> tuple[str, int]:
    """Redact secrets from a device config; returns (scrubbed text, count).
    Secret-bearing XML elements (a config.xml Oxidized backed up from a
    firewall) go through the same element rules as patchbay's own firewall
    history first; then each line is checked for secret keywords. The
    stamps patchbay's history carries become the constant placeholder: this
    file leaves the host, and a short stamp of a weak secret can be tested
    against guesses."""
    text, redacted = redact_xml(strip_stamps(text))
    out: list[str] = []
    in_key_block = False
    for line in text.splitlines():
        opens, last = _key_markers(line)
        if in_key_block or opens:
            if not in_key_block:
                redacted += 1
                out.append("<redacted: private key block>")
            # the line's LAST marker decides: a one-liner that ends its block
            # doesn't swallow what follows, and an END followed by another
            # BEGIN stays inside a block
            if last:
                in_key_block = last == "BEGIN"
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


# A LibreNMS that doesn't answer must not hold a break-glass snapshot hostage:
# an unreachable host fails in 3 s and the first failure ends graph fetching,
# while the 15 s read leaves room for a slow-rendering LibreNMS (a 24 h graph
# on a busy server) so the daily snapshot doesn't lose every graph.
GRAPH_TIMEOUT = httpx.Timeout(15.0, connect=3.0)


def generate(settings: Settings) -> str:
    """Render the snapshot HTML. Requires the web extra (jinja2/fastapi) —
    the generator reuses the live UI's graph builder, templates, and proxies."""
    from . import web  # deferred: pulls fastapi

    conn = web._conn()
    try:
        db.init(conn)
        # Every read below names its table. Never add a generic dump: the
        # alert_* tables hold webhook URLs, which are credentials (ADR-0003
        # Decision 2), and test_transports proves none reaches this file.
        # a demo-seeded model gets a shareable banner instead of the
        # treat-as-sensitive one — nothing in it is real
        is_demo = db.get_state(conn, "demo_seed") == "1"
        graph_json, peak_ready = web.build_topology_graph(conn, settings)
        # the routed view rides along (#50): same builder and template as
        # /routed, the way the map reuses the topology's
        routed_json = web._script_safe_json(routed.build_routed_graph(conn, settings))
        ages = web.source_ages(conn)
        totals = web.device_totals(conn)   # frozen at generation, like ages
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
        tunnels_by_dev: dict[str, list[dict]] = {}
        for r in conn.execute("SELECT * FROM tunnels ORDER BY device, type, name"):
            tunnels_by_dev.setdefault(r["device"], []).append(dict(r))
        n_ipam = conn.execute("SELECT COUNT(*) FROM ipam_addresses").fetchone()[0]
        # patchbay-held firewall history (#23): the latest revision per
        # device. Stored text is already redacted at capture (secret tags +
        # long-blob digests); scrub_config runs over it again below anyway —
        # this file lands on cloud-synced storage, and two independent
        # layers beat one.
        fw_configs = [dict(r) for r in conn.execute(
            "SELECT device, text FROM config_revisions cr "
            "WHERE fetched_at = (SELECT MAX(fetched_at) FROM config_revisions "
            "WHERE device = cr.device) ORDER BY device")]
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
    graphs_error: str | None = None
    if settings.librenms_url and settings.librenms_token:
        for dev, iface in sorted(linked_ports):
            try:
                got = web.fetch_graph_image(settings, dev, iface, "port_bits", 24, 620,
                                            timeout=GRAPH_TIMEOUT)
            except web.GraphSourceError as e:
                # LibreNMS is down — the outage this file exists for. Skip
                # every remaining graph (one dead host costs one timeout, not
                # one per port) and say so where the graphs would be.
                graphs_error = str(e)
                break
            if got:
                graphs_by_dev.setdefault(dev, []).append(
                    {"iface": iface, "uri": _data_uri(*got)})
    # latest configs: patchbay-held firewall revisions first, then Oxidized,
    # all scrubbed; an unreachable Oxidized means a snapshot without its
    # configs, never no snapshot.
    configs: list[dict] = []
    for fw in fw_configs:
        text, redacted = scrub_config(fw["text"])
        configs.append({"name": fw["device"], "text": text,
                        "redacted": redacted, "lines": text.count("\n") + 1})
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
        d["tunnels"] = tunnels_by_dev.get(d["name"], [])
        d["graphs"] = graphs_by_dev.get(d["name"], [])
        d["graphs_missing"] = bool(graphs_error) and not d["graphs"] and any(
            dev == d["name"] for dev, _ in linked_ports)

    d3_js = (Path(__file__).parent / "static" / "d3.v7.min.js").read_text(encoding="utf-8")
    # the UI typeface rides along as a data URI, so the snapshot is set in
    # the same face as the live pages with the network down
    font = (Path(__file__).parent / "static" / "fonts" / "ibm-plex-sans-latin-var.woff2").read_bytes()
    font_url = "data:font/woff2;base64," + base64.b64encode(font).decode()
    return web.templates.env.get_template("snapshot.html").render(
        graph_json=graph_json, routed_json=routed_json, peak_ready=peak_ready, d3_js=d3_js,
        graphs_error=graphs_error, generated=time.strftime("%Y-%m-%d %H:%M %Z"), ages=ages,
        devices=devices, links=links, vlans=vlans, subnets=subnets,
        endpoints=endpoints, gateways=gateways, configs=configs,
        n_ipam=n_ipam, is_demo=is_demo, font_url=font_url, totals=totals,
        tunnel_labels=web.TUNNEL_TYPE_LABEL, now=db.now())


class DeliveryError(Exception):
    """The snapshot was written locally but couldn't be copied off-box. Raised
    only after the local file is safe, so callers report it without implying
    the snapshot was lost. `path` is that local file, when known."""

    path: Path | None = None


def write_snapshot(settings: Settings, out: str | None = None, *,
                   alert_keep: int | None = None, stamp: float | None = None) -> Path:
    """Generate and write. With no explicit path: timestamped file in
    PATCHBAY_SNAPSHOT_DIR, plus a stable patchbay-latest.html copy (a fixed
    name is what a sync target or reverse proxy wants to point at), pruning
    timestamped snapshots no PATCHBAY_SNAPSHOT_KEEP tier claims, then
    delivering to PATCHBAY_SNAPSHOT_DELIVER_DIR when one is configured.

    With `alert_keep` (#66), the file is an alert snapshot,
    patchbay-YYYYMMDD-HHMMSS-alert.html, and the alert snapshots beyond the
    newest `alert_keep` (0 = unlimited) are pruned in both directories. The
    tiers never see an alert snapshot, and the alert count never sees a
    tiered one (retention.py). `stamp` names the file from that time rather
    than the clock after rendering: an alert snapshot passes its claimed
    cooldown time, which no other claim shares to the second."""
    html = generate(settings)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8", newline="\n")
        return path
    d = Path(settings.snapshot_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = "patchbay-%Y%m%d-%H%M%S" + ("-alert" if alert_keep is not None else "") + ".html"
    path = d / time.strftime(name, time.localtime(stamp))
    path.write_text(html, encoding="utf-8", newline="\n")
    (d / "patchbay-latest.html").write_text(html, encoding="utf-8", newline="\n")
    prune(settings, d, alert_keep)
    if settings.snapshot_deliver_dir:
        try:
            deliver(settings, path, alert_keep)
        except DeliveryError as e:
            e.path = path
            raise
    return path


def deliver(settings: Settings, path: Path, alert_keep: int | None = None) -> None:
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
        prune(settings, dest, alert_keep)
    except OSError as e:
        raise DeliveryError(f"{dest}: {e}") from e


def prune(settings: Settings, d: Path, alert_keep: int | None = None) -> list[str]:
    """Delete the timestamped snapshots in one directory that no retention
    tier claims; returns the names removed. Each directory is judged on its
    own files, so a delivery share that missed a night still keeps its own
    first-of-month. An unparsed spec prunes nothing. With `alert_keep`, the
    alert snapshots beyond that count go too, judged on their own."""
    names = [p.name for p in d.iterdir()]
    doomed = (classify(names, settings.snapshot_keep)[1]
              if settings.snapshot_keep is not None else [])
    if alert_keep is not None:
        doomed = doomed + alert_prunable(names, alert_keep)
    for name in doomed:
        (d / name).unlink()
    return doomed


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


# -- snapshot on critical (#66, ADR-0003 Decision 7) --------------------------
# Off unless PATCHBAY_ALERT_SNAPSHOT is on. A crit alert that is raised or
# escalated takes a snapshot after the poll commits, routed or not (a
# silenced one produces no notification at all), at most once per
# PATCHBAY_ALERT_SNAPSHOT_COOLDOWN whatever raised it. The poller is a fresh
# process each cycle, so the cooldown's clock lives in app_state.

ALERT_LAST_KEY = "alert_snapshot_at"             # epoch of the last attempt
ALERT_LOG_KEY = "alert_snapshots"                # sidecar: file -> its cause
# the sidecar names at most this many causes per file, and remembers at
# most this many files: an unlimited keep count lists the oldest as unknown
ALERT_CAUSES_MAX = 5
ALERT_LOG_MAX = 200
_TRIGGER_KINDS = ("raise", "escalate")


def alert_triggers(notes) -> list:
    """The notifications that call for a snapshot: a crit raised, or an
    alert escalated to crit. Pass the engine's notes before routing: a crit
    routed `none` still takes one (the owner's call on #66), and a silenced
    alert (#62) yields no note, so it never does."""
    return [n for n in notes if n.kind in _TRIGGER_KINDS and n.severity == "crit"]


def alert_log(conn) -> dict[str, dict]:
    """The sidecar: alert snapshot file name -> {ts, alerts: [...]}."""
    raw = db.get_state(conn, ALERT_LOG_KEY)
    try:
        entries = json.loads(raw) if raw else []
    except ValueError:
        return {}
    if not isinstance(entries, list):
        return {}
    return {e["name"]: e for e in entries
            if isinstance(e, dict) and isinstance(e.get("name"), str)}


def _immediate(conn) -> None:
    """Take the write lock before reading: the poller and /ops/poll can run
    this at once, and a deferred read-check-write lets both through."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")


def _claim_cooldown(conn, now: float, cooldown_min: int) -> float | None:
    """Atomically check and start the cooldown. Returns None when claimed,
    or the time the open window closes."""
    _immediate(conn)
    try:
        try:
            last = float(db.get_state(conn, ALERT_LAST_KEY) or 0)
        except ValueError:
            last = 0.0
        # at least a second even with no cooldown: the file is named from
        # the claimed second, so two claims must never share one
        window = max(cooldown_min * 60, 1)
        if now - last < window:
            conn.rollback()
            return last + window
        # the attempt starts the window, so a snapshot that keeps failing is
        # one failure event per cooldown, not one per poll
        db.set_state(conn, ALERT_LAST_KEY, repr(now))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return None


def _log_alert_snapshot(conn, name: str, ts: float, triggers: list) -> None:
    # key, rule, severity, and the item's text: what the alerts page already
    # shows, never a transport or its URL. Under the write lock, so two
    # writers can't each drop the other's entry.
    causes = [{"key": n.key, "rule": n.rule, "kind": n.kind, "text": n.text[:200]}
              for n in triggers[:ALERT_CAUSES_MAX]]
    _immediate(conn)
    try:
        entries = [e for e in alert_log(conn).values() if e["name"] != name]
        entries.append({"name": name, "ts": ts, "alerts": causes,
                        "more": max(0, len(triggers) - ALERT_CAUSES_MAX)})
        db.set_state(conn, ALERT_LOG_KEY, json.dumps(entries[-ALERT_LOG_MAX:]))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def take_alert_snapshot(settings: Settings, notes, *, now: float | None = None) -> list[str]:
    """Run after dispatch: when PATCHBAY_ALERT_SNAPSHOT is on, write an alert
    snapshot if `notes` holds a crit trigger and the cooldown has passed. Returns poll-output lines and never
    raises: a failure is recorded for the snapshot-failed rule (trigger
    `alert`) and the poll goes on."""
    if not settings.alert_snapshot:
        return []
    triggers = alert_triggers(notes)
    if not triggers:
        return []
    now = db.now() if now is None else now
    try:
        with db.connect(settings.db_path) as conn:
            until = _claim_cooldown(conn, now, settings.alert_snapshot_cooldown)
    except Exception as e:
        return [f"[warn] alert snapshot: {type(e).__name__}: {e}"]
    if until is not None:
        return ["[ok]   alert snapshot: in cooldown until "
                + time.strftime("%H:%M", time.localtime(until))]

    lines: list[str] = []
    path: Path | None = None
    failure = None
    try:
        path = write_snapshot(settings, alert_keep=settings.alert_snapshot_keep, stamp=now)
        lines.append(f"[ok]   alert snapshot: {path}")
    except DeliveryError as e:
        path = e.path
        lines.append(f"[warn] alert snapshot written but delivery failed: {e}")
        failure = (e, "undelivered")
    except Exception as e:
        lines.append(f"[warn] alert snapshot failed: {e}")
        failure = (e, "failed")
    try:
        with db.connect(settings.db_path) as conn:
            if failure:
                db.record_snapshot_failure(conn, failure[0], kind=failure[1],
                                           trigger="alert")
                conn.commit()
            if path is not None:
                _log_alert_snapshot(conn, path.name, now, triggers)
    except Exception as e:
        lines.append(f"[warn] alert snapshot record: {type(e).__name__}: {e}")
    return lines
