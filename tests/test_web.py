"""Page smoke + graceful degradation: every page renders on an empty DB (no
collector has ever run) and on a small synthetic model; missing integrations
shrink features, never 500."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from patchbay import db as pdb

PAGES = ["/", "/alerts", "/ports", "/clients", "/guests", "/topology",
         "/vlans", "/drift", "/patchpanel", "/ops", "/snapshots"]


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


def test_pages_carry_a_header(clean_env, tmp_path, client):
    # every shell page uses the page-header slot: a <header class="page">
    # with an <h1> matching the rail's name for that page (NAV, or the
    # device name for a drill-down) — and none of the old top-bar crumbs
    seed(str(tmp_path / "test.db"))
    expect_h1 = {
        "/": "Overview",
        "/alerts": "Alerts",
        "/ports": "All ports",
        "/clients": "All AP clients",
        "/guests": "All guests",
        "/topology": "Topology",
        "/vlans": "VLANs",
        "/drift": "Drift",
        "/patchpanel": "Patch panels",
        "/ops": "Ops",
        "/snapshots": "Snapshots",
        "/device/sw1": "sw1",
    }
    old_crumbs = ["/ vlans", "/ drift", "/ ops", "/ patch panels", "/ configs"]
    for p in PAGES + ["/device/sw1"]:
        body = client.get(p).text
        assert '<header class="page">' in body, p
        assert f"<h1>{expect_h1[p]}</h1>" in body, p
        for crumb in old_crumbs:
            assert crumb not in body, (p, crumb)


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


def test_actions_refresh_their_live_regions(clean_env, client):
    # issue #19: after an action completes, _actions.html re-fetches the page
    # and swaps every [data-live] region by id. The regions render even when
    # empty, so the first snapshot / first poll has an element to land in.
    ops = client.get("/ops").text
    assert "patchbayRefreshLive" in ops
    for rid in ("lastpoll", "dclconflicts", "dclexport", "effconfig"):
        assert f'id="{rid}" data-live' in ops, rid
    snaps = client.get("/snapshots").text
    assert "patchbayRefreshLive" in snaps
    for rid in ("snaplatest", "snapkept"):
        assert f'id="{rid}" data-live' in snaps, rid
    # act buttons stay outside every region — a swap would drop listeners
    assert 'data-live' not in snaps.split('<button class="act"')[1].split(">")[0]


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


def test_declaration_help_covers_every_var(clean_env, client):
    # issue #20: DECLARATION_HELP in config.py is the single source of truth
    # and must not drift from DECLARATION_VARS; /ops renders all of it
    from patchbay.config import DECLARATION_HELP, DECLARATION_VARS

    assert set(DECLARATION_HELP) == set(DECLARATION_VARS)
    for var, h in DECLARATION_HELP.items():
        assert h["what"] and h["syntax"] and h["example"], var
    body = client.get("/ops").text
    for var in DECLARATION_VARS:
        assert f'id="help-{var}"' in body, var
        assert f'class="dclq" data-var="{var}"' in body, var


def test_declaration_warning_sits_beside_its_field(clean_env, client):
    # issue #20: a malformed entry's warning renders in the field's own
    # warning block, not (only) in the page-top box
    clean_env.setenv("PATCHBAY_LINKS", "notapair")
    body = client.get("/ops").text
    field = body.split('class="dclwarn" data-var="PATCHBAY_LINKS"', 1)[1]
    head, warn_block = field.split(">", 1)
    assert "hidden" not in head
    assert "skipped malformed entry" in warn_block.split("</div>\n", 1)[0]
    # ...and the top box is gone once every warning has a field to live on
    assert "Skipped configuration entries" not in body


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
    # not only on a first run. The Links table is gone outright (the map
    # answers it now), so no heading text for it exists to check.
    body = client.get("/").text
    for heading in ("Fabric — switches, routers, and firewalls", "Access points"):
        assert f"<h2>{heading}" not in body
    assert "<h2>Links</h2>" not in body
    seed(str(tmp_path / "test.db"))
    body = client.get("/").text
    assert "<h2>Fabric — switches, routers, and firewalls" in body
    assert "<h2>Access points</h2>" not in body   # the seed has no APs
    assert "<h2>Links</h2>" not in body


def test_routers_belong_to_the_fabric(clean_env, tmp_path, client):
    # issue #27: a dedicated router gets a Fabric card, and a router VM
    # (promoted role + parent, like a virtualized firewall) appears both in
    # Fabric and folded under its hypervisor. On the map, routers sit in the
    # Edge tier beside firewalls, with their own legend entry.
    import patchbay.web as web

    dbp = str(tmp_path / "test.db")
    seed(dbp)
    c = sqlite3.connect(dbp)
    c.row_factory = sqlite3.Row
    pdb.upsert_device(c, name="rtr1", source="librenms", role="router",
                      mgmt_ip="192.0.2.3", status="up")
    pdb.upsert_device(c, name="vyos1", source="vsphere", role="router",
                      parent="hyp1", status="up")
    c.commit(); c.close()

    body = client.get("/").text
    assert '<div class="card router">' in body
    assert "rtr1" in body
    # the router VM is a Fabric card *and* a guest row under hyp1
    assert body.count("vyos1") >= 2

    topo = client.get("/topology").text
    assert "rtr1" in topo and "vyos1" in topo
    assert 'data-role="router"' in topo          # legend entry
    assert web.RANK["router"] == web.RANK["firewall"]   # Edge tier
    assert web.ICONS["router"]                   # has a glyph of its own


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


# --- configs-timeline ---

def _ox_mock(monkeypatch, handler):
    """Point web._ox_client at an httpx.MockTransport for the life of a test —
    no real Oxidized, no new dependency."""
    import httpx
    import patchbay.web as web

    monkeypatch.setattr(web, "_ox_client", lambda settings: httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://ox.invalid"))


def test_configs_timeline_lists_newest_first(clean_env, monkeypatch, client):
    import httpx

    clean_env.setenv("OXIDIZED_URL", "http://ox.invalid")
    nodes_json = [{"name": "core1"}, {"name": "edge1"}]
    versions = {
        # newest-first per node, like oxidized's own /node/version.json
        "core1": [
            {"oid": "c2", "date": "2026-08-20 10:00:00 +0000",
             "message": "core1 change 2", "author": "alice"},
            {"oid": "c1", "date": "2026-08-18 10:00:00 +0000",
             "message": "core1 change 1", "author": "alice"},
        ],
        "edge1": [
            {"oid": "e2", "date": "2026-08-21 10:00:00 +0000",
             "message": "edge1 change 2", "author": "bob"},
            {"oid": "e1", "date": "2026-08-15 10:00:00 +0000",
             "message": "edge1 change 1", "author": "bob"},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/nodes.json":
            return httpx.Response(200, json=nodes_json)
        if request.url.path == "/node/version.json":
            return httpx.Response(200, json=versions[request.url.params["node_full"]])
        raise AssertionError(f"unexpected request: {request.url}")

    _ox_mock(monkeypatch, handler)
    r = client.get("/configs")
    assert r.status_code == 200
    body = r.text

    # interleaved across nodes, newest first: edge1(21st), core1(20th),
    # core1(18th), edge1(15th) — each row's diff/view link carries oid+prev
    pos_e2 = body.index("/configs/edge1?v=e2&amp;prev=e1")
    pos_c2 = body.index("/configs/core1?v=c2&amp;prev=c1")
    pos_c1 = body.index("/configs/core1?v=c1")
    pos_e1 = body.index("/configs/edge1?v=e1")
    assert pos_e2 < pos_c2 < pos_c1 < pos_e1
    assert "&amp;prev=" not in body[pos_c1:pos_c1 + 40]   # oldest version: no prev
    assert "&amp;prev=" not in body[pos_e1:pos_e1 + 40]


def test_configs_timeline_survives_one_bad_node(clean_env, monkeypatch, client):
    import httpx

    clean_env.setenv("OXIDIZED_URL", "http://ox.invalid")
    nodes_json = [{"name": "core1"}, {"name": "edge1"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/nodes.json":
            return httpx.Response(200, json=nodes_json)
        if request.url.path == "/node/version.json":
            node = request.url.params["node_full"]
            if node == "edge1":
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json=[
                {"oid": "c1", "date": "2026-08-20 10:00:00 +0000",
                 "message": "core1 change", "author": "alice"},
            ])
        raise AssertionError(f"unexpected request: {request.url}")

    _ox_mock(monkeypatch, handler)
    r = client.get("/configs")
    assert r.status_code == 200
    body = r.text
    assert "core1 change" in body
    assert "/configs/core1?v=c1" in body
    assert "Could not read history for: edge1" in body


def test_configs_timeline_unreachable_is_a_state_not_a_crash(clean_env, monkeypatch, client):
    import httpx

    clean_env.setenv("OXIDIZED_URL", "http://ox.invalid")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _ox_mock(monkeypatch, handler)
    r = client.get("/configs")
    assert r.status_code == 200
    assert "unreachable" in r.text.lower()
# --- snapshots-page ---

def test_snapshots_page_lists_newest_first(clean_env, tmp_path, client):
    d = tmp_path / "snaps"
    d.mkdir()
    clean_env.setenv("PATCHBAY_SNAPSHOT_DIR", str(d))
    (d / "patchbay-20250101-000000.html").write_text("old")
    (d / "patchbay-20250201-000000.html").write_text("new")
    (d / "patchbay-latest.html").write_text("latest")
    body = client.get("/snapshots").text
    i_new = body.index("patchbay-20250201-000000.html")
    i_old = body.index("patchbay-20250101-000000.html")
    assert i_new < i_old  # newest first
    assert '/snapshots/patchbay-20250201-000000.html' in body
    assert '/snapshots/patchbay-latest.html' in body  # "download the latest" link


def test_snapshot_download_serves_only_the_pattern(clean_env, tmp_path, client):
    d = tmp_path / "snaps"
    d.mkdir()
    clean_env.setenv("PATCHBAY_SNAPSHOT_DIR", str(d))
    (d / "patchbay-20250101-000000.html").write_text("hello")
    (d / "other.html").write_text("nope")

    r = client.get("/snapshots/patchbay-20250101-000000.html")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.text == "hello"

    assert client.get("/snapshots/..%2F..%2Fpatchbay.db").status_code == 404
    assert client.get("/snapshots/other.html").status_code == 404
    assert client.get("/snapshots/patchbay-latest.html").status_code == 404  # not written yet


def test_snapshots_page_empty_state(client):
    r = client.get("/snapshots")
    assert r.status_code == 200
    assert "No snapshot yet" in r.text
    assert "PATCHBAY_SNAPSHOT_AT" in r.text


def test_ops_no_longer_offers_snapshot_buttons(client):
    body = client.get("/ops").text
    assert "snapshot now" not in body
    assert "download the latest snapshot" not in body
    assert 'data-url="/ops/snapshot"' not in body
    # Declarations now comes before the effective-configuration table
    i_decl = body.index("Declarations — what only you can tell patchbay")
    i_conf = body.index("What patchbay is running on")
    assert i_decl < i_conf
# --- map-url-modes ---


def test_topology_page_renders_mode_control(clean_env, tmp_path, client):
    # ADR-0001 Decision 1: the map's state lives in the URL and `view` is a
    # mode picked from a segmented control, not a checkbox — same route,
    # same graph JSON, the client just paints differently.
    import re

    seed(str(tmp_path / "test.db"))
    body = client.get("/topology").text

    for mode in ("wiring", "load", "vlan", "evidence"):
        assert f'id="view-{mode}" value="{mode}"' in body, mode
        assert f'<label for="view-{mode}">' in body, mode

    # every legend .item is tagged with the modes it applies to, and at
    # least one item claims each of the four modes
    items = re.findall(r'<span class="item"[^>]*>', body)
    assert items, "no legend items found"
    assert all('data-modes="' in item for item in items), "every .item must carry data-modes"
    for mode in ("wiring", "load", "vlan", "evidence"):
        assert any(
            (m := re.search(r'data-modes="([^"]*)"', item)) and mode in m.group(1).split()
            for item in items
        ), f"no legend item claims mode {mode}"

    # the old load-view-checkbox-driven show/hide is gone
    assert "lnksrc" not in body

    # toolbar ids the ADR/brief pin down as stable stay stable
    for control_id in ("hideoff", "coreonly", "hosts", "unmhosts", "loadmode",
                        "vlansel", "legend", "leg-heat", "leg-peak", "leg-vlan", "topo"):
        assert f'id="{control_id}"' in body, control_id


def test_topology_view_load_vlan_and_focus_params_do_not_change_the_graph(clean_env, tmp_path, client):
    # Decision 1's whole point: no route change. The graph JSON is identical
    # no matter what the client will do with `view`/`vlan`/`focus` — those
    # are client-side paint, not server-side filters.
    seed(str(tmp_path / "test.db"))
    base = _graph(client, tmp_path)
    for qs in ("?view=load&load=peak", "?view=vlan&vlan=24", "?view=evidence",
               "?focus=sw1", "?view=bogus", "?vlan=99999"):
        assert _graph_at(client, qs) == base, qs


def _graph_at(client, qs):
    import json
    import re

    r = client.get(f"/topology{qs}")
    assert r.status_code == 200
    m = re.search(r"const graph = (\{.*?\});", r.text, re.S)
    assert m
    return json.loads(m.group(1))
# --- overview-exceptions ---

def test_overview_attention_list_is_flat_and_below_the_counts(clean_env, tmp_path, client):
    # issue #13: a slow link is one line in one list, positioned after the
    # stat grid; device state is NOT repeated here (the cards are that UI)
    dbp = str(tmp_path / "test.db")
    seed(dbp)
    c = sqlite3.connect(dbp)
    c.execute("UPDATE devices SET status = 'down' WHERE name = 'fw1'")
    c.execute("UPDATE interfaces SET speed_bps = 10000000 WHERE name IN ('1/0/1', 'vmnic0')")
    c.commit(); c.close()

    body = client.get("/").text
    assert "hyp1 vmnic0 ↔ sw1 1/0/1 runs at 10M" in body
    assert '<a href="/topology?focus=hyp1">' in body
    assert "All clear" not in body
    assert "not up" not in body                      # no device-down banner
    assert body.index('class="statgrid"') < body.index('class="attention"')


def test_overview_expected_declaration_silences_an_item(clean_env, tmp_path, client):
    dbp = str(tmp_path / "test.db")
    seed(dbp)
    c = sqlite3.connect(dbp)
    c.execute("UPDATE interfaces SET speed_bps = 10000000 WHERE name IN ('1/0/1', 'vmnic0')")
    c.commit(); c.close()

    clean_env.setenv("PATCHBAY_EXPECT", "sw1:1/0/1")
    body = client.get("/").text
    assert "runs at 10M" not in body                 # the declared port is quiet
    assert "All clear" in body                       # and the list can go clear

    clean_env.setenv("PATCHBAY_EXPECT", "hyp1")      # bare device silences too
    assert "runs at 10M" not in client.get("/").text


def test_overview_says_all_clear_when_nothing_is(clean_env, tmp_path, client):
    seed(str(tmp_path / "test.db"))
    body = client.get("/").text
    assert "All clear — no unexpected slow links" in body


def test_overview_hides_the_strip_when_nothing_could_be_checked(client):
    # first-run empty DB: no links, no IPAM, no polls yet — the list has
    # nothing honest to say, so it says nothing
    body = client.get("/").text
    assert '<section class="attention">' not in body


def _aggregate_db(tmp_path):
    """seed() plus a down port, an AP with one fresh and one stale client,
    and a stopped VM — one of each thing the aggregate filters hide."""
    dbp = str(tmp_path / "test.db")
    seed(dbp)
    c = sqlite3.connect(dbp)
    c.row_factory = sqlite3.Row
    sid = c.execute("SELECT id FROM devices WHERE name = 'sw1'").fetchone()[0]
    pdb.upsert_interface(c, device_id=sid, name="1/0/2", oper_status="down",
                         speed_bps=1_000_000_000, description="spare")
    pdb.upsert_device(c, name="ap1", source="unifi", role="ap", status="up")
    pdb.upsert_endpoint(c, mac="02:00:00:00:00:01", source="unifi",
                        hostname="phone", ip="192.0.2.50", device="ap1",
                        interface="lab-wifi", vlan=24)
    pdb.upsert_endpoint(c, mac="02:00:00:00:00:02", source="unifi",
                        hostname="tablet", ip="192.0.2.51", device="ap1",
                        interface="lab-wifi", vlan=24)
    c.execute("UPDATE endpoints SET last_seen = last_seen - 3600 "
              "WHERE mac = '02:00:00:00:00:02'")
    pdb.upsert_device(c, name="vm-b", source="vsphere", role="vm",
                      parent="hyp1", status="down")
    c.commit(); c.close()
    return dbp


def test_aggregate_ports(clean_env, tmp_path, client):
    # issue #26: every fabric port on one page, Device column linking home,
    # Up by default, ?state=all addressable in the URL
    _aggregate_db(tmp_path)
    body = client.get("/ports").text
    assert 'href="/device/sw1"' in body
    assert "1/0/1" in body and "uplink [3]" in body
    assert "1/0/2" not in body                        # down port hidden
    every = client.get("/ports?state=all").text
    assert "1/0/2" in every and "spare" in every
    # entry link rides the Overview section heading
    assert 'href="/ports">all ports →</a>' in client.get("/").text


def test_aggregate_clients(clean_env, tmp_path, client):
    _aggregate_db(tmp_path)
    body = client.get("/clients").text
    assert "phone" in body and 'href="/device/ap1"' in body
    assert "lab-wifi" in body
    assert "tablet" not in body                       # stale client hidden
    every = client.get("/clients?state=all").text
    assert "tablet" in every
    assert 'href="/clients">all clients →</a>' in client.get("/").text


def test_aggregate_guests(clean_env, tmp_path, client):
    _aggregate_db(tmp_path)
    body = client.get("/guests").text
    assert "vm-a" in body and 'href="/device/hyp1"' in body
    assert "vm-b" not in body                         # stopped VM hidden
    every = client.get("/guests?state=all").text
    assert "vm-b" in every
    assert 'href="/guests">all guests →</a>' in client.get("/").text


def test_aggregates_degrade_on_empty_db(client):
    # a category with no data renders a said-out-loud empty state, and the
    # Overview shows no entry links because the headings themselves are gone
    assert "No fabric ports yet" in client.get("/ports").text
    assert "No wireless clients yet" in client.get("/clients").text
    assert "No guests yet" in client.get("/guests").text
    assert "all ports →" not in client.get("/").text


def _slow_link_db(tmp_path):
    """seed() plus a 10M link speed, so the slow-link check fires."""
    dbp = str(tmp_path / "test.db")
    seed(dbp)
    c = sqlite3.connect(dbp)
    c.execute("UPDATE interfaces SET speed_bps = 10000000 WHERE name IN ('1/0/1', 'vmnic0')")
    c.commit(); c.close()
    return dbp


def test_attention_section_and_alerts_page(clean_env, tmp_path, client):
    # issue #28: the Overview's attention list is a distinct capped section
    # with a severity-colored category strip, and /alerts is the full page
    _slow_link_db(tmp_path)
    body = client.get("/").text
    assert '<section class="attention">' in body
    assert ">Attention</a></h2>" in body
    assert 'class="attn-chip crit"' in body          # 10M link = crit
    assert "1 slow links" in body
    assert '<ul class="attn capped">' in body
    assert 'href="/alerts"' in body

    page = client.get("/alerts").text
    assert "runs at 10M" in page
    assert "slow links" in page

    # the filtered state is URL-addressable, like the map
    assert "runs at 10M" in client.get("/alerts?category=link").text
    assert "runs at 10M" not in client.get("/alerts?category=ipam").text
    assert "runs at 10M" in client.get("/alerts?severity=crit").text
    assert "runs at 10M" not in client.get("/alerts?severity=warn").text
    assert "Nothing matches this filter" in client.get("/alerts?category=ipam").text


def test_alerts_page_degrades(clean_env, tmp_path, client):
    # empty DB: checks have nothing to look at; seeded clean DB: all clear
    assert "No checks have anything to look at yet" in client.get("/alerts").text
    seed(str(tmp_path / "test.db"))
    assert "All clear" in client.get("/alerts").text


def test_first_seen_recorded_at_poll_time(clean_env, tmp_path):
    # issue #28 tie-in with #22: an item's first_seen is written by the poll
    # path, kept while it fires, and forgotten when it clears — so a
    # condition that clears and returns reads as new, which it is
    from patchbay import db as pdb2
    from patchbay.attention import (attention_items, record_first_seen,
                                    stamp_first_seen)
    from patchbay.config import load_settings

    dbp = _slow_link_db(tmp_path)
    clean_env.setenv("PATCHBAY_DB", dbp)
    settings = load_settings()
    c = sqlite3.connect(dbp)
    c.row_factory = sqlite3.Row

    items, _ = attention_items(c, settings)
    assert items and items[0]["category"] == "link" and items[0]["key"]
    stamp_first_seen(c, items)
    assert items[0]["first_seen"] is None            # nothing recorded yet

    record_first_seen(c, settings)
    stamp_first_seen(c, items)
    first = items[0]["first_seen"]
    assert first is not None
    record_first_seen(c, settings)                   # still firing: kept
    stamp_first_seen(c, items)
    assert items[0]["first_seen"] == first

    # the condition clears -> the key is forgotten
    c.execute("UPDATE interfaces SET speed_bps = 10000000000")
    c.commit()
    record_first_seen(c, settings)
    stamp_first_seen(c, items)
    assert items[0]["first_seen"] is None
    c.close()


def test_overview_folds_vms_into_hypervisors(clean_env, tmp_path, client):
    seed(str(tmp_path / "test.db"))
    body = client.get("/").text
    assert "<details>" in body
    assert '<summary class="sub">1/1 VMs running</summary>' in body
    assert "vm-a" in body
    assert "<th>Host</th>" not in body


def test_overview_drops_the_links_table(clean_env, tmp_path, client):
    seed(str(tmp_path / "test.db"))
    body = client.get("/").text
    assert "<h2>Links</h2>" not in body
    assert "<tr><th>A</th><th>Port</th><th>B</th><th>Port</th><th>Source</th></tr>" not in body
# --- deep-links-and-device-endpoints ---

def test_device_page_folds_endpoints_into_ports(clean_env, tmp_path, client):
    # ADR-0001 Decision 7: endpoints fold into the ports table (exact port
    # name first, then the same "ethernet"-prefix strip port_vlans/port_roles
    # use); AP clients (interface = SSID) and MAC-only rows land in the
    # not-tied-to-a-port section instead.
    import re

    dbp = str(tmp_path / "test.db")
    seed(dbp)
    c = sqlite3.connect(dbp)
    c.row_factory = sqlite3.Row
    pdb.init(c)
    sid = pdb.upsert_device(c, name="sw1", source="librenms")
    pdb.upsert_interface(c, device_id=sid, name="ethernet1/0/5", oper_status="up")
    pdb.upsert_endpoint(c, mac="02:00:00:00:00:01", source="fdb", device="sw1",
                        interface="1/0/1", hostname="laptop1", ip="192.0.2.50", vlan=24)
    pdb.upsert_endpoint(c, mac="02:00:00:00:00:02", source="fdb", device="sw1",
                        interface="1/0/5", hostname="laptop2", ip="192.0.2.51", vlan=24)
    pdb.upsert_endpoint(c, mac="02:00:00:00:00:03", source="unifi", device="sw1",
                        interface="Guest-WiFi", hostname="phone1")
    pdb.upsert_endpoint(c, mac="02:00:00:00:00:04", source="fdb", device="sw1")
    c.commit(); c.close()

    body = client.get("/device/sw1").text
    assert "Endpoints seen here" not in body

    # exact port-name match: the count link shows "1" and its eprow carries
    # the endpoint's hostname
    assert re.search(r'class="ep" data-if="1/0/1"[^>]*>1<', body)
    m = re.search(r'<tr class="eprow" data-if="1/0/1">(.*?)'
                  r'(?=<tr class="eprow"|</template>)', body, re.S)
    assert m and "laptop1" in m.group(1)

    # the "ethernet" prefix fallback: keyed by the port's own name, not the
    # bare interface number the endpoint carries
    m = re.search(r'<tr class="eprow" data-if="ethernet1/0/5">(.*?)'
                  r'(?=<tr class="eprow"|</template>)', body, re.S)
    assert m and "laptop2" in m.group(1)

    # not tied to a port: the AP client (by SSID) and the MAC-only row
    assert "Endpoints not tied to a port (2)" in body
    assert "phone1" in body and "Guest-WiFi" in body


def test_vlans_row_links_to_the_map(clean_env, tmp_path, client):
    # ADR-0001 Decisions 1 & 6: only non-default URL params are serialized
    seed(str(tmp_path / "test.db"))
    body = client.get("/vlans").text
    assert "/topology?view=vlan&vlan=24" in body
    assert "hideoff=" not in body


def test_patchpanel_rows_are_anchored(clean_env, tmp_path, client):
    # seed()'s sw1 port carries description "uplink [3]" -> populated
    # position 3; /patchpanel?panel=<name>#p3 must have somewhere to land
    seed(str(tmp_path / "test.db"))
    body = client.get("/patchpanel").text
    assert 'id="p3"' in body


def test_panel_size_sits_on_the_purpose_line(clean_env, tmp_path, client):
    # issue #18: the declared size is data, not a control — in the header's
    # controls slot it sat beside the <h1> and read like part of the name
    clean_env.setenv("PATCHBAY_PANELS", r"Basement:12=\[(\d+)\]")
    seed(str(tmp_path / "test.db"))
    body = client.get("/patchpanel").text
    assert "them · 12 positions</p>" in body
    controls = body.split('<div class="controls">', 1)[1].split("</div>", 1)[0]
    assert "positions" not in controls
# --- map-tiers-fit ---


def test_topology_page_renders_tier_bands(clean_env, tmp_path, client):
    # ADR-0001 Decision 2: RANK becomes four labelled swimlanes drawn behind
    # the nodes; the map fits itself to the frame instead of "breathing" in
    # two dimensions. The bands are drawn client-side (data-driven — a band
    # only renders once a rank has a visible node), so what ships is the JS
    # that draws them, not server-rendered markup.
    seed(str(tmp_path / "test.db"))
    body = client.get("/topology").text

    for name in ("Internet", "Edge", "Fabric", "Access & compute"):
        assert f'"{name}"' in body, name

    # a `fit` control exists in the toolbar, next to nothing else
    assert 'id="fitbtn"' in body

    # layout is a URL mode (issue #11): free is the default (soft rank force,
    # full XY pins), tiers is the opt-in where the band owns every Y
    assert 'params.get("layout") === "tiers"' in body
    assert 'id="tierbox"' in body
    assert "d3.forceY(d => bandCenter(d.rank))" in body   # free mode's nudge
    assert "n.y = n.fy = bandCenter(n.rank)" in body      # tiers still owns Y

    # the simulation can now shrink further to fit a large map to the frame
    assert "scaleExtent([0.2, 3])" in body


def test_topology_remembers_view_options(clean_env, tmp_path, client):
    # issue #16: a bare /topology restores the remembered state by rewriting
    # the URL before TIERS or readState() look at it; explicit params always
    # win (no merging); focus and foreign params never persist; the snapshot
    # is exempt (SNAPSHOT guards both sides)
    seed(str(tmp_path / "test.db"))
    body = client.get("/topology").text
    assert 'localStorage.getItem("patchbay.topo")' in body
    assert 'localStorage.setItem("patchbay.topo"' in body
    assert '!SNAPSHOT && !location.search && !location.hash.includes("=")' in body
    assert 'keep.delete("focus")' in body


# --- poll-driven refresh (issue #6) ---

def test_freshness_is_null_before_the_first_poll(client):
    r = client.get("/api/freshness")
    assert r.status_code == 200
    assert r.json() == {"last_poll": None}


def test_freshness_reports_the_last_poll_time(client, tmp_path):
    c = sqlite3.connect(str(tmp_path / "test.db"))
    pdb.init(c)
    pdb.save_last_poll(c, ["[ok] test"])
    c.commit()
    c.close()
    ts = client.get("/api/freshness").json()["last_poll"]
    assert isinstance(ts, float) and ts > 0


def test_pages_refresh_by_freshness_not_by_timer(client, tmp_path):
    """The blind 60-second meta refresh is gone everywhere; every shell page
    carries the freshness script instead, and the header age is tickable."""
    seed(str(tmp_path / "test.db"))
    for page in PAGES:
        body = client.get(page).text
        assert "http-equiv" not in body, page
        assert "/api/freshness" in body, page
    # the tickable header age renders once a source has reported
    c = sqlite3.connect(str(tmp_path / "test.db"))
    pdb.init(c)
    pdb.save_raw(c, source="librenms", endpoint="devices", payload=[])
    c.commit()
    c.close()
    assert 'id="agespan"' in client.get("/").text


def test_fragile_pages_hold_the_refresh(client, tmp_path):
    """Pages whose on-screen state a reload would destroy define the hold
    hook; the plain dashboard does not (it reloads freely). The topology
    map no longer holds itself — its first-touch-holds-forever rule parked
    a browser on 16-hour-old data; the header's auto-refresh toggle is the
    explicit hold now."""
    seed(str(tmp_path / "test.db"))
    for page in ("/ops", "/snapshots"):
        assert "patchbayHold" in client.get(page).text, page
    assert "_topoTouched" not in client.get("/topology").text
    body = client.get("/").text
    assert "window.patchbayHold = " not in body


def test_auto_refresh_toggle_and_readable_age(clean_env, tmp_path, client):
    # the header carries an explicit auto-refresh toggle (per-browser,
    # default on) beside the age — and the age reads in human units
    import patchbay.web as web

    assert web.human_age(45) == "45 min"
    assert web.human_age(967) == "16 h"
    assert web.human_age(3000) == "2 d"

    c = sqlite3.connect(str(tmp_path / "test.db"))
    pdb.init(c)
    pdb.save_raw(c, source="librenms", endpoint="devices", payload=[])
    c.commit(); c.close()
    body = client.get("/").text
    assert 'id="autoref"' in body
    assert 'aria-pressed' in body
    assert '"patchbay.autorefresh"' in body
    assert "0 min</span> ago" in body        # humanized server render
    assert "min ago</span>" not in body      # the raw form is gone


def test_port_graphs_carry_readable_captions(client, tmp_path):
    """Issue #5: rrdtool's legend is illegible vector paths, so the graph row
    names each series in HTML with the image's exact colors and spells out
    the unit suffixes (m = milli, not mega)."""
    seed(str(tmp_path / "test.db"))
    body = client.get("/device/sw1").text
    for needle in ("Errors In", "Discards Out", "#805080",
                   "m = milli (thousandths)", "95th pct"):
        assert needle in body, needle


# --- config version view spacing (issue #12) ---

def test_version_text_never_doubles_newlines(clean_env, monkeypatch):
    """oxidized-web's list-shaped view response carries a trailing newline on
    each element; joining with '\n' doubled every blank line. CRLF string
    bodies doubled the same way in <pre>."""
    import httpx
    import patchbay.web as web

    shapes = [
        ["line one\n", "line two\n", "\n", "line three\n"],   # list w/ newlines
        ["line one", "line two", "", "line three"],           # list w/o
        "line one\r\nline two\r\n\r\nline three\r\n",         # CRLF string
        {"output": "line one\nline two\n\nline three\n"},     # dict, clean
    ]
    for shape in shapes:
        def handler(request, s=shape):
            return httpx.Response(200, json=s)
        client = httpx.Client(transport=httpx.MockTransport(handler),
                              base_url="http://ox.invalid")
        text = web._ox_version_text(client, "core1", {"oid": "x"}, 0)
        assert "\n\n\n" not in text, shape       # a blank line stays one blank line
        assert "\r" not in text
        assert text.count("line three") == 1
        assert "line one\nline two\n" in text.replace("\n\n", "\n")


def test_topology_layout_defaults_to_free(client, tmp_path):
    """Issue #11: after real-world use the tier bands lost the default; the
    map arranges freely unless layout=tiers is asked for."""
    seed(str(tmp_path / "test.db"))
    body = client.get("/topology").text
    assert 'layout: "free"' in body                      # the default in DEFAULTS
    assert "if (!TIERS) { gBands.selectAll" in body      # no bands drawn in free
    assert "if (!TIERS) d.fy = e.y" in body              # free pins both axes


def test_labeled_unmanaged_node_keeps_vlan_chips(clean_env, tmp_path, client):
    # a custom-labeled unmanaged node no longer encodes its feeding port in
    # its name; the VLAN chips must come from the declaration instead
    clean_env.setenv("PATCHBAY_UNMANAGED", "closet-switch=sw1:1/0/9")
    db_path = str(tmp_path / "test.db")
    seed(db_path)
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("INSERT INTO port_vlans (device, interface, vid, tagged, source) "
              "VALUES ('sw1', '1/0/9', 24, 1, 'oxidized')")
    from patchbay.config import load_settings
    from patchbay.normalize import normalize
    s = load_settings()
    normalize(c, declared_unmanaged=s.unmanaged)
    c.commit(); c.close()
    g = _graph(client, tmp_path)
    node = next(n for n in g["nodes"] if n["name"] == "closet-switch")
    assert node["label"] == "closet-switch"
    assert 24 in node["vlans"]


def test_labeled_unmanaged_node_gets_vlans_from_endpoint_ips(clean_env, tmp_path, client):
    # the last-resort VLAN fallback (addresses of endpoints behind the
    # switch) must find labeled nodes too, not only unmanaged@ names
    clean_env.setenv("PATCHBAY_UNMANAGED", "closet-switch=sw1:1/0/9")
    db_path = str(tmp_path / "test.db")
    seed(db_path)
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    from patchbay.config import load_settings
    from patchbay.normalize import normalize
    s = load_settings()
    normalize(c, declared_unmanaged=s.unmanaged)
    # an endpoint behind the labeled switch with an address in VLAN 24's subnet
    pdb.upsert_endpoint(c, mac="02:00:00:00:08:01", source="fdb",
                        device="closet-switch", interface="?", ip="192.0.2.77")
    c.commit(); c.close()
    g = _graph(client, tmp_path)
    node = next(n for n in g["nodes"] if n["name"] == "closet-switch")
    assert 24 in node["vlans"]


# --- /configs: patchbay-held firewall history (#23) --------------------------

def _seed_fw_history(db_path):
    from patchbay.collectors.opnsense import save_config_revision
    from tests.test_collectors import _CFG_XML
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    pdb.init(c)
    save_config_revision(c, "fw1", _CFG_XML)
    changed = _CFG_XML.replace("allow lan", "allow lan and dmz").replace(
        "/firewall_rules.php made changes", "rule edited")
    save_config_revision(c, "fw1", changed)
    ids = [r[0] for r in c.execute(
        "SELECT id FROM config_revisions ORDER BY fetched_at, id")]
    c.commit(); c.close()
    return ids


def test_configs_lists_firewall_history_without_oxidized(clean_env, tmp_path, client):
    _seed_fw_history(str(tmp_path / "test.db"))
    r = client.get("/configs")
    assert r.status_code == 200
    assert "fw1" in r.text
    assert "rule edited" in r.text          # timeline carries the description


def test_confignode_shows_firewall_revision_and_diff(clean_env, tmp_path, client):
    old_id, new_id = _seed_fw_history(str(tmp_path / "test.db"))
    r = client.get(f"/configs/fw1?v={new_id}")
    assert r.status_code == 200
    assert "allow lan and dmz" in r.text
    assert "FAKEKEYB64" not in r.text
    d = client.get(f"/configs/fw1?v={new_id}&prev={old_id}")
    assert d.status_code == 200
    assert "allow lan and dmz" in d.text    # the diff shows the real change


# --- /routed (#17) -----------------------------------------------------------

def test_routed_page_renders_graph(clean_env, tmp_path, client):
    seed(str(tmp_path / "test.db"))
    r = client.get("/routed")
    assert r.status_code == 200
    assert '"rails"' in r.text and '"hosts"' in r.text
    assert "lab 24" in r.text or "lab" in r.text   # the seeded VLAN reaches the page
    # empty DB stays a page, not a crash
    r2 = client.get("/routed?hosts=0&groups=0&focus=v24")
    assert r2.status_code == 200


# --- /configs canonical display names ---------------------------------------

def test_canonical_label_shortens_known_fqdn_only():
    from patchbay.web import _canonical_label
    canon = {"core1", "fw1"}
    assert _canonical_label("core1.example.internal", canon) == "core1"
    assert _canonical_label("core1", canon) == "core1"
    # an FQDN whose first label is not a known device stays whole
    assert _canonical_label("mystery.example.internal", canon) == "mystery.example.internal"


def test_routed_graph_json_is_script_safe(clean_env, tmp_path, client):
    # a hostile VLAN name (IPAM free text, LLDP sysnames) must not be able
    # to break out of the routed page's <script> element
    path = str(tmp_path / "test.db")
    seed(path)
    import sqlite3

    c = sqlite3.connect(path)
    c.execute("INSERT OR REPLACE INTO vlans (vid, name, source) "
              "VALUES (66, '</script><script>alert(1)', 'ipam')")
    c.execute("INSERT INTO subnets (cidr, vlan, source) VALUES "
              "('192.0.2.128/29', 66, 'ipam')")
    # an attached host so the rail survives the participation filter
    c.execute("INSERT INTO devices (name, source, role, status) "
              "VALUES ('victim1', 'test', 'host', 'up')")
    c.execute("INSERT INTO interfaces (device_id, name, ip) VALUES "
              "((SELECT id FROM devices WHERE name='victim1'), "
              "'eth0', '192.0.2.130/29')")
    c.commit(); c.close()
    r = client.get("/routed")
    assert r.status_code == 200
    assert "</script><script>alert" not in r.text
    assert "\\u003c/script" in r.text     # the name reached the page, escaped
