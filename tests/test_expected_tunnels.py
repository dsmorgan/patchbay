"""expected-tunnel-missing (#63)."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from patchbay import alerting, attention, demo, expected_tunnels
from patchbay import db as pdb
from patchbay.config import load_settings

WG = ("fw1", "wireguard", "site-b · wg-peer")


def _demo(tmp_path):
    c = sqlite3.connect(str(tmp_path / "demo.db"))
    c.row_factory = sqlite3.Row
    demo.seed(c)
    return c


def _set(c, status):
    c.execute("UPDATE tunnels SET status = ? WHERE type = 'wireguard'", (status,))


def test_stop_raises_warn_within_a_poll_and_start_clears(clean_env, tmp_path):
    c = _demo(tmp_path)
    s = load_settings()
    expected_tunnels.add(c, *WG)
    alerting.run(c, s)                    # baseline: demo's own items raise here
    _set(c, "down")
    raised = [n for n in alerting.run(c, s) if n.kind == "raise"]
    assert [(n.key, n.severity, n.rule) for n in raised] == [
        ("tunnel:fw1:wireguard:site-b · wg-peer", "warn", "expected-tunnel-missing")]
    assert "wireguard" in raised[0].text and "fw1" in raised[0].text
    assert "site-b · wg-peer" in raised[0].text and raised[0].href == "/routed"
    _set(c, "up")
    assert [(n.kind, n.key) for n in alerting.run(c, s)] == [
        ("clear", "tunnel:fw1:wireguard:site-b · wg-peer")]
    c.close()


def test_absent_and_stale_count_as_missing(clean_env, tmp_path):
    c = _demo(tmp_path)
    s = load_settings()
    expected_tunnels.add(c, *WG)
    expected_tunnels.add(c, "fw1", "openvpn", "road-warrior")
    items, _ = attention.attention_items(c, s)
    assert [i["key"] for i in items if i["category"] == "tunnel"] == [
        "tunnel:fw1:openvpn:road-warrior"]
    c.execute("UPDATE tunnels SET last_seen = last_seen - 3 * 3600")
    items, _ = attention.attention_items(c, s)
    assert len([i for i in items if i["category"] == "tunnel"]) == 2
    c.close()


def test_undeclared_tunnel_never_alerts(clean_env, tmp_path):
    c = _demo(tmp_path)
    _set(c, "down")
    items, _ = attention.attention_items(c, load_settings())
    assert not [i for i in items if i["category"] == "tunnel"]
    c.close()


def test_label_and_catalog():
    assert attention.CATEGORIES["tunnel"] == "tunnels"
    assert alerting.RULES["expected-tunnel-missing"].category == "tunnel"


@pytest.fixture()
def client(clean_env, tmp_path):
    from patchbay import web
    return TestClient(web.app, follow_redirects=False)


def test_tab_add_remove_and_validation(client, clean_env):
    r = client.post("/alerts/tunnels", content="device=fw1&type=wireguard&name=site-b")
    assert r.status_code == 303
    page = client.get("/alerts?tab=tunnels").text
    assert "site-b" in page and "Expected tunnels" in page
    for bad in ("device=fw1&type=gre&name=x", "device=&type=vpn&name=x",
                "device=fw1&type=vpn&name="):
        assert client.post("/alerts/tunnels", content=bad).status_code == 400
    r = client.post("/alerts/tunnels/remove", content="device=fw1&type=wireguard&name=site-b")
    assert r.status_code == 303
    assert "No tunnels declared" in client.get("/alerts?tab=tunnels").text
