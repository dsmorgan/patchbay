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
    """The demo exercises WAN discovery and the participation filter: the
    uplink VLAN (no subnet record) resolves as the wan rail from the
    default route's exit interface, and the IPAM aggregate never rails."""
    from patchbay.config import load_settings
    from patchbay.routed import build_routed_graph

    c, _ = _seed(str(tmp_path / "demo.db"))
    g = build_routed_graph(c, load_settings())
    by = {r["key"]: r for r in g["rails"]}
    assert g["default"]["rail"] == "v199"
    assert by["v199"]["wan"] is True
    assert not any(k.startswith("net:") for k in by)   # aggregate filtered
    c.close()
