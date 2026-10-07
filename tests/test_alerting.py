"""The alert engine (ADR-0003 Decision 1, issue #59): lifecycle, `for`
counting, one-shot event rules, the fixed inhibition rule, and history
retention. Items are synthetic; the engine never cares which rule made
them, only their key and rule name."""

from __future__ import annotations

import json
import sqlite3

import pytest

from fastapi.testclient import TestClient

from patchbay import alerting
from patchbay import db as pdb

T0 = 1_800_000_000.0   # a fixed clock, so timestamps are assertable


def _item(key="link:sw1:1/0/1:hyp1:vmnic0", rule="slow-link", category="link",
          severity="warn", **extra):
    return {"key": key, "rule": rule, "category": category, "severity": severity,
            "text": f"{key} text", "href": "/topology", **extra}


def _alerts(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM alerts ORDER BY id")]


def _events(conn):
    return [(r["event"], r["key"], r["ts"]) for r in
            conn.execute("SELECT * FROM alert_events ORDER BY id")]


def test_rules_seeded_from_catalog_without_overwriting_edits(conn):
    alerting.seed_rules(conn)
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM alert_rules")}
    assert set(rows) == set(alerting.RULES)
    assert json.loads(rows["ipam-drift"]["params"]) == {"for": 1}
    assert rows["slow-link"]["route"] == "none"      # informs, never pages
    assert rows["stale-source"]["route"] is None     # the default route

    # a site's edit survives the next poll's seeding
    conn.execute("UPDATE alert_rules SET params = '{\"for\": 3}' WHERE name = 'ipam-drift'")
    alerting.seed_rules(conn)
    assert json.loads(conn.execute(
        "SELECT params FROM alert_rules WHERE name = 'ipam-drift'").fetchone()[0]) == {"for": 3}


def test_raise_hold_clear_and_raise_again(conn):
    it = _item()
    notes = alerting.evaluate(conn, [it], now=T0)
    [a] = _alerts(conn)
    assert (a["state"], a["raised_at"], a["active_at"]) == ("active", T0, T0)
    assert [n.kind for n in notes] == ["raise"]       # for = 1: same cycle

    # still firing: one row, the text refreshed, nothing re-sent
    it2 = {**it, "text": "now slower", "severity": "crit"}
    assert alerting.evaluate(conn, [it2], now=T0 + 300) == []
    [a] = _alerts(conn)
    assert (a["polls"], a["text"], a["severity"]) == (2, "now slower", "crit")
    assert a["raised_at"] == T0

    notes = alerting.evaluate(conn, [], now=T0 + 600)
    [a] = _alerts(conn)
    assert (a["state"], a["cleared_at"]) == ("cleared", T0 + 600)
    assert [n.kind for n in notes] == ["clear"]

    # the same condition returning is a new alert; the old row is history
    alerting.evaluate(conn, [it], now=T0 + 900)
    rows = _alerts(conn)
    assert [r["state"] for r in rows] == ["cleared", "active"]
    assert rows[1]["raised_at"] == T0 + 900
    assert _events(conn) == [
        ("raised", it["key"], T0), ("active", it["key"], T0),
        ("cleared", it["key"], T0 + 600),
        ("raised", it["key"], T0 + 900), ("active", it["key"], T0 + 900)]


def test_for_counts_polls_before_active(conn):
    alerting.seed_rules(conn)
    conn.execute("UPDATE alert_rules SET params = '{\"for\": 3}' WHERE name = 'slow-link'")
    it = _item()
    assert alerting.evaluate(conn, [it], now=T0) == []
    assert _alerts(conn)[0]["state"] == "pending"
    assert alerting.evaluate(conn, [it], now=T0 + 300) == []
    assert _alerts(conn)[0]["state"] == "pending"
    notes = alerting.evaluate(conn, [it], now=T0 + 600)
    [a] = _alerts(conn)
    assert (a["state"], a["active_at"], a["raised_at"]) == ("active", T0 + 600, T0)
    assert [(n.kind, n.raised_at) for n in notes] == [("raise", T0)]


def test_pending_that_clears_is_history_not_news(conn):
    alerting.seed_rules(conn)
    conn.execute("UPDATE alert_rules SET params = '{\"for\": 2}' WHERE name = 'slow-link'")
    alerting.evaluate(conn, [_item()], now=T0)
    assert alerting.evaluate(conn, [], now=T0 + 300) == []   # never announced
    ev = conn.execute("SELECT event, detail FROM alert_events ORDER BY id").fetchall()
    assert [tuple(r) for r in ev] == [("raised", None), ("cleared", "never active")]


def test_remind_renotifies_an_active_alert(conn):
    alerting.seed_rules(conn)
    conn.execute("UPDATE alert_rules SET params = '{\"remind\": 3600}' "
                 "WHERE name = 'slow-link'")
    it = _item()
    alerting.evaluate(conn, [it], now=T0)
    assert alerting.evaluate(conn, [it], now=T0 + 1800) == []
    assert [n.kind for n in alerting.evaluate(conn, [it], now=T0 + 3600)] == ["remind"]
    assert alerting.evaluate(conn, [it], now=T0 + 3900) == []   # clock restarted


def test_event_rule_notifies_and_clears_in_one_cycle(conn):
    catalog = {**alerting.RULES,
               "config-changed": alerting.Rule(category="config", event=True)}
    it = _item(key="config:fw1:rev:7", rule="config-changed", category="config",
               severity="info")
    notes = alerting.evaluate(conn, [it], now=T0, catalog=catalog)
    assert [n.kind for n in notes] == ["event"]
    [a] = _alerts(conn)
    assert (a["state"], a["raised_at"], a["cleared_at"]) == ("cleared", T0, T0)
    assert [e[0] for e in _events(conn)] == ["raised", "active", "cleared"]
    # the same occurrence seen again is not a second event
    assert alerting.evaluate(conn, [it], now=T0 + 300, catalog=catalog) == []
    assert len(_alerts(conn)) == 1


def test_disabled_rule_raises_nothing(conn):
    alerting.seed_rules(conn)
    conn.execute("UPDATE alert_rules SET enabled = 0 WHERE name = 'slow-link'")
    assert alerting.evaluate(conn, [_item()], now=T0) == []
    assert _alerts(conn) == []


def test_down_device_inhibits_link_down_at_its_ports(conn):
    # there is no link-down rule yet (#60); the inhibition is a fixed rule
    # keyed on the rule name, so a synthetic item exercises it
    pdb.upsert_device(conn, name="sw1", source="librenms", status="up")
    pdb.upsert_device(conn, name="ap1", source="unifi", status="down")
    now = pdb.now()
    at_down = _item(key="link-down:sw1:1/0/5", rule="link-down",
                    ports=[("sw1", "1/0/5"), ("ap1", "eth0")])
    at_up = _item(key="link-down:sw1:1/0/6", rule="link-down",
                  ports=[("sw1", "1/0/6")])
    slow = _item(key="link:sw1:1/0/7:ap1:eth0", ports=[("ap1", "eth0")])
    alerting.evaluate(conn, [at_down, at_up, slow], now=now)
    # only link-down items are inhibited, and only at a down device's ports
    assert {a["key"] for a in _alerts(conn)} == {at_up["key"], slow["key"]}

    # an alert already open when its device goes down is held, not cleared:
    # the device returning with the port still down must not re-raise it
    conn.execute("UPDATE devices SET status = 'down' WHERE name = 'sw1'")
    notes = alerting.evaluate(conn, [at_down, at_up, slow], now=now + 300)
    assert notes == []
    assert {a["key"]: a["state"] for a in _alerts(conn)}[at_up["key"]] == "active"


def test_stale_device_counts_as_down_for_inhibition(conn):
    pdb.upsert_device(conn, name="ap1", source="unifi", status="up")
    conn.execute("UPDATE devices SET last_seen = ?",
                 (pdb.now() - 3 * 3600,))
    assert alerting.down_devices(conn, pdb.now()) == {"ap1"}


def test_legacy_first_seen_carries_over_once(conn):
    # an upgrade from the app_state map keeps each item's age, then the map
    # is gone so a later raise of the same key is new
    it = _item()
    pdb.set_state(conn, "alert_first_seen", json.dumps({it["key"]: T0 - 86400}))
    alerting.evaluate(conn, [it], now=T0)
    assert _alerts(conn)[0]["raised_at"] == T0 - 86400
    assert pdb.get_state(conn, "alert_first_seen") is None


def test_dispatch_drops_route_none_and_reaches_the_dispatcher(conn):
    sent = []

    class Recorder:
        def send(self, notes):
            sent.extend(notes)

    notes = alerting.evaluate(
        conn, [_item(), _item(key="source:stale", rule="stale-source",
                              category="source")], now=T0)
    alerting.dispatch(notes, Recorder())
    assert [n.key for n in sent] == ["source:stale"]   # slow-link routes nowhere
    alerting.dispatch(notes)                           # the no-op default


def test_history_retention_by_age_and_count(conn, monkeypatch):
    now = pdb.now()
    old = now - 91 * 86400
    rows = [(old, "raised", "a"), (old, "cleared", "a")] + [
        (now - i, "raised", f"k{i}") for i in range(5)]
    conn.executemany(
        "INSERT INTO alert_events (ts, event, key, rule, category, severity) "
        "VALUES (?, ?, ?, 'r', 'link', 'warn')", rows)
    conn.execute("INSERT INTO alerts (key, rule, category, severity, state, "
                 "raised_at, cleared_at) VALUES ('a', 'r', 'link', 'warn', "
                 "'cleared', ?, ?)", (old, old))
    conn.execute("INSERT INTO alerts (key, rule, category, severity, state, "
                 "raised_at) VALUES ('b', 'r', 'link', 'warn', 'active', ?)", (old,))
    monkeypatch.setattr(alerting, "EVENT_KEEP_MAX", 3)

    from patchbay.normalize import normalize
    normalize(conn)      # housekeeping runs the prune, beside raw_payloads

    keys = [r["key"] for r in conn.execute(
        "SELECT key FROM alert_events ORDER BY ts DESC")]
    assert keys == ["k0", "k1", "k2"]                  # newest 3, none aged
    # a cleared alert ages out with its history; an open one never does
    assert [r["key"] for r in conn.execute("SELECT key FROM alerts")] == ["b"]


def _web_db(tmp_path, speed):
    from tests.test_web import seed

    dbp = str(tmp_path / "test.db")
    seed(dbp)
    c = sqlite3.connect(dbp)
    c.execute("UPDATE interfaces SET speed_bps = ? WHERE name IN ('1/0/1', 'vmnic0')",
              (speed,))
    c.commit(); c.close()
    return dbp


def test_poll_raises_then_clears_into_history(clean_env, tmp_path):
    # the issue's done-when: a slow link that appears and disappears shows
    # as raised then cleared in History, and the Overview is unchanged
    import patchbay.web as web

    dbp = _web_db(tmp_path, 10_000_000)
    client = TestClient(web.app)
    assert "No alert history yet" in client.get("/alerts?tab=history").text

    assert client.post("/ops/poll").status_code == 200
    page = client.get("/alerts?tab=history").text
    assert 'class="ev-raised">raised' in page and "runs at 10M" in page
    assert "runs at 10M" in client.get("/").text        # Overview as before
    assert "runs at 10M" in client.get("/alerts").text  # Active tab

    c = sqlite3.connect(dbp)
    c.execute("UPDATE interfaces SET speed_bps = 10000000000")
    c.commit(); c.close()
    assert client.post("/ops/poll").status_code == 200
    page = client.get("/alerts?tab=history").text
    assert page.index('class="ev-cleared">cleared') < page.index('class="ev-raised">raised')
    assert "runs at 10M" not in client.get("/alerts").text

    c = sqlite3.connect(dbp)
    c.row_factory = sqlite3.Row
    [a] = c.execute("SELECT * FROM alerts").fetchall()
    assert a["state"] == "cleared" and a["cleared_at"] >= a["raised_at"]
    c.close()

    # history filters are URL state on the history tab
    assert "runs at 10M" in client.get("/alerts?tab=history&category=link").text
    assert "No history matches" in client.get("/alerts?tab=history&category=ipam").text


def test_duplicate_keys_in_one_cycle_raise_once(conn):
    # two links rows differing only by source give one slow-link key; the
    # second must not hit the open-alert index and abort the whole cycle
    it = _item(key="source:stale", rule="stale-source", category="source")
    notes = alerting.evaluate(conn, [it, dict(it)], now=T0)
    assert len(_alerts(conn)) == 1
    assert [e[0] for e in _events(conn)].count("raised") == 1
    assert [n.kind for n in notes] == ["raise"]


def test_failed_evaluate_rolls_back_only_alerting(conn, monkeypatch):
    # run() owns a savepoint: a bug in the engine must discard its own
    # half-written diff and leave the poll's earlier writes for the commit
    pdb.upsert_device(conn, name="sw1", source="librenms", status="up")

    def boom(c, items, **kw):
        c.execute("INSERT INTO alert_events (ts, event, key, rule, category, "
                  "severity) VALUES (1, 'raised', 'k', 'r', 'link', 'warn')")
        raise RuntimeError("engine bug")

    monkeypatch.setattr(alerting, "attention_items", lambda c, s: ([], []))
    monkeypatch.setattr(alerting, "evaluate", boom)

    with pytest.raises(RuntimeError):
        alerting.run(conn, None)
    assert conn.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM devices").fetchone()[0] == "sw1"
