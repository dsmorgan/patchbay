"""Known cables (issue #79): a pulled cable's link-down alert holds after
its LLDP or UniFi row expires, clears when the port comes back, can be
retired by the operator, and ages out on its own. The map still forgets a
cable nobody reports. Every model here is synthetic."""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from patchbay import alerting, attention, known_cables, normalize
from patchbay import db as pdb
from patchbay.config import load_settings

T0 = 1_800_000_000.0
HOUR = 3600.0
DAY = 86400.0
KEY = "link-down:sw1:1/0/2:sw2:0/1"


class Clock:
    t = T0


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(pdb, "now", lambda: c.t)
    return c


@pytest.fixture()
def settings(clean_env):
    return load_settings()


def _poll(conn, settings, clock, *, lldp=True, sw1="up", sw2="up",
          names=("sw1", "sw2"), source="lldp", declared=None):
    """One poll cycle: both switches answer, their ports report `sw1` and
    `sw2` oper status, LLDP reports the cable only while `lldp`."""
    a, b = names
    for name, iface, oper in ((a, "1/0/2", sw1), (b, "0/1", sw2)):
        did = pdb.upsert_device(conn, name=name, source="librenms", role="switch",
                                status="up")
        pdb.upsert_interface(conn, device_id=did, name=iface, oper_status=oper,
                             admin_status="up")
    if lldp:
        pdb.upsert_link(conn, a_device=a, a_interface="1/0/2", b_device=b,
                        b_interface="0/1", source=source)
    normalize.normalize(conn, declared_links=declared)
    items = attention.attention_items(conn, settings, now=clock.t)[0]
    return alerting.evaluate(conn, items, now=clock.t)


def _known(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM known_cables")]


def _links(conn):
    return conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]


def _open(conn):
    return {r["key"]: r["state"] for r in conn.execute(
        "SELECT key, state FROM alerts WHERE state != 'cleared'")}


def _pull(conn, settings, clock):
    """The cable is pulled: both ports go down, LLDP falls silent, and the
    poller keeps polling past the evidence TTL."""
    _poll(conn, settings, clock)
    clock.t += 300
    notes = _poll(conn, settings, clock, lldp=False, sw1="down", sw2="down")
    assert [(n.kind, n.key) for n in notes] == [("raise", KEY)]
    clock.t += normalize.EVIDENCE_TTL + 300
    return _poll(conn, settings, clock, lldp=False, sw1="down", sw2="down")


def test_pulled_cable_alert_holds_past_the_evidence_ttl(conn, settings, clock):
    notes = _pull(conn, settings, clock)
    # the map forgot it, as before #79...
    assert _links(conn) == 0
    # ...and the alert did not: no clear, still active
    assert notes == []
    assert _open(conn) == {KEY: "active"}
    [k] = _known(conn)
    assert (k["a_device"], k["b_device"], k["retired_at"]) == ("sw1", "sw2", None)
    assert k["last_up"] == T0
    [it] = [i for i in attention.attention_items(conn, settings, now=clock.t)[0]
            if i["rule"] == "link-down"]
    # still matchable by silences and held by device-down inhibition
    assert it["ports"] == [("sw1", "1/0/2"), ("sw2", "0/1")]
    assert it["retirable"] is True
    assert alerting.inhibited(it, {"sw2"})


def test_port_back_up_clears_and_refreshes_the_record(conn, settings, clock):
    _pull(conn, settings, clock)
    clock.t += 300
    notes = _poll(conn, settings, clock)          # plugged back in
    assert [(n.kind, n.key) for n in notes] == [("clear", KEY)]
    [k] = _known(conn)
    assert k["last_up"] == clock.t
    assert _links(conn) == 1


def test_port_up_without_a_report_still_clears(conn, settings, clock):
    # something else was plugged in that speaks no LLDP: the port is up, so
    # nothing is down; the stale record ages out on its own
    _pull(conn, settings, clock)
    clock.t += 300
    notes = _poll(conn, settings, clock, lldp=False)
    assert [(n.kind, n.key) for n in notes] == [("clear", KEY)]
    assert len(_known(conn)) == 1


def test_age_out_after_retain_days(conn, settings, clock):
    _pull(conn, settings, clock)
    clock.t = T0 + 29 * DAY
    _poll(conn, settings, clock, lldp=False, sw1="down", sw2="down")
    assert _open(conn) == {KEY: "active"}
    clock.t = T0 + 30 * DAY + 60
    notes = _poll(conn, settings, clock, lldp=False, sw1="down", sw2="down")
    assert _known(conn) == []
    assert [(n.kind, n.key) for n in notes] == [("clear", KEY)]


def test_retain_days_is_the_rules_parameter(conn, settings, clock):
    _pull(conn, settings, clock)
    conn.execute("UPDATE alert_rules SET params = ? WHERE name = 'link-down'",
                 (json.dumps({"for": 1, "retain_days": 2}),))
    clock.t = T0 + 2 * DAY + 60
    _poll(conn, settings, clock, lldp=False, sw1="down", sw2="down")
    assert _known(conn) == []


def test_a_reported_cable_never_ages_out(conn, settings, clock):
    # a declaration down for over a month is still stated: it stays recorded
    declared = [("sw1", "1/0/2", "sw2", "0/1")]
    _poll(conn, settings, clock, lldp=False, sw1="down", declared=declared)
    clock.t = T0 + 40 * DAY
    _poll(conn, settings, clock, lldp=False, sw1="down", declared=declared)
    [k] = _known(conn)
    assert k["last_up"] is None and k["first_seen"] == T0
    assert _open(conn) == {KEY: "active"}


def test_retire_clears_quietly_and_stops_alerting(clean_env, tmp_path, clock):
    dbp = str(tmp_path / "test.db")
    with pdb.connect(dbp) as c:
        pdb.init(c)
        _pull(c, load_settings(), clock)
    import patchbay.web as web

    client = TestClient(web.app, follow_redirects=False)
    page = client.get("/alerts").text
    assert "retire cable" in page
    assert 'name="b_interface" value="0/1"' in page
    r = client.post("/alerts/cables/retire", data={
        "a_device": "sw2", "a_interface": "0/1",     # either orientation
        "b_device": "sw1", "b_interface": "1/0/2"})
    assert (r.status_code, r.headers["location"]) == (303, "/alerts")
    assert "sw1 1/0/2 ↔ sw2 0/1" not in client.get("/alerts").text

    c = sqlite3.connect(dbp)
    c.row_factory = sqlite3.Row
    assert _open(c) == {}
    last = c.execute("SELECT event, detail FROM alert_events ORDER BY id DESC").fetchone()
    assert (last["event"], last["detail"]) == ("cleared", "cable retired")
    [k] = _known(c)
    assert k["retired_at"] == clock.t
    # later polls with the port still down raise nothing and send nothing
    clock.t += 300
    assert _poll(c, load_settings(), clock, lldp=False, sw1="down", sw2="down") == []
    assert _open(c) == {}
    c.commit()
    c.close()


def test_retired_cable_is_lifted_when_it_comes_back(conn, settings, clock):
    _pull(conn, settings, clock)
    assert known_cables.retire(conn, known_cables.ends_of("sw1", "1/0/2", "sw2", "0/1"),
                               clock.t)
    alerting.clear_settled(conn, KEY, "cable retired", clock.t)
    clock.t += 300
    # LLDP still reports it with the port down: the retirement holds
    pdb.upsert_link(conn, a_device="sw1", a_interface="1/0/2", b_device="sw2",
                    b_interface="0/1", source="lldp")
    assert _poll(conn, settings, clock, sw1="down", sw2="down") == []
    clock.t += 300
    _poll(conn, settings, clock)                     # plugged back in: live again
    assert _known(conn)[0]["retired_at"] is None
    clock.t += 300
    notes = _poll(conn, settings, clock, sw1="down")
    assert [(n.kind, n.key) for n in notes] == [("raise", KEY)]


@pytest.mark.parametrize("form", [
    {},
    {"a_device": "sw1", "a_interface": "1/0/2", "b_device": "sw2"},
    {"a_device": "sw1", "a_interface": "1/0/2", "b_device": "sw2", "b_interface": ""},
    {"a_device": "sw1", "a_interface": "a\nb", "b_device": "sw2", "b_interface": "0/1"},
    {"a_device": "x" * 301, "a_interface": "1", "b_device": "sw2", "b_interface": "0/1"},
])
def test_bad_retire_is_400_before_any_write(clean_env, tmp_path, form):
    import patchbay.web as web

    with pdb.connect(str(tmp_path / "test.db")) as c:
        pdb.init(c)

    client = TestClient(web.app, follow_redirects=False)
    assert client.post("/alerts/cables/retire", data=form).status_code == 400
    c = sqlite3.connect(str(tmp_path / "test.db"))
    assert c.execute("SELECT COUNT(*) FROM known_cables").fetchone()[0] == 0
    c.close()


def test_retire_refuses_declared_and_unknown_cables(clean_env, tmp_path):
    import patchbay.web as web

    dbp = str(tmp_path / "test.db")
    with pdb.connect(dbp) as c:
        pdb.init(c)
        pdb.upsert_link(c, a_device="sw1", a_interface="1/0/2", b_device="sw2",
                        b_interface="0/1", source="declared")
    client = TestClient(web.app, follow_redirects=False)
    form = {"a_device": "sw1", "a_interface": "1/0/2",
            "b_device": "sw2", "b_interface": "0/1"}
    r = client.post("/alerts/cables/retire", data=form)
    assert r.status_code == 400 and "PATCHBAY_LINKS" in r.text
    assert client.post("/alerts/cables/retire",
                       data={**form, "b_interface": "0/9"}).status_code == 404
    c = sqlite3.connect(dbp)
    assert c.execute("SELECT COUNT(*) FROM known_cables").fetchone()[0] == 0
    c.close()


def test_declared_link_down_offers_no_retire(conn, settings):
    for name, iface in (("sw1", "1/0/2"), ("sw2", "0/1")):
        did = pdb.upsert_device(conn, name=name, source="librenms", role="switch",
                                status="up")
        pdb.upsert_interface(conn, device_id=did, name=iface, oper_status="down")
    pdb.upsert_link(conn, a_device="sw1", a_interface="1/0/2", b_device="sw2",
                    b_interface="0/1", source="declared")
    [it] = [i for i in attention.attention_items(conn, settings)[0]
            if i["rule"] == "link-down"]
    assert it["retirable"] is False


def test_retire_is_behind_the_auth_gate(clean_env, tmp_path):
    from patchbay import auth

    clean_env.setenv("PATCHBAY_AUTH", "password")
    clean_env.setenv("PATCHBAY_PASSWORD_HASH", auth.hash_password("pw"))
    import patchbay.web as web

    client = TestClient(web.app, follow_redirects=False)
    form = {"a_device": "sw1", "a_interface": "1/0/2",
            "b_device": "sw2", "b_interface": "0/1"}
    assert client.post("/alerts/cables/retire", data=form).status_code == 401


def test_cross_origin_retire_is_rejected(clean_env, tmp_path):
    import patchbay.web as web

    client = TestClient(web.app, follow_redirects=False)
    form = {"a_device": "sw1", "a_interface": "1/0/2",
            "b_device": "sw2", "b_interface": "0/1"}
    assert client.post("/alerts/cables/retire", data=form,
                       headers={"origin": "https://evil.example"}).status_code == 403


def test_rename_follows_the_record(conn, settings, clock):
    # the far switch is renamed after its link row expired: nothing in
    # links carries the rename, so the record has to follow on its own
    _pull(conn, settings, clock)
    conn.execute("INSERT INTO aliases (alias, canonical) VALUES ('sw2', 'aa-sw2')")
    conn.execute("UPDATE devices SET name = 'aa-sw2' WHERE name = 'sw2'")
    normalize.normalize(conn)
    [k] = _known(conn)
    # renamed and re-sorted, like a links row
    assert (k["a_device"], k["a_interface"], k["b_device"], k["b_interface"]) == \
        ("aa-sw2", "0/1", "sw1", "1/0/2")
    assert (k["first_seen"], k["last_up"]) == (T0, T0)
    [it] = [i for i in attention.attention_items(conn, settings, now=clock.t)[0]
            if i["rule"] == "link-down"]
    assert it["key"] == "link-down:aa-sw2:0/1:sw1:1/0/2"


def test_rename_folds_into_a_twin(conn, settings, clock):
    ends = known_cables.ends_of("sw1", "1/0/2", "sw2", "0/1")
    conn.execute("INSERT INTO known_cables VALUES ('sw1', '1/0/2', 'sw2', '0/1', ?, ?, NULL)",
                 (T0, T0 + 10))
    conn.execute("INSERT INTO known_cables VALUES ('sw1', '1/0/2', 'sw2.lab', '0/1', ?, ?, ?)",
                 (T0 - 50, T0, T0))
    known_cables.rename(conn, lambda n: {"sw2.lab": "sw2"}.get(n, n))
    [k] = _known(conn)
    assert known_cables.ends_of(k["a_device"], k["a_interface"],
                                k["b_device"], k["b_interface"]) == ends
    # oldest sighting, newest up time, and live: only one half was retired
    assert (k["first_seen"], k["last_up"], k["retired_at"]) == (T0 - 50, T0 + 10, None)


def test_a_moved_cable_supersedes_the_old_record(conn, settings, clock):
    _poll(conn, settings, clock)
    # the switch port now leads to sw3: one cable per port
    did = pdb.upsert_device(conn, name="sw3", source="librenms", role="switch",
                            status="up")
    pdb.upsert_interface(conn, device_id=did, name="0/4", oper_status="up")
    conn.execute("DELETE FROM links")
    pdb.upsert_link(conn, a_device="sw1", a_interface="1/0/2", b_device="sw3",
                    b_interface="0/4", source="lldp")
    clock.t += 300
    _poll(conn, settings, clock, lldp=False, sw2="down")
    assert [(k["b_device"], k["b_interface"]) for k in _known(conn)] == [("sw3", "0/4")]
    assert _open(conn) == {}


def test_retire_sends_a_clear_only_if_the_raise_was_sent(conn, settings, clock):
    _pull(conn, settings, clock)                  # announced: active, notified
    [n] = alerting.clear_settled(conn, KEY, "cable retired", clock.t)
    assert (n.kind, n.key, n.rule) == ("clear", KEY, "link-down")
    ev = conn.execute("SELECT event, detail FROM alert_events ORDER BY id DESC").fetchone()
    assert (ev["event"], ev["detail"]) == ("cleared", "cable retired")
    assert alerting.clear_settled(conn, KEY, "cable retired", clock.t) == []


def test_retire_of_an_unannounced_alert_is_silent(conn, settings, clock):
    _poll(conn, settings, clock)
    conn.execute("UPDATE alert_rules SET params = ? WHERE name = 'link-down'",
                 (json.dumps({"for": 3}),))
    clock.t += 300
    _poll(conn, settings, clock, lldp=False, sw1="down", sw2="down")
    assert _open(conn) == {KEY: "pending"}
    assert alerting.clear_settled(conn, KEY, "cable retired", clock.t) == []
    ev = conn.execute("SELECT event, detail FROM alert_events ORDER BY id DESC").fetchone()
    assert (ev["event"], ev["detail"]) == ("cleared", "cable retired")
    assert _open(conn) == {}


def test_retire_route_dispatches_the_clear(clean_env, tmp_path, clock, monkeypatch):
    sent = []

    class Rec:
        def __init__(self, *a, **k):
            pass

        def send(self, notes):
            sent.extend(notes)

    from patchbay import transports
    monkeypatch.setattr(transports, "WebhookDispatcher", Rec)
    with pdb.connect(str(tmp_path / "test.db")) as c:
        pdb.init(c)
        _pull(c, load_settings(), clock)
    import patchbay.web as web

    client = TestClient(web.app, follow_redirects=False)
    assert client.post("/alerts/cables/retire", data={
        "a_device": "sw1", "a_interface": "1/0/2",
        "b_device": "sw2", "b_interface": "0/1"}).status_code == 303
    assert [(n.kind, n.key) for n in sent] == [("clear", KEY)]


def test_undeclaring_a_down_cable_clears_its_alert(conn, settings, clock):
    declared = [("sw1", "1/0/2", "sw2", "0/1")]
    _poll(conn, settings, clock, lldp=False, declared=declared)
    clock.t += 300
    notes = _poll(conn, settings, clock, lldp=False, sw1="down", sw2="down",
                  declared=declared)
    assert [(n.kind, n.key) for n in notes] == [("raise", KEY)]
    clock.t += 300
    notes = _poll(conn, settings, clock, lldp=False, sw1="down", sw2="down", declared=[])
    assert [(n.kind, n.key) for n in notes] == [("clear", KEY)]
    assert _known(conn) == []


def test_undeclaring_keeps_a_cable_discovery_still_states(conn, settings, clock):
    declared = [("sw1", "1/0/2", "sw2", "0/1")]
    _poll(conn, settings, clock, lldp=False, declared=declared)
    clock.t += 300
    # LLDP reports the same cable; the declaration goes, the record stays
    _poll(conn, settings, clock, sw1="down", declared=[])
    assert len(_known(conn)) == 1
    assert _open(conn) == {KEY: "active"}


def test_retire_dispatches_off_the_event_loop(clean_env, tmp_path, clock, monkeypatch):
    import threading

    from patchbay import transports

    threads = []

    class Rec:
        def __init__(self, *a, **k):
            pass

        def send(self, notes):
            threads.append(threading.current_thread())

    monkeypatch.setattr(transports, "WebhookDispatcher", Rec)
    with pdb.connect(str(tmp_path / "test.db")) as c:
        pdb.init(c)
        _pull(c, load_settings(), clock)
    import patchbay.web as web

    seen = {}
    real = web.alerting.dispatch

    def spy(notes, dispatcher=None):
        import asyncio
        try:
            asyncio.get_running_loop()
            seen["loop"] = True
        except RuntimeError:
            seen["loop"] = False
        return real(notes, dispatcher)

    monkeypatch.setattr(web.alerting, "dispatch", spy)
    client = TestClient(web.app, follow_redirects=False)
    assert client.post("/alerts/cables/retire", data={
        "a_device": "sw1", "a_interface": "1/0/2",
        "b_device": "sw2", "b_interface": "0/1"}).status_code == 303
    # dispatch ran in a worker thread, where no event loop is running
    assert seen == {"loop": False} and len(threads) == 1
