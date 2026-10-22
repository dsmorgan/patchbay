"""Silences (issue #62, ADR-0003 Decision 6): each scope kind, expiry, the
PATCHBAY_EXPECT import (the variable itself is ignored, #69), the engine's silenced state (no clear when an
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


def _mark(conn, items, now=T0):
    silences.apply(conn, items, now)
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


def test_expected_tunnel_and_port_canary_items_are_silenceable(conn, clean_env):
    from patchbay import expected_tunnels

    pdb.upsert_device(conn, name="fw1", source="opnsense", role="firewall", status="up")
    expected_tunnels.add(conn, "fw1", "wireguard", "wg-site")
    settings = load_settings()

    def tunnel():
        [it] = [i for i in attention.attention_items(conn, settings)[0]
                if i["rule"] == "expected-tunnel-missing"]
        return it

    it = tunnel()
    assert it["silenced"] is None
    # the button offers the device, never the device-level hold as a port
    assert ("device", "fw1") in silences.scopes_for(it)
    assert not any(k == "port" for k, _ in silences.scopes_for(it))
    sid = _silence(conn, "device", "fw1")
    assert tunnel()["silenced"]["scope"] == "fw1"
    silences.delete(conn, sid)
    _silence(conn, "category", "tunnel")
    assert tunnel()["silenced"]["kind"] == "category"

    canary = _item("port:canary:sw1:1/0/5:out_discards", "port-canary", "port",
                   ports=[("sw1", "1/0/5")])
    _silence(conn, "port", "sw1:1/0/5")
    assert _mark(conn, [canary])[0]["silenced"]["scope"] == "sw1:1/0/5"


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


def _cycle(conn, items, now):
    return alerting.evaluate(conn, _mark(conn, [dict(i) for i in items], now=now), now=now)


def test_announced_then_silenced_then_ended_sends_one_clear(conn):
    # the receiver heard the raise, so it must hear the clear, or it stays
    # firing forever; the silence itself still sends nothing
    it = _item("source:stale")
    assert [n.kind for n in _cycle(conn, [it], T0)] == ["raise"]
    _silence(conn, "key", "source:stale")
    assert _cycle(conn, [it], T0 + 300) == []
    notes = _cycle(conn, [], T0 + 600)
    assert [(n.kind, n.key) for n in notes] == [("clear", "source:stale")]
    assert _events(conn)[-1] == ("cleared", None)
    assert _cycle(conn, [], T0 + 900) == []


def test_never_announced_silenced_alert_ends_quietly(conn):
    _silence(conn, "key", "source:stale")
    it = _item("source:stale")
    assert _cycle(conn, [it], T0) == []
    assert _cycle(conn, [], T0 + 300) == []
    assert _events(conn)[-1] == ("cleared", "silenced")


def test_pending_then_silenced_then_ended_is_quiet(conn):
    alerting.seed_rules(conn)
    conn.execute("UPDATE alert_rules SET params = '{\"for\": 3}' WHERE name = 'stale-source'")
    it = _item("source:stale")
    assert _cycle(conn, [it], T0) == []
    assert _alerts(conn)[0]["state"] == "pending"
    _silence(conn, "key", "source:stale")
    assert _cycle(conn, [it], T0 + 300) == []
    assert _cycle(conn, [], T0 + 600) == []
    assert _events(conn)[-1] == ("cleared", "silenced")


def test_announced_reraised_pending_then_ended_sends_one_clear(conn):
    alerting.seed_rules(conn)
    conn.execute("UPDATE alert_rules SET params = '{\"for\": 2}' WHERE name = 'stale-source'")
    it = _item("source:stale")
    assert _cycle(conn, [it], T0) == []
    assert [n.kind for n in _cycle(conn, [it], T0 + 300)] == ["raise"]   # announced
    _silence(conn, "key", "source:stale", until=T0 + 900)
    assert _cycle(conn, [it], T0 + 600) == []
    assert _cycle(conn, [it], T0 + 1200) == []        # expired: pending again
    [a] = _alerts(conn)
    assert (a["state"], a["last_notified_at"]) == ("pending", T0 + 300)
    notes = _cycle(conn, [], T0 + 1500)
    assert [n.kind for n in notes] == ["clear"]


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


def test_queued_retry_for_a_silenced_alert_is_dropped(tmp_path):
    from tests.test_transports import HOOK, Recorder

    p = str(tmp_path / "t.db")
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    pdb.init(c)
    c.execute("INSERT INTO alert_transports (name, kind, url) VALUES ('hook', 'generic', ?)",
              (HOOK,))
    c.commit()

    def poll(now, rec):
        notes = _cycle(c, [_item("source:stale")], now)
        c.commit()
        alerting.dispatch(notes, transports.WebhookDispatcher(p, transport=rec.transport,
                                                             now=now))

    down = Recorder(status=500)
    poll(T0, down)                               # the raise fails and is queued
    assert len(down.seen) == 1
    assert pdb.get_state(c, transports.OUTBOX_KEY) != "[]"
    _silence(c, "key", "source:stale")
    c.commit()
    up = Recorder()
    poll(T0 + 300, up)
    assert up.seen == []                         # not sent late
    assert pdb.get_state(c, transports.OUTBOX_KEY) == "[]"   # nor kept
    assert [(r["event"], r["detail"]) for r in c.execute(
        "SELECT event, detail FROM alert_events ORDER BY id")][-1] == ("dropped", "silenced")
    c.close()


# -- PATCHBAY_EXPECT is ignored (#69) -------------------------------------

def test_expect_entries_silence_nothing(clean_env, conn):
    clean_env.setenv("PATCHBAY_EXPECT", "sw1:1/0/2, hyp1")
    settings = load_settings()
    assert settings.expected == {"sw1:1/0/2", "hyp1"}   # kept for the import
    assert silences.all_silences(conn) == []
    link = _item("link-down:hyp1:vmnic0:sw1:1/0/2", "link-down", "link",
                 ports=[("hyp1", "vmnic0"), ("sw1", "1/0/2")])
    down = _item("device:hyp1", "device-down", "device", devices=["hyp1"])
    assert [i["silenced"] for i in _mark(conn, [link, down])] == [None, None]


def test_expect_warns_and_names_the_alerts_page(clean_env):
    assert not [w for w in load_settings().parse_warnings if "PATCHBAY_EXPECT" in w]
    clean_env.setenv("PATCHBAY_EXPECT", "hyp1")
    [w] = [w for w in load_settings().parse_warnings if w.startswith("PATCHBAY_EXPECT:")]
    assert "ignored" in w and "/alerts?tab=silences" in w and "import" in w
    clean_env.setenv("PATCHBAY_EXPECT", " , ")
    assert not [w for w in load_settings().parse_warnings if "PATCHBAY_EXPECT" in w]


def test_an_address_named_device_can_be_silenced_as_a_device(conn):
    # #110: a UniFi device with no name is stored as its MAC, and the
    # Active row offers it as a device scope; that scope must parse
    mac = "02:00:00:00:00:01"
    item = {"key": f"device:{mac}", "category": "device", "devices": [mac]}
    assert ("device", mac) in silences.scopes_for(item)
    for name in (mac, "2001:db8::1"):
        row = silences.parse({"target": f"device:{name}"}, now=T0,
                             created_by="operator", categories={"device"})
        assert (row["kind"], row["scope"]) == ("device", name)
        _silence(conn, "device", name)
    [it] = _mark(conn, [dict(item)])
    assert it["silenced"]["scope"] == mac
    # dev:iface in the device kind is still refused
    with pytest.raises(ValueError, match="bare name"):
        silences.parse({"kind": "device", "scope": "sw1:1/0/2"}, now=T0,
                       created_by="operator", categories=set())


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


def test_expect_leaves_the_item_active_everywhere(clean_env, tmp_path):
    dbp, client = _web(clean_env, tmp_path, PATCHBAY_EXPECT="sw1:1/0/1")
    assert "runs at 10M" in client.get("/").text
    assert "runs at 10M" in client.get("/alerts").text
    assert "runs at 10M" not in client.get("/alerts?tab=silenced").text
    page = client.get("/alerts?tab=silences").text
    assert "sw1:1/0/1" not in page and "import on Ops" not in page

    # a poll raises it: nothing is silenced
    assert client.post("/ops/poll").status_code == 200
    c = sqlite3.connect(dbp)
    assert c.execute("SELECT state FROM alerts").fetchall() != [("silenced",)]
    assert c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
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
    client.cookies.set(auth.SESSION_COOKIE,
                       auth.session_token(load_settings(), "alice@example.com"))
    assert client.post("/alerts/silences",
                       data={"kind": "category", "scope": "link"}).status_code == 303
    c = sqlite3.connect(dbp)
    assert c.execute("SELECT created_by FROM alert_silences").fetchone()[0] == "alice@example.com"
    c.close()


# -- importing PATCHBAY_EXPECT as silences (#69) -------------------------

MAC = "02:00:00:00:00:01"


class _Expect:
    """What import_expect reads of Settings."""

    def __init__(self, *entries):
        self.expected = set(entries)


def _import(conn, *entries, now=T0):
    conn.commit()     # import_expect starts its own transaction
    out = silences.import_expect(conn, _Expect(*entries), now=now, created_by="operator")
    conn.commit()
    return out


def _rows(conn):
    return [(r["kind"], r["scope"], r["until"], r["reason"], r["created_by"])
            for r in conn.execute("SELECT * FROM alert_silences ORDER BY scope")]


def test_import_makes_one_permanent_silence_per_entry_kind(conn):
    entries = ("sw1:1/0/2", "hyp1", MAC, "192.0.2.9", "2001:db8::1")
    out = _import(conn, *entries)
    assert sorted(out["created"]) == sorted(entries)
    assert out["covered"] == out["invalid"] == []
    reason = "imported from PATCHBAY_EXPECT"
    assert sorted(_rows(conn)) == sorted([
        ("port", "sw1:1/0/2", None, reason, "operator"),
        ("device", "hyp1", None, reason, "operator"),
        ("device", MAC, None, reason, "operator"),
        ("device", "192.0.2.9", None, reason, "operator"),
        ("device", "2001:db8::1", None, reason, "operator")])
    # the kind is the one the legacy row had
    assert {e: silences.expect_kind(e) for e in entries} == {r[1]: r[0] for r in _rows(conn)}


def test_import_twice_creates_nothing_and_reports_the_covered(conn):
    _import(conn, "sw1:1/0/2", "hyp1")
    before = _rows(conn)
    out = _import(conn, "sw1:1/0/2", "hyp1")
    assert out["created"] == [] and sorted(out["covered"]) == ["hyp1", "sw1:1/0/2"]
    assert _rows(conn) == before
    # a new entry imports alone
    out = _import(conn, "sw1:1/0/2", "hyp1", "sw2")
    assert (out["created"], len(_rows(conn))) == (["sw2"], 3)


def test_import_skips_what_a_permanent_silence_covers_not_a_timed_one(conn):
    _silence(conn, "device", "hyp1", until=None, reason="by hand")
    _silence(conn, "device", "hyp2", until=T0 + DAY)     # ends: not equivalent
    _silence(conn, "device", "sw1")                       # a device is not its port
    _silence(conn, "port", "sw3", until=None)             # kind differs from `sw3`
    out = _import(conn, "hyp1", "hyp2", "sw1:1/0/2", "sw3")
    assert out["covered"] == ["hyp1"]
    assert sorted(out["created"]) == ["hyp2", "sw1:1/0/2", "sw3"]


def test_import_reports_an_entry_that_is_not_a_silence(conn):
    out = _import(conn, "sw1:", "hyp1", "x" * (silences.SCOPE_MAX + 1))
    assert out["created"] == ["hyp1"]
    assert sorted(e for e, _ in out["invalid"]) == ["sw1:", "x" * (silences.SCOPE_MAX + 1)]
    assert len(_rows(conn)) == 1


def test_expect_pending_counts_down_as_entries_are_imported(clean_env, conn):
    clean_env.setenv("PATCHBAY_EXPECT", "sw1:1/0/2, hyp1")
    settings = load_settings()
    assert silences.expect_pending(conn, settings) == 2
    silences.import_expect(conn, settings, now=T0, created_by="operator")
    conn.commit()
    assert silences.expect_pending(conn, settings) == 0
    assert sorted((r["kind"], r["scope"]) for r in silences.all_silences(conn)) == [
        ("device", "hyp1"), ("port", "sw1:1/0/2")]
    # ending one makes it pending again: only a permanent silence covers
    conn.execute("UPDATE alert_silences SET until = ? WHERE scope = 'hyp1'", (T0 - 1,))
    assert silences.expect_pending(conn, settings) == 1


def _import_post(client, **kw):
    r = client.post("/ops/expect/import", **kw)
    return r, (r.json() if r.status_code != 403 else None)


def test_import_route_silences_what_the_ignored_variable_does_not(clean_env, tmp_path):
    dbp, client = _web(clean_env, tmp_path, PATCHBAY_EXPECT="sw1:1/0/1")

    def silenced():
        page = client.get("/alerts?tab=silenced").text
        return ("runs at 10M" not in client.get("/").text
                and "runs at 10M" not in client.get("/alerts").text
                and "runs at 10M" in page)

    assert not silenced()                     # the variable alone does nothing
    r, body = _import_post(client)
    assert r.status_code == 200
    assert "[ok]   port sw1:1/0/1: imported as a permanent silence" in body["lines"]
    assert "1 imported, 0 already covered, 0 not imported." in body["lines"]
    assert any("env file" in line and "restart" in line for line in body["lines"])
    assert silenced()

    page = client.get("/alerts?tab=silences").text
    assert page.count("port sw1:1/0/1") == 1
    assert "imported from PATCHBAY_EXPECT" in page and "end now" in page

    # the operator removes the variable: nothing changes
    clean_env.delenv("PATCHBAY_EXPECT")
    assert silenced()
    assert client.get("/alerts?tab=silences").text.count("port sw1:1/0/1") == 1
    assert client.post("/ops/poll").status_code == 200
    c = sqlite3.connect(dbp)
    assert c.execute("SELECT state FROM alerts").fetchall() == [("silenced",)]
    c.close()


def test_import_route_is_idempotent(clean_env, tmp_path):
    dbp, client = _web(clean_env, tmp_path, PATCHBAY_EXPECT="sw1:1/0/1,hyp1")
    _import_post(client)
    r, body = _import_post(client)
    assert r.status_code == 200
    assert "[skip] hyp1: a permanent silence already covers it" in body["lines"]
    assert "0 imported, 2 already covered, 0 not imported." in body["lines"]
    c = sqlite3.connect(dbp)
    assert c.execute("SELECT COUNT(*) FROM alert_silences").fetchone()[0] == 2
    c.close()


def test_import_without_the_variable_writes_nothing(clean_env, tmp_path):
    dbp, client = _web(clean_env, tmp_path)
    r, body = _import_post(client)
    assert r.status_code == 200 and "not set" in body["lines"][0]
    c = sqlite3.connect(dbp)
    assert c.execute("SELECT COUNT(*) FROM alert_silences").fetchone()[0] == 0
    c.close()


def test_cross_origin_import_is_rejected(clean_env, tmp_path):
    dbp, client = _web(clean_env, tmp_path, PATCHBAY_EXPECT="hyp1")
    r, _ = _import_post(client, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    c = sqlite3.connect(dbp)
    assert c.execute("SELECT COUNT(*) FROM alert_silences").fetchone()[0] == 0
    c.close()


def test_import_is_created_by_the_oidc_identity_when_there_is_one(clean_env, tmp_path):
    dbp, client = _web(clean_env, tmp_path, PATCHBAY_AUTH="oidc",
                       PATCHBAY_OIDC_CLIENT_ID="cid", PATCHBAY_OIDC_CLIENT_SECRET="cs",
                       PATCHBAY_OIDC_AUTH_URL="https://idp.example/authorize",
                       PATCHBAY_OIDC_TOKEN_URL="https://idp.example/token",
                       PATCHBAY_EXPECT="hyp1")
    assert client.post("/ops/expect/import").status_code == 401
    client.cookies.set(auth.SESSION_COOKIE,
                       auth.session_token(load_settings(), "alice@example.com"))
    assert client.post("/ops/expect/import").status_code == 200
    c = sqlite3.connect(dbp)
    assert c.execute("SELECT created_by FROM alert_silences").fetchall() == [
        ("alice@example.com",)]
    c.close()


def test_ops_notice_shows_only_while_the_variable_is_set(clean_env, tmp_path):
    dbp, client = _web(clean_env, tmp_path)
    page = client.get("/ops").text
    assert "PATCHBAY_EXPECT is ignored" not in page
    assert "data-expect-import" not in page.split("<script>")[0]   # no button
    assert 'id="expectnotice"' in page                             # swap target stays

    clean_env.setenv("PATCHBAY_EXPECT", "sw1:1/0/1,hyp1")
    page = " ".join(client.get("/ops").text.split())
    assert "PATCHBAY_EXPECT is ignored" in page and "/alerts?tab=silences" in page
    assert "import as silences" in page and "2 of 2 entries are not yet a silence" in page
    # the finishing step waits until nothing is pending
    assert "delete the PATCHBAY_EXPECT line" not in page

    _import_post(client)
    page = " ".join(client.get("/ops").text.split())
    assert "All 2 entries are now permanent silences" in page
    assert "delete the PATCHBAY_EXPECT line from your env file and restart" in page

    clean_env.delenv("PATCHBAY_EXPECT")
    assert "PATCHBAY_EXPECT is ignored" not in client.get("/ops").text


def test_ops_notice_tells_a_stored_declaration_to_clear_the_field(clean_env, tmp_path):
    dbp, client = _web(clean_env, tmp_path)
    assert client.post("/ops/config", json={"var": "PATCHBAY_EXPECT",
                                            "value": "hyp1"}).status_code == 200
    assert "PATCHBAY_EXPECT is ignored" in client.get("/ops").text
    _, body = _import_post(client)
    assert "clear the PATCHBAY_EXPECT field" in client.get("/ops").text
    assert any("clear the PATCHBAY_EXPECT field" in line for line in body["lines"])
    assert client.post("/ops/config", json={"var": "PATCHBAY_EXPECT",
                                            "value": ""}).status_code == 200
    assert "PATCHBAY_EXPECT is ignored" not in client.get("/ops").text


def test_ops_shows_the_parse_warning_beside_the_variable(clean_env, tmp_path):
    _, client = _web(clean_env, tmp_path, PATCHBAY_EXPECT="hyp1")
    page = client.get("/ops").text
    assert "PATCHBAY_EXPECT: ignored" in page and "/alerts?tab=silences" in page
    assert page.count("PATCHBAY_EXPECT: ignored") == 1   # beside the field only


def test_ops_lists_an_entry_it_cannot_import_and_withholds_the_finish(clean_env, tmp_path):
    # a port silence cannot name an empty interface
    dbp, client = _web(clean_env, tmp_path, PATCHBAY_EXPECT="sw1:,hyp1")
    page = " ".join(client.get("/ops").text.split())
    assert "1 of 2 entries are not yet a silence" in page
    assert "Cannot import" in page and "sw1: — scope: a port is device:interface" in page
    _import_post(client)
    page = " ".join(client.get("/ops").text.split())
    assert "All 2" not in page and "0 of 2" not in page
    assert "Cannot import" in page and "delete the PATCHBAY_EXPECT line" not in page
    # nothing silences it: only the valid entry became a silence
    c = sqlite3.connect(dbp)
    c.row_factory = sqlite3.Row
    assert [r["scope"] for r in silences.all_silences(c)] == ["hyp1"]
    c.close()

