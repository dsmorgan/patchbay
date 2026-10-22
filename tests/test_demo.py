"""The demo network: fictional, self-contained, and every page renders from
it. This is the path a stranger evaluates patchbay through, so it gets the
same page-smoke treatment as a real model."""

import sqlite3

from fastapi.testclient import TestClient

from patchbay import db as pdb
from patchbay import demo

PAGES = ["/", "/topology", "/vlans", "/configs", "/drift", "/patchpanel",
         "/ops", "/device/fw1", "/device/core1", "/device/hyp1"]


def _seed(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    summary = demo.seed(c)
    c.commit()
    return c, summary


def test_demo_seeds_a_working_model(tmp_path):
    c, summary = _seed(str(tmp_path / "demo.db"))
    assert "unmanaged inferred" in summary
    # the normalizer, not the seed, produced the derived rows
    assert c.execute("SELECT COUNT(*) FROM endpoints WHERE source='fdb'"
                     ).fetchone()[0] > 0
    assert c.execute("SELECT COUNT(*) FROM links WHERE source='declared'"
                     ).fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM devices WHERE role='unmanaged-switch'"
                     ).fetchone()[0] == 1
    # the firewall guest's VLAN came through the hypervisor port-group path
    assert c.execute("SELECT vid FROM port_vlans WHERE device='fw1' "
                     "AND interface='vmx2' AND source='vsphere'").fetchone()[0] == 20
    assert pdb.get_state(c, demo.MARKER) == "1"
    c.close()


def test_demo_pages_render(clean_env, tmp_path):
    path = str(tmp_path / "demo.db")
    _seed(path)[0].close()
    clean_env.setenv("PATCHBAY_DB", path)
    import patchbay.web as web
    client = TestClient(web.app)
    for p in PAGES:
        r = client.get(p)
        assert r.status_code == 200, (p, r.status_code)
    # every drift category has a finding to show
    drift = client.get("/drift").text
    for marker in ("esp32-a1b2c3", "lab-bench", "web-staging"):
        assert marker in drift, marker


def test_demo_contains_no_real_site_data(tmp_path):
    """The demo is headed for screenshots and a public snapshot; the repo
    rule (no real network data, RFC 5737/3849 addresses only) applies to it
    doubly. MACs must be locally administered."""
    import re

    c, _ = _seed(str(tmp_path / "demo.db"))
    ips = [r[0] for r in c.execute(
        "SELECT mgmt_ip FROM devices WHERE mgmt_ip IS NOT NULL UNION "
        "SELECT ip FROM endpoints WHERE ip IS NOT NULL UNION "
        "SELECT ip FROM ipam_addresses")]
    ok = re.compile(r"^(192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|2001:db8:"
                    r"|198\.18\.)")
    bad = [ip for ip in ips if not ok.match(ip)]
    assert not bad, bad
    macs = [r[0] for r in c.execute(
        "SELECT mac FROM endpoints UNION SELECT mac FROM interfaces "
        "WHERE mac IS NOT NULL")]
    assert all(m.startswith("02:") for m in macs if m), \
        [m for m in macs if m and not m.startswith("02:")]
    c.close()


def test_demo_is_deterministic(tmp_path):
    """Same seed, same model — screenshots and docs stay reproducible."""
    rows = []
    for name in ("a.db", "b.db"):
        c, _ = _seed(str(tmp_path / name))
        rows.append(set(c.execute(
            "SELECT a_device, a_interface, b_device, b_interface, source "
            "FROM links").fetchall()) | set(c.execute(
                "SELECT mac, ip, hostname FROM endpoints").fetchall()))
        c.close()
    assert rows[0] == rows[1]


def test_demo_routed_view_finds_the_uplink(clean_env, tmp_path):
    """The demo exercises WAN discovery, the participation filter, and the
    tunnel egress (#42): the uplink VLAN (no subnet record) resolves as the
    wan rail from the default route's exit interface, the IPAM aggregate
    never rails, and the site-b subnet rails only because the WireGuard
    route reaches it."""
    from patchbay.config import load_settings
    from patchbay.routed import build_routed_graph

    c, _ = _seed(str(tmp_path / "demo.db"))
    g = build_routed_graph(c, load_settings())
    by = {r["key"]: r for r in g["rails"]}
    assert g["default"]["rail"] == "v199"
    assert by["v199"]["wan"] is True
    assert "net:2001:db8::/32" not in by               # aggregate filtered
    assert by["net:172.16.44.0/24"]["via_tunnel"] == ["site-b · wg-peer"]
    assert g["tunnels"][0]["rails"] == ["net:172.16.44.0/24"]
    assert g["tunnels"][0]["label"] == "WireGuard"
    c.close()


# --- phase 6: the Alerts page has something on every tab (#119) --------------

ALERT_TABS = {
    "active": "in errors at",              # the canary's flood
    "history": "notified",
    "rules": "port-canary",
    "tunnels": "site-b",
    "transports": "demo-webhook",
    "silenced": "IPAM conflicts",
    "silences": "IPAM catches up",
}


def test_demo_alert_tabs_all_show_something(clean_env, tmp_path):
    path = str(tmp_path / "demo.db")
    _seed(path)[0].close()
    clean_env.setenv("PATCHBAY_DB", path)
    import patchbay.web as web
    client = TestClient(web.app)
    for tab, marker in ALERT_TABS.items():
        r = client.get(f"/alerts?tab={tab}")
        assert r.status_code == 200, tab
        assert marker in r.text, (tab, marker)
    # every kind of history line the engine and the dispatcher write
    history = client.get("/alerts?tab=history").text
    for event in ("raised", "active", "notified", "escalated", "silenced", "cleared"):
        assert event in history, event


def test_demo_canary_is_active_on_a_flooded_port(tmp_path):
    from types import SimpleNamespace

    from patchbay.attention import attention_items

    c, _ = _seed(str(tmp_path / "demo.db"))
    items, _ = attention_items(c, SimpleNamespace(ipam_url=None, expected=set()))
    [canary] = [i for i in items if i["rule"] == "port-canary"]
    assert canary["key"] == "port:canary:edge1:e1/0/4:in_errors"
    assert canary["severity"] == "warn" and not canary["silenced"]
    # held for two samples, and the engine says so
    state = c.execute("SELECT state, polls FROM alerts WHERE key = ?",
                      (canary["key"],)).fetchone()
    assert tuple(state) == ("active", 2)
    # a week of its own baseline stands behind the verdict
    assert "baseline" in canary["text"] and "no baseline yet" not in canary["text"]
    # the IPAM drift item is the one the silence covers
    [ipam] = [i for i in items if i["rule"] == "ipam-drift"]
    assert ipam["silenced"]["reason"]
    c.close()


def test_demo_history_has_a_raise_a_notification_and_a_clear(tmp_path):
    c, _ = _seed(str(tmp_path / "demo.db"))
    events = [r[0] for r in c.execute(
        "SELECT event FROM alert_events WHERE key = 'device:ap-garage' ORDER BY id")]
    assert events == ["raised", "active", "notified", "cleared", "notified"]
    c.close()


def test_demo_declares_an_expected_tunnel_the_firewall_reports(tmp_path):
    from patchbay import expected_tunnels

    c, _ = _seed(str(tmp_path / "demo.db"))
    [entry] = expected_tunnels.load(c)
    assert (entry["device"], entry["type"]) == ("fw1", "wireguard")
    assert c.execute("SELECT status FROM tunnels WHERE name = ?",
                     (entry["name"],)).fetchone()[0] == "up"
    c.close()


def test_demo_transport_can_never_be_reached(tmp_path):
    """The demo's webhook is a story, not a receiver: its host never
    resolves (.invalid, RFC 2606), it is disabled, and a dispatch with
    notifications due sends nothing."""
    import httpx

    from patchbay import alerting, transports

    path = str(tmp_path / "demo.db")
    c, _ = _seed(path)
    [t] = c.execute("SELECT * FROM alert_transports").fetchall()
    assert httpx.URL(t["url"]).host.endswith(".invalid")
    assert t["enabled"] == 0
    c.commit()
    c.close()

    seen = []
    note = alerting.Notification(kind="raise", key="k", rule="device-down",
                                 severity="crit", text="t", href="/", raised_at=1.0,
                                 route=None)
    transports.WebhookDispatcher(path, transport=httpx.MockTransport(
        lambda req: seen.append(req) or httpx.Response(200))).send([note])
    assert seen == []


def test_demo_phase_6_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr(pdb, "now", lambda: 1_800_000_000.0)
    dumps = []
    for name in ("a.db", "b.db"):
        c, _ = _seed(str(tmp_path / name))
        dumps.append([
            c.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
            for t in ("alerts", "alert_events", "alert_silences", "alert_transports")
        ] + [c.execute("SELECT * FROM rate_history WHERE in_errors IS NOT NULL "
                       "ORDER BY ts, device, interface").fetchall()])
        c.close()
    assert dumps[0] == dumps[1]
    assert len(dumps[0][-1]) > 1700          # a week of the canary port
