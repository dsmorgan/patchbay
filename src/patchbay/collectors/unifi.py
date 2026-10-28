"""UniFi collector for the self-hosted Network application.

Auth: local admin + cookie login (POST /api/login). Self-hosted controllers
do not support API keys — that is a UniFi OS feature. APs become devices
(role=ap); wireless clients become endpoints attached to their AP.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx

from ..config import Settings
from .. import db
from . import register

NAME = "unifi"
DEVICE_STATES = {0: "down", 1: "up", 4: "upgrading", 5: "provisioning", 6: "heartbeat_missed"}

# UniFi API model codes → human-readable names.  The controller returns short
# codes (e.g. "U7PG2") that don't map cleanly to the product names shown in
# the UI; correct the ones we know about so patchbay displays useful labels.
MODEL_NAMES: dict[str, str] = {
    "U7PG2":   "AC Pro",       # UAP-AC-Pro / UAP-AC-Pro-Gen2 (same code)
    "U7PIW":   "AC In-Wall Pro",  # UAP-AC-IW-Pro
    "UAP6MP":  "U6 Pro",       # U6-Pro
    "U7PROXG": "U7 Pro XGS",   # U7-Pro-XGS (alternate code)
    "UAPA6A4": "U7 Pro XGS",   # U7-Pro-XGS (actual API code)
    "USWED77": "USW-Pro-10-PoE",
    "USPM16P": "USW-Pro-Max-16-PoE",
    "US8P150": "US-8-150W",
    "US8P60":  "US-8-60W",
}


def _bps(entry: dict, key: str, live: bool) -> int | None:
    """A UniFi `*-r` figure (bytes/s, rolling) as bits/s. Rates are
    liveness like speed and status: from a non-up device the controller's
    cached figure is not an observation, so it is omitted (#53)."""
    v = entry.get(key)
    return int(v * 8) if live and v is not None else None


ERR_STATE_KEY = "unifi_port_error_counters"
# the discard counters' own memory (#83), kept apart from the error one so
# a site upgrading mid-week keeps its error baseline
DROP_STATE_KEY = "unifi_port_drop_counters"
# port_table's absolute (rx, tx) counters for each rate_history pair
ERR_FIELDS = ("rx_errors", "tx_errors")
DROP_FIELDS = ("rx_dropped", "tx_dropped")
# 6x the default 300 s poll interval (Settings doesn't know the interval): a
# longer gap means missed polls, and a delta across it is not a rate
ERR_MAX_ELAPSED_S = 1800


def _counter_rates(prev: dict, now_ts: float, key: str, port: dict, live: bool,
                   seen: dict, fields: tuple[str, str] = ERR_FIELDS
                   ) -> tuple[float | None, float | None]:
    """Per-second rx/tx rates by delta against the previous poll's absolute
    counters named by `fields`: errors (#70) or discards (#83). Remembers
    this poll's counters in `seen` and returns (in, out), None for each
    direction with no rate: first sight of a port, a negative delta (counter
    wrap or device restart), non-positive elapsed time, or a non-live device
    (the counters are the controller's cache, and not remembered either, so
    a device coming back starts fresh instead of averaging over its
    outage)."""
    if not live:
        return None, None
    cur = [port.get(f) for f in fields]
    seen[key] = [now_ts, *cur]
    old = prev.get(key)
    if not old or len(old) != 3:
        return None, None
    elapsed = now_ts - old[0]
    if elapsed > ERR_MAX_ELAPSED_S:
        return None, None
    out: list[float | None] = []
    for before, after in zip(old[1:], cur):
        try:
            delta = after - before
        except TypeError:
            out.append(None)
            continue
        out.append(delta / elapsed if elapsed > 0 and delta >= 0 else None)
    return out[0], out[1]


SAMPLED_STATE_KEY = "unifi_rate_sampled"
# A port is sampled at most once per this many seconds (#117). The rates are
# the controller's rolling figures and the error rates come from counter
# deltas, so a manual "Poll now" seconds after a poll re-reads the same
# figures (a duplicate sample for the canary and the p95 baseline) or divides
# a one-error delta by a two-second gap (a spike). Well under the 300 s
# default interval, so a scheduled poll never trips it.
MIN_SAMPLE_GAP_S = 30


def _due(prev: dict, now_ts: float, key: str, seen: dict) -> bool:
    """Whether this port gets a sample on this poll. `seen` is next poll's
    `prev`: it records now_ts for a due port, and keeps the earlier time for
    one polled less than MIN_SAMPLE_GAP_S after it, so a run of quick
    repolls can't push the next sample out."""
    last = prev.get(key)
    if isinstance(last, (int, float)) and 0 <= now_ts - last < MIN_SAMPLE_GAP_S:
        seen[key] = last
        return False
    seen[key] = now_ts
    return True


def _sample(conn: sqlite3.Connection, device: str, iface: str,
            in_bps: int | None, out_bps: int | None,
            in_errors: float | None = None, out_errors: float | None = None,
            in_discards: float | None = None,
            out_discards: float | None = None) -> None:
    """One rate_history row when there is a rate to record: it feeds the
    load view's 24-hour peak, and the normalizer ages the table out. Error
    rates feed the port canaries; discard rates are only collected, for #86
    to judge (#83)."""
    db.record_rates(conn, device, iface, in_bps=in_bps, out_bps=out_bps,
                    in_errors=in_errors, out_errors=out_errors,
                    in_discards=in_discards, out_discards=out_discards)


def _temperature(d: dict) -> float | None:
    """Controller-reported thermals (#40): `general_temperature` (°C) on
    devices with sensors — hardware LibreNMS can't see, since UniFi gear
    doesn't speak ENTITY-SENSOR MIB. `has_temperature: false` means the
    field is a meaningless placeholder; a non-up device's reading is
    stale, and stale liveness is omitted, not written."""
    if d.get("has_temperature") is False:
        return None
    try:
        t = d.get("general_temperature")
        return float(t) if t is not None else None
    except (TypeError, ValueError):
        return None


class UnifiCollector:
    name = NAME

    def configured(self, settings: Settings) -> bool:
        return bool(settings.unifi_url and settings.unifi_user and settings.unifi_pass)

    def collect(self, settings: Settings, conn: sqlite3.Connection) -> str:
        base = settings.unifi_url.rstrip("/")
        site = "default"
        n_aps = n_clients = 0
        # every request first, then the writes: the caller's transaction
        # holds SQLite's write lock from the first write on (#109)
        with httpx.Client(verify=settings.tls_verify, timeout=20) as client:
            r = client.post(f"{base}/api/login",
                            json={"username": settings.unifi_user, "password": settings.unifi_pass})
            r.raise_for_status()

            # raise_for_status: a controller error envelope (LoginRequired…)
            # must fail loudly, not read as "0 APs"
            dr = client.get(f"{base}/api/s/{site}/stat/device")
            dr.raise_for_status()
            devices = dr.json().get("data", [])
            cr = client.get(f"{base}/api/s/{site}/stat/sta")
            cr.raise_for_status()
            clients = cr.json().get("data", [])

        # the write phase: no network I/O from here on
        db.save_raw(conn, source=NAME, endpoint="stat/device", payload=devices)
        try:
            prev_err = json.loads(db.get_state(conn, ERR_STATE_KEY) or "{}")
        except ValueError:
            prev_err = {}
        seen_err: dict[str, list] = {}
        try:
            prev_drop = json.loads(db.get_state(conn, DROP_STATE_KEY) or "{}")
        except ValueError:
            prev_drop = {}
        seen_drop: dict[str, list] = {}
        try:
            prev_sampled = json.loads(db.get_state(conn, SAMPLED_STATE_KEY) or "{}")
        except ValueError:
            prev_sampled = {}
        seen_sampled: dict[str, float] = {}
        err_ts = db.now()
        ap_by_mac: dict[str, str] = {}
        sw_mac_to_name: dict[str, str] = {}        # switch chassis MAC → device name
        sw_port_names: dict[str, dict[int, str]] = {}  # device name → {port_idx: port_name}
        # uplinks buffered until every switch's port names are known:
        # (device, local port name, upstream device, remote port index)
        pending_links: list[tuple[str, str, str, int]] = []
        n_switches = 0
        for d in devices:
            dev_type = d.get("type")
            dev_name = d.get("name") or d.get("mac")
            status = DEVICE_STATES.get(d.get("state"), "unknown")

            # A non-up device's port_table is the controller's cache, not
            # an observation: port oper/admin/speed are liveness and must
            # be omitted rather than written stale (same gate temperature
            # already had). Port names are identity and still land.
            live = status == "up"

            if dev_type == "usw":
                sw_id = db.upsert_device(
                    conn, name=dev_name, source=NAME,
                    mgmt_ip=d.get("ip"), vendor="Ubiquiti",
                    model=MODEL_NAMES.get(d.get("model"), d.get("model")),
                    os=f"unifi {d.get('version', '')}".strip(), role="switch",
                    status=status,
                    temperature=_temperature(d) if live else None,
                )
                if d.get("mac"):
                    sw_mac_to_name[d["mac"].lower()] = dev_name
                port_idx_map: dict[int, str] = {}
                for port in (d.get("port_table") or []):
                    idx = port.get("port_idx")
                    port_name = port.get("name") or (f"Port {idx}" if idx else None)
                    if not port_name:
                        continue
                    if idx is not None:
                        port_idx_map[idx] = port_name
                    speed = port.get("speed") or 0
                    in_bps = _bps(port, "rx_bytes-r", live)
                    out_bps = _bps(port, "tx_bytes-r", live)
                    db.upsert_interface(
                        conn, device_id=sw_id, name=port_name,
                        oper_status=("up" if port.get("up") else "down")
                                    if live else None,
                        admin_status=("up" if port.get("enable") else "down")
                                     if live else None,
                        speed_bps=(speed * 1_000_000 or None) if live else None,
                        in_bps=in_bps,
                        out_bps=out_bps,
                    )
                    key = json.dumps([dev_name, port_name])
                    if live and not _due(prev_sampled, err_ts, key, seen_sampled):
                        # a repoll inside the gap: no sample, and the
                        # counters' baselines stay where they were
                        for was, keep in ((prev_err, seen_err),
                                          (prev_drop, seen_drop)):
                            if key in was:
                                keep[key] = was[key]
                        continue
                    in_err, out_err = _counter_rates(
                        prev_err, err_ts, key, port, live, seen_err)
                    in_drop, out_drop = _counter_rates(
                        prev_drop, err_ts, key, port, live, seen_drop,
                        DROP_FIELDS)
                    _sample(conn, dev_name, port_name, in_bps, out_bps,
                            in_err, out_err, in_drop, out_drop)
                sw_port_names[dev_name] = port_idx_map
                # Switch-to-switch uplink (same LLDP data as APs). The
                # remote port name isn't knowable yet — the upstream
                # switch may come later in the device list — so buffer
                # the link and resolve names after the loop.
                uplink = d.get("uplink") or {}
                upstream = uplink.get("uplink_device_name")
                upstream_port = uplink.get("uplink_remote_port")
                local_idx = uplink.get("port_idx")
                if upstream and upstream_port is not None and local_idx is not None:
                    local_port = port_idx_map.get(local_idx, f"Port {local_idx}")
                    pending_links.append(
                        (dev_name, local_port, upstream, upstream_port))
                n_switches += 1
                continue

            if dev_type != "uap":
                continue
            ap_by_mac[d["mac"].lower()] = dev_name
            dev_id = db.upsert_device(
                conn, name=dev_name, source=NAME,
                mgmt_ip=d.get("ip"), vendor="Ubiquiti",
                model=MODEL_NAMES.get(d.get("model"), d.get("model")),
                os=f"unifi {d.get('version', '')}".strip(), role="ap",
                status=status,
                temperature=_temperature(d) if live else None,
            )
            uplink = d.get("uplink") or {}
            iface_name = uplink.get("name") or uplink.get("ifname") or "eth0"
            # the uplink carries the same rolling rates as a switch port;
            # where the switch is polled too, the load view shows the
            # busier of the two readings of that cable
            ap_in = _bps(uplink, "rx_bytes-r", live)
            ap_out = _bps(uplink, "tx_bytes-r", live)
            db.upsert_interface(
                conn, device_id=dev_id,
                name=iface_name,
                # the MAC is identity (it's how switch FDB inference finds
                # this AP); status and speed are liveness, gated like the
                # switch ports above
                oper_status=("up" if uplink.get("up") else
                             ("down" if uplink else None)) if live else None,
                speed_bps=((uplink.get("speed") or 0) * 1_000_000 or None)
                          if live else None,
                in_bps=ap_in, out_bps=ap_out,
                mac=d.get("mac"),
                description="uplink",
            )
            if not live or _due(prev_sampled, err_ts,
                                json.dumps([dev_name, iface_name]), seen_sampled):
                _sample(conn, dev_name, iface_name, ap_in, ap_out)
            # Physical link to the upstream switch via LLDP-discovered
            # port. Buffered like the switch uplinks so a renamed port
            # on the upstream switch resolves to its stored name.
            upstream = uplink.get("uplink_device_name")
            upstream_port = uplink.get("uplink_remote_port")
            if upstream and upstream_port is not None:
                pending_links.append(
                    (dev_name, iface_name, upstream, upstream_port))
            n_aps += 1

        db.set_state(conn, ERR_STATE_KEY, json.dumps(seen_err))
        db.set_state(conn, DROP_STATE_KEY, json.dumps(seen_drop))
        db.set_state(conn, SAMPLED_STATE_KEY, json.dumps(seen_sampled))

        # uplink_remote_port is a port index; resolve it through the
        # upstream switch's port_table so a renamed port still matches
        # the stored interface. "Port N" is the unmodified-USW fallback.
        for a_dev, a_if, upstream, remote_idx in pending_links:
            remote_port = sw_port_names.get(upstream, {}).get(
                remote_idx, f"Port {remote_idx}")
            db.upsert_link(conn, a_device=a_dev, a_interface=a_if,
                           b_device=upstream, b_interface=remote_port,
                           source=NAME)

        # Retire APs/switches forgotten by the controller — a device
        # removed there stayed on every page with frozen status forever.
        # Scoped to rows this collector owns (a merged record whose
        # primary source is librenms is that collector's to retire), and
        # guarded on a non-empty listing like every other prune. Stored
        # names are post-normalize canonical, so match both forms.
        if devices:
            from ..normalize import canonical_name
            seen: set[str] = set()
            for d in devices:
                n = d.get("name") or d.get("mac")
                if n and d.get("type") in ("usw", "uap"):
                    seen |= {n, canonical_name(n)}
            if seen:
                conn.execute(
                    f"DELETE FROM devices WHERE source = ? "
                    f"AND role IN ('ap', 'switch') "
                    f"AND name NOT IN ({','.join('?' * len(seen))})",
                    (NAME, *sorted(seen)))

        db.save_raw(conn, source=NAME, endpoint="stat/sta", payload=clients)
        fdb_rows: list[tuple[str, str, str]] = []
        for c in clients:
            mac = (c.get("mac") or "").lower()
            if not mac:
                continue
            ap_name = ap_by_mac.get((c.get("ap_mac") or "").lower())
            db.upsert_endpoint(
                conn, mac=mac, source=NAME,
                ip=c.get("ip") or None,
                hostname=c.get("name") or c.get("hostname") or None,
                device=ap_name,
                interface=c.get("essid") or None,   # SSID as the "port" for wireless
                vlan=c.get("vlan"),
            )
            n_clients += 1
            # wired clients carry sw_mac + sw_port — write into fdb so
            # normalize's fdb-uplink inference can place them on switch ports
            if c.get("is_wired"):
                sw_mac = (c.get("sw_mac") or "").lower()
                sw_port_idx = c.get("sw_port")
                if sw_mac and sw_port_idx is not None:
                    sw_name = sw_mac_to_name.get(sw_mac)
                    port_name = sw_port_names.get(sw_name or "", {}).get(sw_port_idx)
                    if sw_name and port_name:
                        fdb_rows.append((sw_name, port_name, mac))
        if clients:  # same empty-response guard as librenms fdb
            conn.execute("DELETE FROM fdb WHERE source = 'unifi'")
            conn.executemany(
                "INSERT OR IGNORE INTO fdb (device, interface, mac, source) VALUES (?, ?, ?, 'unifi')",
                fdb_rows,
            )
            # a client absent from the controller's current station list
            # is no longer on any of these APs: clear the stale AP
            # attribution but keep the endpoint — its identity (and any
            # ARP-refreshed liveness) is still real. Without this, a
            # laptop that went wired kept counting as a wireless client
            # until the endpoint TTL fired.
            seen_macs = {(c.get("mac") or "").lower() for c in clients}
            ap_names = [d["name"] for d in conn.execute(
                "SELECT name FROM devices WHERE source = ? AND role = 'ap'",
                (NAME,))]
            if ap_names:
                q = ",".join("?" * len(ap_names))
                stale = [r["mac"] for r in conn.execute(
                    f"SELECT mac FROM endpoints WHERE device IN ({q})",
                    ap_names) if r["mac"] not in seen_macs]
                if stale:
                    qm = ",".join("?" * len(stale))
                    conn.execute(
                        f"UPDATE endpoints SET device = NULL, interface = NULL "
                        f"WHERE mac IN ({qm})", stale)
        parts = []
        if n_switches:
            parts.append(f"{n_switches} switches")
        parts.append(f"{n_aps} APs, {n_clients} clients, {len(fdb_rows)} fdb")
        return ", ".join(parts)


register(UnifiCollector())
