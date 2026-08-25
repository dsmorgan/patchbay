"""Page smoke + graceful degradation: every page renders on an empty DB (no
collector has ever run) and on a small synthetic model; missing integrations
shrink features, never 500."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from patchbay import db as pdb

PAGES = ["/", "/topology", "/vlans", "/drift", "/patchpanel", "/ops"]


@pytest.fixture()
def client(clean_env):
    import patchbay.web as web
    return TestClient(web.app)


def seed(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    pdb.init(c)
    sid = pdb.upsert_device(c, name="sw1", source="librenms", role="switch",
                            vendor="ExampleSwitch 24", mgmt_ip="192.0.2.2",
                            status="up")
    pdb.upsert_device(c, name="fw1", source="opnsense", role="firewall",
                      os="opnsense 26.1", mgmt_ip="192.0.2.1", status="up")
    hid = pdb.upsert_device(c, name="hyp1", source="vsphere", role="hypervisor",
                            status="up")
    pdb.upsert_device(c, name="vm-a", source="vsphere", role="vm",
                      parent="hyp1", status="up")
    pdb.upsert_interface(c, device_id=sid, name="1/0/1", oper_status="up",
                         speed_bps=10_000_000_000, description="uplink [3]")
    pdb.upsert_interface(c, device_id=hid, name="vmnic0", oper_status="up",
                         speed_bps=10_000_000_000)
    pdb.upsert_link(c, a_device="sw1", a_interface="1/0/1", b_device="hyp1",
                    b_interface="vmnic0", source="lldp")
    pdb.upsert_subnet(c, cidr="192.0.2.0/24", source="phpipam", vlan=24)
    c.execute("INSERT INTO vlans (vid, name, source) VALUES (24, 'lab', 'phpipam')")
    c.commit(); c.close()


def test_all_pages_render_on_empty_db(client):
    for p in PAGES:
        assert client.get(p).status_code == 200, p
    assert client.get("/device/nothing").status_code == 404


def test_all_pages_render_on_seeded_db(clean_env, tmp_path, client):
    seed(str(tmp_path / "test.db"))
    for p in PAGES + ["/device/sw1", "/device/hyp1", "/device/fw1"]:
        r = client.get(p)
        assert r.status_code == 200, (p, r.status_code)
    assert "sw1" in client.get("/").text
    assert "vm-a" in client.get("/device/hyp1").text  # guests listed


def test_configs_page_degrades_without_oxidized(client):
    # OXIDIZED_URL unset: the page must render a "not configured" state
    r = client.get("/configs")
    assert r.status_code == 200


def test_graph_validates_gtype(client):
    assert client.get("/graph?device=sw1&gtype=../../etc").status_code in (400, 404)


def test_device_alias_redirects(clean_env, tmp_path, client):
    seed(str(tmp_path / "test.db"))
    c = sqlite3.connect(str(tmp_path / "test.db"))
    c.execute("INSERT INTO aliases VALUES ('sw1.example.lan', 'sw1')")
    c.commit(); c.close()
    r = client.get("/device/sw1.example.lan", follow_redirects=False)
    assert r.status_code in (302, 307) and "/device/sw1" in r.headers["location"]


def test_positions_api_validation(client):
    assert client.post("/api/positions", json={"name": "x", "x": "abc", "y": 1}
                       ).status_code == 400
    assert client.post("/api/positions", content=b"junk",
                       headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/api/positions", json={"name": "x", "x": 1, "y": 2}
                       ).status_code == 200


def test_ops_renders_last_poll(clean_env, tmp_path, client):
    # the last-poll panel only renders when a poll has been recorded, so the
    # bare smoke test never exercised it (a Jinja error hid here once)
    import sqlite3

    c = sqlite3.connect(str(tmp_path / "test.db"))
    c.row_factory = sqlite3.Row
    pdb.init(c)
    pdb.save_last_poll(c, ["[ok]   librenms: 5 devices", "[fail] unifi: timeout"])
    c.commit(); c.close()
    t = client.get("/ops").text
    assert "Last poll:" in t
    assert "some sources failed" in t
    assert "[fail] unifi: timeout" in t


def test_ops_snapshot_download_404s_before_first_snapshot(client):
    assert client.get("/ops/snapshot/latest").status_code == 404


def test_ops_declaration_editing(clean_env, client):
    r = client.post("/ops/config", json={"var": "PATCHBAY_CAPACITY",
                                         "value": "sw1:1/0/16=3G"})
    assert r.status_code == 200 and r.json()["warnings"] == []
    assert "sw1:1/0/16=3G" in client.get("/ops").text
    # env-owned variables are refused
    clean_env.setenv("PATCHBAY_LINKS", "a:1=b:2")
    r = client.post("/ops/config", json={"var": "PATCHBAY_LINKS", "value": "x"})
    assert r.status_code == 409
    # non-declarations are refused
    r = client.post("/ops/config", json={"var": "LIBRENMS_TOKEN", "value": "x"})
    assert r.status_code == 400


def _graph(client, tmp_path):
    import json
    import re
    r = client.get("/topology")
    assert r.status_code == 200
    m = re.search(r"const graph = (\{.*?\});", r.text, re.S)
    assert m, "topology graph JSON not found in the page"
    return json.loads(m.group(1))


def test_redundant_wan_ports_share_one_cloud(clean_env, tmp_path, client):
    # two circuits from one provider: one cloud node, one edge per port
    seed(str(tmp_path / "test.db"))
    c = sqlite3.connect(str(tmp_path / "test.db"))
    for name in ("1/0/16", "1/0/17"):
        c.execute("INSERT INTO interfaces (device_id, name, oper_status) "
                  "SELECT id, ?, 'up' FROM devices WHERE name='sw1'", (name,))
    c.execute("INSERT INTO gateways (name, address, status, source) "
              "VALUES ('WAN_GW', '192.0.2.254', 'Online', 'opnsense')")
    c.commit(); c.close()
    clean_env.setenv("PATCHBAY_WAN_NAME", "Fiber")
    clean_env.setenv("PATCHBAY_WAN_PORT", "sw1:1/0/16,sw1:1/0/17")
    g = _graph(client, tmp_path)
    assert [n["name"] for n in g["nodes"]].count("Fiber") == 1
    wan = [e for e in g["links"] if e["target"] == "Fiber"]
    assert sorted(e["alab"] for e in wan) == ["1/0/16", "1/0/17"]


def test_two_providers_get_their_own_clouds(clean_env, tmp_path, client):
    seed(str(tmp_path / "test.db"))
    c = sqlite3.connect(str(tmp_path / "test.db"))
    for name in ("1/0/16", "1/0/17"):
        c.execute("INSERT INTO interfaces (device_id, name, oper_status) "
                  "SELECT id, ?, 'up' FROM devices WHERE name='sw1'", (name,))
    c.execute("INSERT INTO gateways (name, address, status, source) "
              "VALUES ('WAN_GW', '192.0.2.254', 'Online', 'opnsense')")
    c.commit(); c.close()
    clean_env.setenv("PATCHBAY_WAN_NAME", "Fiber,Cable")
    clean_env.setenv("PATCHBAY_WAN_PORT", "sw1:1/0/16,sw1:1/0/17")
    g = _graph(client, tmp_path)
    names = [n["name"] for n in g["nodes"]]
    assert "Fiber" in names and "Cable" in names
    # only one gateway is reported, so the second provider says so plainly
    cable = next(n for n in g["nodes"] if n["name"] == "Cable")
    assert cable["sub"] == "no gateway reported" and cable["status"] == "unknown"


def test_no_wan_evidence_draws_no_cloud(clean_env, tmp_path, client):
    # no declared landing port and no WAN gateway: patchbay has no evidence
    # the internet is reachable, so it doesn't draw it
    seed(str(tmp_path / "test.db"))
    g = _graph(client, tmp_path)
    assert not [n for n in g["nodes"] if n["role"] == "cloud"]


def test_mirror_port_is_labelled_not_treated_as_a_path(clean_env, tmp_path, client):
    seed(str(tmp_path / "test.db"))
    c = sqlite3.connect(str(tmp_path / "test.db"))
    c.execute("INSERT INTO interfaces (device_id, name, oper_status) "
              "SELECT id, '1/0/14', 'up' FROM devices WHERE name='sw1'")
    c.execute("INSERT INTO port_roles (device, interface, role, detail, source) "
              "VALUES ('sw1', '1/0/14', 'monitor-dst', 'session 1 mirrors vlan 1', "
              "'oxidized')")
    pdb.upsert_link(c, a_device="sw1", a_interface="1/0/14", b_device="hyp1",
                    b_interface="vmnic5", source="declared")
    c.commit(); c.close()
    g = _graph(client, tmp_path)
    e = next(e for e in g["links"] if e["alab"] == "1/0/14" or e["blab"] == "1/0/14")
    assert "monitor" in e["cls"] and "port mirror" in e["note"]
    assert "port mirror" in client.get("/device/sw1").text


def test_dashboard_says_what_it_counted(clean_env, tmp_path, client):
    seed(str(tmp_path / "test.db"))
    body = client.get("/").text
    # the headline numbers carry their own definition, so nobody has to guess
    assert "physical ports on 2 devices" in body
    assert "distinct MAC addresses known to any source" in body


def test_first_run_tells_you_what_to_configure(client):
    # An empty DB with no sources is what every new user sees first. A page of
    # zeros with empty headings reads as broken software, and the one useful
    # message ("no collectors configured") only appears in the poller's log.
    body = client.get("/").text
    assert "Nothing to show yet" in body
    assert ".env.example" in body and "docs/configuration.md" in body


def test_onboarding_notice_goes_away_once_there_are_devices(clean_env, tmp_path, client):
    seed(str(tmp_path / "test.db"))
    assert "Nothing to show yet" not in client.get("/").text


def test_configured_but_empty_points_at_ops(clean_env, tmp_path, client):
    # sources configured, nothing collected yet: a different problem, so a
    # different answer — go look at the per-source poll results
    clean_env.setenv("LIBRENMS_URL", "http://librenms.invalid:8000")
    clean_env.setenv("LIBRENMS_TOKEN", "x")
    body = client.get("/").text
    assert "librenms" in body and "/ops" in body
    assert ".env.example" not in body


def test_build_stamp_always_names_the_release(clean_env):
    """The header stamp leads with the version and appends the build. A bare
    commit was all a container ever showed, so the release number the tag
    promises was invisible exactly where it mattered most."""
    import importlib

    from patchbay import __version__
    import patchbay.web as web

    # PATCHBAY_BUILD=dev is the ARG default, not an answer; rendering it put
    # the word "dev" beside the product name on every unstamped build
    clean_env.setenv("PATCHBAY_BUILD", "dev")
    assert importlib.reload(web)._build_version() != "dev"

    # what the Dockerfile bakes in: a bare short SHA
    clean_env.setenv("PATCHBAY_BUILD", "abc1234")
    assert importlib.reload(web)._build_version() == f"{__version__}+abc1234"

    # a stamp that already names the version isn't doubled
    clean_env.setenv("PATCHBAY_BUILD", f"{__version__}+abc1234")
    assert importlib.reload(web)._build_version() == f"{__version__}+abc1234"

    # a release image is stamped with the bare version and shows exactly
    # that — a tagged release needs no sha beside it
    clean_env.setenv("PATCHBAY_BUILD", __version__)
    assert importlib.reload(web)._build_version() == __version__

    # every form starts with the release number
    for stamp in ("dev", "abc1234", __version__, f"{__version__}+abc1234"):
        clean_env.setenv("PATCHBAY_BUILD", stamp)
        assert importlib.reload(web)._build_version().startswith(__version__)


def test_empty_sections_are_hidden(clean_env, tmp_path, client):
    # An empty "Access points" heading is noise for every site without APs,
    # not only on a first run
    body = client.get("/").text
    for heading in ("Fabric", "Access points", "Links"):
        assert f"<h2>{heading}</h2>" not in body
    seed(str(tmp_path / "test.db"))
    body = client.get("/").text
    assert "<h2>Fabric</h2>" in body and "<h2>Links</h2>" in body
    assert "<h2>Access points</h2>" not in body   # the seed has no APs


def test_every_connection_enforces_foreign_keys(clean_env, tmp_path):
    """The pragma is per-connection, so each place that opens one has to set
    it. /ops polls and normalizes on the web app's connection, so a missing
    pragma there silently stopped device retirement from cascading."""
    import sqlite3

    from patchbay import db as pdb
    import patchbay.web as web

    dbp = str(tmp_path / "fk.db")
    clean_env.setenv("PATCHBAY_DB", dbp)
    with pdb.connect(dbp) as c:
        pdb.init(c)

    c = web._conn()
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "web._conn"
    c.close()
    with pdb.connect(dbp) as c:
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "db.connect"

    # and the constraint it guards actually cascades through that connection
    c = web._conn()
    pdb.init(c)
    did = pdb.upsert_device(c, name="gone", source="vsphere", role="vm")
    pdb.upsert_interface(c, device_id=did, name="vmx0")
    c.execute("DELETE FROM devices WHERE name = 'gone'")
    assert c.execute("SELECT COUNT(*) FROM interfaces").fetchone()[0] == 0
    c.close()
