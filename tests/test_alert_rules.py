"""The v1 rule catalog (issue #60, ADR-0003 Decision 4): device down, link
down, gateway degraded, config changed, and snapshot failed, each as the
attention item it produces and the alert the engine makes of it, plus the
Rules tab that tunes them. Every model here is synthetic."""

from __future__ import annotations

import argparse
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from patchbay import alerting, attention
from patchbay import db as pdb
from patchbay.config import load_settings


@pytest.fixture()
def settings(clean_env):
    return load_settings()


def _items(conn, settings, rule):
    return [i for i in attention.attention_items(conn, settings)[0] if i["rule"] == rule]


def _dev(conn, name, role, status="up", **kw):
    return pdb.upsert_device(conn, name=name, source="librenms", role=role,
                             status=status, **kw)


def _cable(conn, a, ai, b, bi, *, source="lldp", a_oper="up", b_oper="up"):
    for dev, iface, oper in ((a, ai, a_oper), (b, bi, b_oper)):
        did = conn.execute("SELECT id FROM devices WHERE name = ?", (dev,)).fetchone()[0]
        pdb.upsert_interface(conn, device_id=did, name=iface, oper_status=oper)
    pdb.upsert_link(conn, a_device=a, a_interface=ai, b_device=b, b_interface=bi,
                    source=source)


# --- device down -------------------------------------------------------------

def test_device_down_watches_roles_including_hypervisor(conn, settings):
    _dev(conn, "sw1", "switch", "down")
    _dev(conn, "hyp1", "hypervisor", "notResponding")
    _dev(conn, "ap1", "ap")
    _dev(conn, "vm1", "vm", "poweredOff")        # guests are not watched
    _dev(conn, "nas1", "host", "down")           # nor are hosts
    items = {i["key"]: i for i in _items(conn, settings, "device-down")}
    assert set(items) == {"device:sw1", "device:hyp1"}
    assert items["device:sw1"]["severity"] == "crit"
    assert items["device:sw1"]["category"] == "device"
    assert items["device:hyp1"]["text"] == "hypervisor hyp1 is notresponding"
    assert items["device:sw1"]["href"] == "/device/sw1"


def test_device_down_counts_stale_as_down(conn, settings):
    _dev(conn, "ap1", "ap")
    conn.execute("UPDATE devices SET last_seen = ?", (pdb.now() - 3 * 3600,))
    [it] = _items(conn, settings, "device-down")
    assert it["text"] == "ap ap1 has not been reported for 3 h"


def test_device_down_roles_parameter_and_expected(conn, clean_env):
    _dev(conn, "hyp1", "hypervisor", "down")
    _dev(conn, "sw1", "switch", "down")
    alerting.seed_rules(conn)
    conn.execute("UPDATE alert_rules SET params = ? WHERE name = 'device-down'",
                 (json.dumps({"for": 1, "roles": ["switch"]}),))
    assert [i["key"] for i in _items(conn, load_settings(), "device-down")] == ["device:sw1"]

    clean_env.setenv("PATCHBAY_EXPECT", "sw1")
    assert _items(conn, load_settings(), "device-down") == []


def test_overview_hides_device_items_that_alerts_shows(clean_env, tmp_path):
    from tests.test_web import seed

    import patchbay.web as web

    seed(str(tmp_path / "test.db"))
    c = sqlite3.connect(str(tmp_path / "test.db"))
    c.execute("UPDATE devices SET status = 'down' WHERE name = 'sw1'")
    c.commit(); c.close()
    client = TestClient(web.app)
    assert "switch sw1 is down" not in client.get("/").text
    page = client.get("/alerts").text
    assert "switch sw1 is down" in page
    assert ">devices</a>" in page                    # a filter chip of its own


def test_disabled_device_is_a_decision_not_an_alert(conn, settings):
    # LibreNMS admin-disabled: no device-down item, but its cables are still
    # inhibited, because disabled stays a down state for the engine
    _dev(conn, "sw1", "switch")
    _dev(conn, "sw2", "switch", "disabled")
    _cable(conn, "sw1", "1/0/9", "sw2", "1/0/1", a_oper="down")
    assert _items(conn, settings, "device-down") == []
    items = attention.attention_items(conn, settings)[0]
    assert [i["rule"] for i in items if i["rule"] == "link-down"] == ["link-down"]
    assert alerting.evaluate(conn, items) == []
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


# --- link down ---------------------------------------------------------------

def test_link_down_one_item_per_stated_cable(conn, settings):
    _dev(conn, "sw1", "switch")
    _dev(conn, "ap1", "ap")
    _dev(conn, "hyp1", "hypervisor")
    _cable(conn, "sw1", "1/0/2", "ap1", "eth0", a_oper="down", b_oper="down")
    # the same cable from a declaration: still one item
    pdb.upsert_link(conn, a_device="ap1", a_interface="eth0", b_device="sw1",
                    b_interface="1/0/2", source="declared")
    # an inferred cable going quiet says nothing reliable about a cable
    _cable(conn, "sw1", "1/0/3", "hyp1", "vmnic1", source="fdb-uplink", a_oper="down")
    [it] = _items(conn, settings, "link-down")
    assert it["key"] == "link-down:ap1:eth0:sw1:1/0/2"
    assert (it["category"], it["severity"]) == ("link", "warn")
    assert it["ports"] == [("ap1", "eth0"), ("sw1", "1/0/2")]
    assert "ap1 eth0, sw1 1/0/2 reports down" in it["text"]


def test_link_down_skips_admin_down_and_expected(conn, clean_env):
    _dev(conn, "sw1", "switch")
    _dev(conn, "ap1", "ap")
    _cable(conn, "sw1", "1/0/2", "ap1", "eth0", a_oper="down")
    conn.execute("UPDATE interfaces SET admin_status = 'down' WHERE name = '1/0/2'")
    assert _items(conn, load_settings(), "link-down") == []

    conn.execute("UPDATE interfaces SET admin_status = 'up' WHERE name = '1/0/2'")
    assert len(_items(conn, load_settings(), "link-down")) == 1
    clean_env.setenv("PATCHBAY_EXPECT", "sw1:1/0/2")
    assert _items(conn, load_settings(), "link-down") == []


def test_down_switch_is_one_alert_not_one_per_cable(conn, settings):
    _dev(conn, "core1", "switch")
    _dev(conn, "edge1", "switch")
    _dev(conn, "ap1", "ap")
    _cable(conn, "core1", "1/0/3", "edge1", "e1/0/1")
    _cable(conn, "edge1", "e1/0/2", "ap1", "eth0")
    now = pdb.now()
    alerting.evaluate(conn, attention.attention_items(conn, settings)[0], now=now)

    # edge1 dies: its uplink on core1 and the AP's port both go down
    conn.execute("UPDATE devices SET status = 'down' WHERE name = 'edge1'")
    conn.execute("UPDATE interfaces SET oper_status = 'down' "
                 "WHERE name IN ('1/0/3', 'eth0')")
    items = attention.attention_items(conn, settings)[0]
    assert {i["rule"] for i in items} >= {"device-down", "link-down"}
    notes = alerting.evaluate(conn, items, now=now + 300)
    assert [(n.kind, n.key, n.severity) for n in notes] == [
        ("raise", "device:edge1", "crit")]


# --- the done-when, on the demo model ------------------------------------------

def test_demo_ap_down_then_its_switch_down(clean_env, tmp_path):
    from patchbay import demo

    c = sqlite3.connect(str(tmp_path / "demo.db"))
    c.row_factory = sqlite3.Row
    demo.seed(c)
    s = load_settings()
    alerting.run(c, s)                    # the healthy baseline poll

    c.execute("UPDATE devices SET status = 'down' WHERE name = 'ap-attic'")
    c.execute("UPDATE interfaces SET oper_status = 'down' WHERE name = 'e1/0/2'")
    raised = [(n.key, n.severity) for n in alerting.run(c, s) if n.kind == "raise"]
    assert raised == [("device:ap-attic", "crit")]   # within one poll

    c.execute("UPDATE devices SET status = 'up' WHERE name = 'ap-attic'")
    c.execute("UPDATE interfaces SET oper_status = 'up' WHERE name = 'e1/0/2'")
    alerting.run(c, s)
    # the switch goes: every cable on it reads down from the far end too
    c.execute("UPDATE devices SET status = 'down' WHERE name = 'edge1'")
    c.execute("UPDATE interfaces SET oper_status = 'down' WHERE name = '1/0/3' "
              "OR (name = 'eth0' AND device_id IN "
              "(SELECT id FROM devices WHERE role = 'ap'))")
    raised = [(n.key, n.severity) for n in alerting.run(c, s) if n.kind == "raise"]
    assert raised == [("device:edge1", "crit")]
    c.close()


# --- gateway degraded --------------------------------------------------------

def _gw(conn, name, status, loss, age=0):
    conn.execute("INSERT OR REPLACE INTO gateways (name, address, status, loss, "
                 "delay, source, last_seen) VALUES (?, '192.0.2.254', ?, ?, "
                 "'5 ms', 'opnsense', ?)", (name, status, loss, pdb.now() - age))


def test_gateway_down_is_crit_and_loss_is_warn(conn, settings):
    _gw(conn, "WAN_GW", "Offline", "100.0 %")
    _gw(conn, "WAN2_GW", "Packetloss", "12.0 %")
    _gw(conn, "WAN3_GW", "Online", "1.0 %")
    _gw(conn, "WAN6_GW", "pending", None)       # no data yet: no opinion
    _gw(conn, "OLD_GW", "down", "100 %", age=3 * 3600)   # stale-source's to say
    got = {i["key"]: i["severity"] for i in _items(conn, settings, "gateway-degraded")}
    assert got == {"gateway:WAN_GW": "crit", "gateway:WAN2_GW": "warn"}


def test_gateway_loss_threshold_and_severity_rise(conn, settings):
    _gw(conn, "WAN_GW", "Online", "8 %")
    alerting.seed_rules(conn)
    conn.execute("UPDATE alert_rules SET params = '{\"loss\": 10}' "
                 "WHERE name = 'gateway-degraded'")
    assert _items(conn, settings, "gateway-degraded") == []

    conn.execute("UPDATE alert_rules SET params = '{}' WHERE name = 'gateway-degraded'")
    now = pdb.now()
    alerting.evaluate(conn, _items(conn, settings, "gateway-degraded"), now=now)
    _gw(conn, "WAN_GW", "Offline", "100 %")
    notes = alerting.evaluate(conn, _items(conn, settings, "gateway-degraded"),
                              now=now + 300)
    # the rise is news: exactly one notification, at the new severity
    assert [(n.kind, n.severity) for n in notes] == [("escalate", "crit")]
    [a] = conn.execute("SELECT * FROM alerts").fetchall()   # one alert, risen
    assert (a["key"], a["severity"], a["state"]) == ("gateway:WAN_GW", "crit", "active")
    assert a["last_notified_at"] == now + 300

    # a drop back to lossy is not: it waits for the clear
    _gw(conn, "WAN_GW", "Online", "8 %")
    assert alerting.evaluate(conn, _items(conn, settings, "gateway-degraded"),
                             now=now + 600) == []
    assert conn.execute("SELECT severity FROM alerts").fetchone()[0] == "warn"


# --- config changed (event) ---------------------------------------------------

def _rev(conn, device, age, sha, msg=None):
    conn.execute("INSERT INTO config_revisions (device, fetched_at, sha, message, "
                 "author, text) VALUES (?, ?, ?, ?, 'admin', 'x')",
                 (device, pdb.now() - age, sha, msg))


def test_config_changed_fires_once_per_revision(conn, settings):
    _rev(conn, "fw1", 7200, "a" * 64)
    assert _items(conn, settings, "config-changed") == []    # a baseline, not a change
    _rev(conn, "fw1", 60, "b" * 64, "opened 443")
    [it] = _items(conn, settings, "config-changed")
    assert it["key"].startswith("config:fw1:") and it["key"].endswith(":" + "b" * 12)
    assert (it["category"], it["severity"]) == ("config", "info")
    assert it["text"] == "fw1 config changed: opened 443 — admin"
    assert it["href"] == "/configs/fw1"

    notes = alerting.evaluate(conn, _items(conn, settings, "config-changed"))
    assert [(n.kind, n.route) for n in notes] == [("event", "none")]   # informs only
    assert alerting.evaluate(conn, _items(conn, settings, "config-changed")) == []

    # a revert to the first state is a new occurrence, though its hash repeats
    _rev(conn, "fw1", 0, "a" * 64)
    assert [n.kind for n in alerting.evaluate(
        conn, _items(conn, settings, "config-changed"))] == ["event"]


def test_config_changed_leaves_the_list_after_a_day(conn, settings):
    _rev(conn, "fw1", 3 * 86400, "a" * 64)
    _rev(conn, "fw1", 2 * 86400, "b" * 64)
    assert _items(conn, settings, "config-changed") == []


def test_event_items_show_their_own_age(conn, settings):
    _rev(conn, "fw1", 7200, "a" * 64)
    _rev(conn, "fw1", 3600, "b" * 64)
    items = _items(conn, settings, "config-changed")
    alerting.evaluate(conn, items)
    attention.stamp_first_seen(conn, items)
    assert items[0]["first_seen"] == pytest.approx(pdb.now() - 3600, abs=5)


# --- snapshot failed (event) --------------------------------------------------

def test_snapshot_failure_record_fires_one_event_each(conn, settings):
    pdb.record_snapshot_failure(conn, "disk full")
    pdb.record_snapshot_failure(conn, "share offline", kind="undelivered")
    items = _items(conn, settings, "snapshot-failed")
    assert [i["text"] for i in items] == [
        "daily snapshot not delivered: share offline", "daily snapshot failed: disk full"]
    assert {(i["category"], i["severity"]) for i in items} == {("source", "warn")}
    assert len({i["key"] for i in items}) == 2
    notes = alerting.evaluate(conn, items)
    assert [n.kind for n in notes] == ["event", "event"]
    assert all(n.route is None for n in notes)        # the default route pages
    assert alerting.evaluate(conn, _items(conn, settings, "snapshot-failed")) == []


def test_snapshot_failure_record_is_capped(conn):
    for i in range(pdb.SNAPSHOT_FAILURES_KEEP + 5):
        pdb.record_snapshot_failure(conn, f"e{i}")
    kept = pdb.snapshot_failures(conn)
    assert len(kept) == pdb.SNAPSHOT_FAILURES_KEEP
    assert kept[-1]["error"] == f"e{pdb.SNAPSHOT_FAILURES_KEEP + 4}"
    pdb.set_state(conn, pdb.SNAPSHOT_FAILURES, "not json")
    assert pdb.snapshot_failures(conn) == []


def test_poll_records_a_failed_daily_snapshot(clean_env, tmp_path, monkeypatch):
    from patchbay import cli, snapshot

    class Quiet:
        def collect(self, settings, conn):
            return "nothing"

    def boom(settings, *a, **kw):
        raise RuntimeError("renderer crashed")

    monkeypatch.setattr(cli, "available", lambda s: {"quiet": Quiet()})
    monkeypatch.setattr(snapshot, "due_today", lambda conn, s: True)
    monkeypatch.setattr(snapshot, "write_snapshot", boom)
    assert cli.cmd_poll(argparse.Namespace(source=None)) == 1

    with pdb.connect(str(tmp_path / "test.db")) as c:
        [f] = pdb.snapshot_failures(c)
        assert (f["kind"], f["trigger"], f["error"]) == ("failed", "daily",
                                                        "renderer crashed")


def test_ops_snapshot_records_failure(clean_env, tmp_path, monkeypatch):
    import patchbay.web as web
    from patchbay import snapshot

    def boom(settings, *a, **kw):
        raise RuntimeError("renderer crashed")

    monkeypatch.setattr(snapshot, "write_snapshot", boom)
    client = TestClient(web.app)
    assert client.post("/ops/snapshot").status_code == 500
    with pdb.connect(str(tmp_path / "test.db")) as c:
        [f] = pdb.snapshot_failures(c)
        assert (f["kind"], f["trigger"]) == ("failed", "manual")


# --- the catalog and the Rules tab ---------------------------------------------

def test_catalog_defaults_per_adr(conn):
    alerting.seed_rules(conn)
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM alert_rules")}
    assert json.loads(rows["device-down"]["params"]) == {
        "for": 1, "roles": ["switch", "ap", "firewall", "router", "hypervisor"]}
    assert json.loads(rows["gateway-degraded"]["params"]) == {"for": 1, "loss": 5.0}
    assert json.loads(rows["link-down"]["params"]) == {"for": 1}
    # config changed and slow link inform without paging
    assert {n for n, r in rows.items() if r["route"] == "none"} == {
        "config-changed", "slow-link"}
    assert alerting.RULES["config-changed"].event
    assert alerting.RULES["snapshot-failed"].event


def test_rules_tab_lists_and_saves(clean_env, tmp_path):
    import patchbay.web as web

    client = TestClient(web.app)
    page = client.get("/alerts?tab=rules").text
    for name in alerting.RULES:
        assert f'action="/alerts/rules/{name}"' in page
    assert 'value="switch, ap, firewall, router, hypervisor"' in page
    assert "nowhere (attention list only)" in page    # read-only route

    with pdb.connect(str(tmp_path / "test.db")) as c:
        c.execute("UPDATE alert_rules SET params = '{\"for\": 1, \"remind\": 3600}' "
                  "WHERE name = 'device-down'")
    r = client.post("/alerts/rules/device-down", data={
        "enabled": "1", "severity": "warn", "param_for": "2",
        "param_roles": "Switch, ap"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/alerts?tab=rules")
    with pdb.connect(str(tmp_path / "test.db")) as c:
        row = c.execute("SELECT * FROM alert_rules WHERE name = 'device-down'").fetchone()
        assert (row["enabled"], row["severity"]) == (1, "warn")
        assert json.loads(row["params"]) == {"for": 2, "remind": 3600,
                                             "roles": ["switch", "ap"]}
        assert row["route"] is None                    # routes are not edited here

    # an unchecked box disables; an empty severity returns to the rule's own
    client.post("/alerts/rules/device-down", data={
        "severity": "", "param_for": "2", "param_roles": "switch"})
    with pdb.connect(str(tmp_path / "test.db")) as c:
        row = c.execute("SELECT * FROM alert_rules WHERE name = 'device-down'").fetchone()
        assert (row["enabled"], row["severity"]) == (0, None)


@pytest.mark.parametrize("form", [
    {"param_for": "0"}, {"param_for": "soon"}, {"param_roles": " , "},
    {"severity": "panic"},
])
def test_rules_tab_rejects_bad_values_without_writing(clean_env, tmp_path, form):
    import patchbay.web as web

    client = TestClient(web.app)
    client.get("/alerts?tab=rules")                  # seeds the rows
    r = client.post("/alerts/rules/device-down", data={"enabled": "1", **form})
    assert r.status_code == 400
    with pdb.connect(str(tmp_path / "test.db")) as c:
        row = c.execute("SELECT * FROM alert_rules WHERE name = 'device-down'").fetchone()
        assert row["severity"] is None and json.loads(row["params"])["for"] == 1


def test_rules_tab_gateway_loss_and_unknown_rule(clean_env, tmp_path):
    import patchbay.web as web

    client = TestClient(web.app)
    assert client.post("/alerts/rules/gateway-degraded", data={
        "enabled": "1", "param_loss": "12.5 %"}).status_code == 200
    assert client.post("/alerts/rules/gateway-degraded", data={
        "enabled": "1", "param_loss": "150"}).status_code == 400
    with pdb.connect(str(tmp_path / "test.db")) as c:
        assert json.loads(c.execute("SELECT params FROM alert_rules WHERE "
                                    "name = 'gateway-degraded'").fetchone()[0])["loss"] == 12.5
    assert client.post("/alerts/rules/no-such-rule", data={}).status_code == 404

