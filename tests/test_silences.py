"""Silences (issue #62, ADR-0003 Decision 6): each scope kind, expiry, the
PATCHBAY_EXPECT bridge, the engine's silenced state (no clear when an
alert is silenced, one raise when the silence ends), Kuma released by a
silence, and the Silences and Silenced tabs agreeing with the Overview.
Every model here is synthetic."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from patchbay import alerting, attention, auth, silences, transports
from patchbay import db as pdb
from patchbay.config import load_settings

T0 = 1_800_000_000.0
DAY = 86400.0
KUMA = "https://kuma.example.com/api/push/s3cr3tPushTok?status=up&msg=OK&ping="


def _item(key="link:sw1:1/0/1:hyp1:vmnic0", rule="stale-source", category="source",
          severity="warn", **extra):
    return {"key": key, "rule": rule, "category": category, "severity": severity,
            "text": f"{key} text", "href": "/ops", **extra}


def _silence(conn, kind, scope, until=None, reason="known"):
    return silences.add(conn, {"kind": kind, "scope": scope, "until": until,
                               "reason": reason, "created_by": "operator",
                               "created_at": T0})


def _mark(conn, items, now=T0, expected=()):
    class S:
        pass
    s = S()
    s.expected = set(expected)
    silences.apply(conn, items, s, now)
    return items


def _alerts(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM alerts ORDER BY id")]


def _events(conn):
    return [(r["event"], r["detail"]) for r in
            conn.execute("SELECT * FROM alert_events ORDER BY id")]


# -- scopes ---------------------------------------------------------------

def test_each_scope_kind_covers_what_it_names(conn):
    down = _item("device:sw1", "device-down", "device", "crit", devices=["sw1"])
    link = _item("link-down:ap1:eth0:sw1:1/0/2", "link-down", "link",
                 ports=[("ap1", "eth0"), ("sw1", "1/0/2")])
    gw = _item("gateway:WAN_GW", "gateway-degraded", "gateway", devices=["WAN_GW"])
    stale = _item("source:stale")

    def silenced(*items):
        return [i["key"] for i in _mark(conn, list(items)) if i["silenced"]]

    # a device covers its own item and every item with a port on it
    sid = _silence(conn, "device", "sw1")
    assert silenced(down, link, gw, stale) == ["device:sw1", link["key"]]
    silences.delete(conn, sid)

    # a port covers items at that port only, not its device's own item
    sid = _silence(conn, "port", "sw1:1/0/2")
    assert silenced(down, link, gw, stale) == [link["key"]]
    silences.delete(conn, sid)

    sid = _silence(conn, "category", "source")
    assert silenced(down, link, gw, stale) == ["source:stale"]
    silences.delete(conn, sid)

    sid = _silence(conn, "key", "gateway:WAN_GW")
    assert silenced(down, link, gw, stale) == ["gateway:WAN_GW"]
    silences.delete(conn, sid)

    assert silenced(down, link, gw, stale) == []


def test_the_longest_lasting_silence_is_the_one_shown(conn):
    _silence(conn, "category", "source", until=T0 + 3600)
    _silence(conn, "key", "source:stale", until=None, reason="forever")
    _silence(conn, "key", "source:stale", until=T0 + DAY)
    [it] = _mark(conn, [_item("source:stale")])
    assert it["silenced"]["reason"] == "forever"


def test_rule_items_carry_the_names_silences_match(conn, clean_env):
    # slow-link names both ends, so a device or port silence reaches it
    pdb.upsert_device(conn, name="sw1", source="librenms", role="switch", status="up")
    pdb.upsert_device(conn, name="hyp1", source="vsphere", role="hypervisor", status="up")
    for dev in ("sw1", "hyp1"):
        did = conn.execute("SELECT id FROM devices WHERE name = ?", (dev,)).fetchone()[0]
        pdb.upsert_interface(conn, device_id=did, name="1/0/1" if dev == "sw1" else "vmnic0",
                             oper_status="up", speed_bps=10_000_000)
    pdb.upsert_link(conn, a_device="sw1", a_interface="1/0/1", b_device="hyp1",
                    b_interface="vmnic0", source="lldp")
    _silence(conn, "port", "hyp1:vmnic0")
    [it] = [i for i in attention.attention_items(conn, load_settings())[0]
            if i["rule"] == "slow-link"]
    assert it["silenced"]["scope"] == "hyp1:vmnic0"


# -- expiry ---------------------------------------------------------------

def test_expired_silence_covers_nothing(conn):
    _silence(conn, "key", "source:stale", until=T0 + 3600)
    assert _mark(conn, [_item("source:stale")], now=T0 + 3599)[0]["silenced"]
    assert _mark(conn, [_item("source:stale")], now=T0 + 3600)[0]["silenced"] is None


def test_expiry_while_the_condition_holds_is_one_raise_and_no_clear(conn):
    it = _item("source:stale")
    assert [n.kind for n in alerting.evaluate(conn, _mark(conn, [dict(it)]), now=T0)] == ["raise"]

    # silenced from the Active tab: no clear, the row stays, marked
    _silence(conn, "key", "source:stale", until=T0 + DAY)
    for t in (T0 + 300, T0 + 600, T0 + DAY - 1):
        assert alerting.evaluate(conn, _mark(conn, [dict(it)], now=t), now=t) == []
    [a] = _alerts(conn)
    assert a["state"] == "silenced"

    # the next poll after `until` raises as new: exactly one raise
    t = T0 + DAY + 300
    notes = alerting.evaluate(conn, _mark(conn, [dict(it)], now=t), now=t)
    assert [n.kind for n in notes] == ["raise"]
    assert notes[0].raised_at == t
    [a] = _alerts(conn)
    assert (a["state"], a["raised_at"], a["polls"]) == ("active", t, 1)
    assert _events(conn) == [
        ("raised", None), ("active", None),
        ("silenced", "alert source:stale: known"),
        ("raised", "silence ended"), ("active", None)]
    assert alerting.evaluate(conn, _mark(conn, [dict(it)], now=t + 300), now=t + 300) == []


def test_unsilenced_alert_counts_for_again(conn):
    alerting.seed_rules(conn)
    conn.execute("UPDATE alert_rules SET params = '{\"for\": 2}' WHERE name = 'stale-source'")
    _silence(conn, "key", "source:stale", until=T0 + 600)
    it = _item("source:stale")
    assert alerting.evaluate(conn, _mark(conn, [dict(it)]), now=T0) == []
    t = T0 + 900
    assert alerting.evaluate(conn, _mark(conn, [dict(it)], now=t), now=t) == []
    assert _alerts(conn)[0]["state"] == "pending"
    t += 300
    assert [n.kind for n in alerting.evaluate(conn, _mark(conn, [dict(it)], now=t), now=t)] \
        == ["raise"]


def test_condition_ending_while_silenced_sends_nothing(conn):
    it = _item("source:stale")
    alerting.evaluate(conn, _mark(conn, [dict(it)]), now=T0)
    _silence(conn, "key", "source:stale")
    alerting.evaluate(conn, _mark(conn, [dict(it)], now=T0 + 300), now=T0 + 300)
    assert alerting.evaluate(conn, [], now=T0 + 600) == []
    [a] = _alerts(conn)
    assert a["state"] == "cleared"
    assert _events(conn)[-1] == ("cleared", "silenced")


def test_silenced_from_the_start_is_stored_never_sent(conn):
    _silence(conn, "category", "source")
    it = _item("source:stale", severity="crit")
    assert alerting.evaluate(conn, _mark(conn, [dict(it)]), now=T0) == []
    [a] = _alerts(conn)
    assert (a["state"], a["active_at"], a["last_notified_at"]) == ("silenced", None, None)
    # a severity rise under a silence is not news either
    assert alerting.evaluate(conn, _mark(conn, [dict(it)], now=T0 + 300), now=T0 + 300) == []


def test_silenced_event_is_recorded_and_never_replayed(conn):
    ev = _item("config:sw1:7:abc", "config-changed", "config", "info", devices=["sw1"])
    sid = _silence(conn, "device", "sw1")
    assert alerting.evaluate(conn, _mark(conn, [dict(ev)]), now=T0) == []
    silences.delete(conn, sid)
    # the occurrence is past; removing the silence does not send it late
    assert alerting.evaluate(conn, _mark(conn, [dict(ev)], now=T0 + 300), now=T0 + 300) == []
    assert [e for e, _ in _events(conn)] == ["silenced", "cleared"]


def test_silence_wins_over_inhibition(conn):
    # a link-down held by a down device would stay active (and hold Kuma
    # down) forever; an operator's silence releases it
    pdb.upsert_device(conn, name="sw1", source="librenms", status="up")
    pdb.upsert_device(conn, name="ap1", source="unifi", status="up")
    link = _item("link-down:ap1:eth0:sw1:1/0/2", "link-down", "link",
                 ports=[("ap1", "eth0"), ("sw1", "1/0/2")])
    alerting.evaluate(conn, _mark(conn, [dict(link)]), now=T0)
    conn.execute("UPDATE devices SET status = 'down' WHERE name = 'ap1'")
    _silence(conn, "device", "ap1")
    assert alerting.evaluate(conn, _mark(conn, [dict(link)], now=T0 + 300),
                             now=T0 + 300) == []
    assert _alerts(conn)[0]["state"] == "silenced"


def test_expired_silences_age_out_with_history(conn):
    _silence(conn, "key", "a", until=T0 - 91 * DAY)
    _silence(conn, "key", "b", until=T0 - DAY)
    _silence(conn, "key", "c")
    alerting.prune_history(conn, now=T0)
    assert [r["scope"] for r in conn.execute("SELECT scope FROM alert_silences ORDER BY id")] \
        == ["b", "c"]


# -- Kuma -----------------------------------------------------------------

def test_silenced_alert_does_not_hold_kuma_down(tmp_path):
    from tests.test_transports import Recorder

    p = str(tmp_path / "t.db")
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    pdb.init(c)
    c.execute("INSERT INTO alert_transports (name, kind, url) VALUES ('kuma', 'kuma', ?)",
              (KUMA,))
    c.commit()
    rec = Recorder()

    def poll(now):
        notes = alerting.evaluate(c, _mark(c, [_item("source:stale")], now=now), now=now)
        c.commit()
        alerting.dispatch(notes, transports.WebhookDispatcher(p, transport=rec.transport,
                                                             now=now))
        return notes

    poll(T0)
    _silence(c, "key", "source:stale", until=T0 + 3600)
    c.commit()
    assert poll(T0 + 300) == []                  # silenced: nothing sent
    poll(T0 + 3900)                              # expired, still holds
    assert [dict(r.url.params)["status"] for r in rec.seen] == ["down", "up", "down"]
    c.close()


# -- the PATCHBAY_EXPECT bridge -------------------------------------------

def test_expect_entries_are_permanent_read_only_silences(clean_env, conn):
    clean_env.setenv("PATCHBAY_EXPECT", "sw1:1/0/2, hyp1")
    rows = silences.all_silences(conn, load_settings())
    assert [(r["kind"], r["scope"], r["until"], r["source"]) for r in rows] == [
        ("device", "hyp1", None, "env"), ("port", "sw1:1/0/2", None, "env")]
    assert all(isinstance(r["id"], str) for r in rows)   # no form can address one


def _web(clean_env, tmp_path, *, speed=10_000_000, **env):
    from tests.test_web import seed

    for k, v in env.items():
        clean_env.setenv(k, v)
    dbp = str(tmp_path / "test.db")
    seed(dbp)
    c = sqlite3.connect(dbp)
    c.execute("UPDATE interfaces SET speed_bps = ? WHERE name IN ('1/0/1', 'vmnic0')",
              (speed,))
    c.commit(); c.close()
    import patchbay.web as web
    return dbp, TestClient(web.app, follow_redirects=False)


def test_expect_overview_unchanged_and_item_on_silenced_tab(clean_env, tmp_path):
    dbp, client = _web(clean_env, tmp_path, PATCHBAY_EXPECT="sw1:1/0/1")
    body = client.get("/").text
    assert "runs at 10M" not in body and "All clear" in body   # as before #62
    assert "runs at 10M" not in client.get("/alerts").text
    page = client.get("/alerts?tab=silenced").text
    assert "runs at 10M" in page and "port sw1:1/0/1" in page
    page = client.get("/alerts?tab=silences").text
    assert "port sw1:1/0/1" in page and "edit on Ops" in page
    assert "/delete" not in page                      # read-only here

    # a poll stores it silenced and sends nothing
    assert client.post("/ops/poll").status_code == 200
    c = sqlite3.connect(dbp)
    assert c.execute("SELECT state FROM alerts").fetchall() == [("silenced",)]
    c.close()


def test_ops_help_points_expect_at_the_alerts_page(clean_env, tmp_path):
    _, client = _web(clean_env, tmp_path)
    assert "/alerts?tab=silences" in client.get("/ops").text


# -- the tabs and the forms -----------------------------------------------

def test_silence_from_active_hides_it_everywhere_in_the_same_poll(clean_env, tmp_path):
    # the issue's done-when
    dbp, client = _web(clean_env, tmp_path)
    assert client.post("/ops/poll").status_code == 200
    active = client.get("/alerts").text
    assert "runs at 10M" in active and "<summary>silence</summary>" in active
    assert 'value="key:link:hyp1:vmnic0:sw1:1/0/1"' in active
    assert 'value="port:sw1:1/0/1"' in active and 'value="category:link"' in active
    assert '<option value="24" selected>' in active

    r = client.post("/alerts/silences", data={
        "target": "key:link:hyp1:vmnic0:sw1:1/0/1", "hours": "24",
        "reason": "management drop", "back": "active"})
    assert (r.status_code, r.headers["location"]) == (303, "/alerts")

    assert "runs at 10M" not in client.get("/").text
    assert "runs at 10M" not in client.get("/alerts").text
    page = client.get("/alerts?tab=silenced").text
    assert "runs at 10M" in page and "management drop" in page
    assert "Silenced (1)" in page

    assert client.post("/ops/poll").status_code == 200
    c = sqlite3.connect(dbp)
    c.row_factory = sqlite3.Row
    [a] = c.execute("SELECT * FROM alerts").fetchall()
    assert a["state"] == "silenced"
    assert [r["event"] for r in c.execute("SELECT event FROM alert_events ORDER BY id")] \
        == ["raised", "active", "silenced"]
    [s] = c.execute("SELECT * FROM alert_silences").fetchall()
    assert (s["kind"], s["created_by"]) == ("key", "operator")
    assert 23.9 * 3600 < s["until"] - s["created_at"] <= 24 * 3600
    c.close()


def test_silences_tab_add_list_expire_remove(clean_env, tmp_path):
    dbp, client = _web(clean_env, tmp_path)
    r = client.post("/alerts/silences", data={
        "kind": "port", "scope": " sw1 : 1/0/1 ", "hours": "permanent", "back": "silences"})
    assert r.headers["location"] == "/alerts?tab=silences"
    c = sqlite3.connect(dbp)
    c.execute("INSERT INTO alert_silences (kind, scope, until, created_at) "
              "VALUES ('device', 'hyp1', ?, ?)", (pdb.now() - 60, pdb.now() - 3600))
    c.commit()
    page = client.get("/alerts?tab=silences").text
    assert "port sw1:1/0/1" in page and "permanent" in page
    assert 'class="sil-old"' in page and "(expired)" in page
    sid = c.execute("SELECT id FROM alert_silences WHERE kind = 'port'").fetchone()[0]
    assert client.post(f"/alerts/silences/{sid}/delete",
                       data={"back": "silences"}).status_code == 303
    assert client.post(f"/alerts/silences/{sid}/delete").status_code == 404
    assert c.execute("SELECT COUNT(*) FROM alert_silences").fetchone()[0] == 1
    c.close()


@pytest.mark.parametrize("form", [
    {"kind": "nope", "scope": "sw1"},
    {"kind": "device", "scope": ""},
    {"kind": "device", "scope": "sw1:1/0/1"},
    {"kind": "port", "scope": "sw1"},
    {"kind": "port", "scope": ":1/0/1"},
    {"kind": "category", "scope": "weather"},
    {"kind": "key", "scope": "x" * 301},
    {"kind": "key", "scope": "a\nb"},
    {"kind": "key", "scope": "k", "hours": "soon"},
    {"kind": "key", "scope": "k", "hours": "0"},
    {"kind": "key", "scope": "k", "hours": "-3"},
    {"kind": "key", "scope": "k", "hours": "1e9"},
    {"kind": "key", "scope": "k", "reason": "r" * 501},
    {"target": "weather:sw1"},
])
def test_bad_silence_is_400_before_any_write(clean_env, tmp_path, form):
    dbp, client = _web(clean_env, tmp_path)
    assert client.post("/alerts/silences", data=form).status_code == 400
    c = sqlite3.connect(dbp)
    assert c.execute("SELECT COUNT(*) FROM alert_silences").fetchone()[0] == 0
    c.close()


def test_cross_origin_silence_posts_are_rejected(clean_env, tmp_path):
    dbp, client = _web(clean_env, tmp_path)
    evil = {"origin": "https://evil.example"}
    assert client.post("/alerts/silences", data={"kind": "category", "scope": "link"},
                       headers=evil).status_code == 403
    c = sqlite3.connect(dbp)
    sid = silences.add(c, {"kind": "key", "scope": "k", "until": None, "reason": None,
                           "created_by": "operator", "created_at": T0})
    c.commit()
    assert client.post(f"/alerts/silences/{sid}/delete", headers=evil).status_code == 403
    assert c.execute("SELECT COUNT(*) FROM alert_silences").fetchone()[0] == 1
    c.close()


def test_created_by_is_the_oidc_identity_when_there_is_one(clean_env, tmp_path):
    dbp, client = _web(clean_env, tmp_path, PATCHBAY_AUTH="oidc",
                       PATCHBAY_OIDC_CLIENT_ID="cid", PATCHBAY_OIDC_CLIENT_SECRET="cs",
                       PATCHBAY_OIDC_AUTH_URL="https://idp.example/authorize",
                       PATCHBAY_OIDC_TOKEN_URL="https://idp.example/token")
    assert client.post("/alerts/silences",
                       data={"kind": "category", "scope": "link"}).status_code == 401
    secret = auth.session_secret(load_settings())
    client.cookies.set(auth.SESSION_COOKIE,
                       auth.make_token(secret, "alice@example.com", hours=1))
    assert client.post("/alerts/silences",
                       data={"kind": "category", "scope": "link"}).status_code == 303
    c = sqlite3.connect(dbp)
    assert c.execute("SELECT created_by FROM alert_silences").fetchone()[0] == "alice@example.com"
    c.close()
