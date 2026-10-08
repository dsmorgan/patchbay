"""Port-counter canaries (issue #64, ADR-0003 Decision 5): the baseline
math, the floor, the multiplier, warm-up, hysteresis held through the open
alert, `for` 2 through the engine, inhibition by a down device, and the
rate_history migration. Every sample here is synthetic."""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from patchbay import alerting, attention
from patchbay import db as pdb
from patchbay.config import load_settings

T0 = 1_800_000_000.0
H = 3600.0


@pytest.fixture()
def clock(monkeypatch):
    """The rule reads db.now(); pin it so samples are placed exactly."""
    t = {"now": T0}
    monkeypatch.setattr(pdb, "now", lambda: t["now"])
    return t


@pytest.fixture()
def settings(clean_env):
    return load_settings()


def _sample(conn, ts, *, dev="sw1", iface="1/0/5", **counters):
    cols = ", ".join(["device", "interface", "ts", *counters])
    conn.execute(f"INSERT INTO rate_history ({cols}) VALUES "
                 f"({', '.join('?' * (3 + len(counters)))})",
                 (dev, iface, ts, *counters.values()))


def _history(conn, value, *, hours=48, step=300, dev="sw1", iface="1/0/5",
             counter="out_discards"):
    """A steady baseline from `hours` ago up to two hours ago."""
    ts = T0 - hours * H
    while ts < T0 - 2 * H:
        _sample(conn, ts, dev=dev, iface=iface, **{counter: value})
        ts += step


def _canaries(conn, settings):
    return [i for i in attention.attention_items(conn, settings)[0]
            if i["rule"] == "port-canary"]


# --- baseline math -----------------------------------------------------------

def test_p95_is_a_nearest_rank_sample():
    assert attention._p95(list(range(1, 101))) == 95
    assert attention._p95([7]) == 7
    assert attention._p95([0] * 19 + [500]) == 0     # one burst in twenty


def test_baseline_excludes_the_last_hour_and_the_week_before(conn):
    for i in range(100):                      # 1..100 spread over the past day
        _sample(conn, T0 - 2 * H - i * 600, out_discards=float(i + 1))
    _sample(conn, T0 - 8 * 86400, out_discards=1e6)   # outside the week
    for m in (5, 20, 50):                     # the flood itself: last hour
        _sample(conn, T0 - m * 60, out_discards=1e6)
    assert attention.canary_baseline(conn, "sw1", "1/0/5", "out_discards",
                                     T0, 24 * H) == 95
    # each counter is its own series
    assert attention.canary_baseline(conn, "sw1", "1/0/5", "in_errors",
                                     T0, 24 * H) is None


def test_baseline_is_none_while_warming_up(conn):
    _history(conn, 3.0, hours=20)
    assert attention.canary_baseline(conn, "sw1", "1/0/5", "out_discards",
                                     T0, 24 * H) is None
    assert attention.canary_baseline(conn, "sw1", "1/0/5", "out_discards",
                                     T0, 12 * H) == 3.0


# --- the rule ----------------------------------------------------------------

def test_floor_keeps_a_quiet_port_quiet(conn, settings, clock):
    _history(conn, 0.0)
    _sample(conn, T0 - 60, out_discards=40.0)     # 0 -> 40: under the floor
    assert _canaries(conn, settings) == []
    _sample(conn, T0 - 30, out_discards=60.0)     # newest sample wins
    [it] = _canaries(conn, settings)
    assert it["key"] == "port:canary:sw1:1/0/5:out_discards"
    assert (it["category"], it["severity"]) == ("port", "warn")
    assert it["text"] == "sw1 1/0/5 out discards at 60/s (baseline 0/s)"
    assert it["href"] == "/device/sw1#port-1/0/5"
    assert it["ports"] == [("sw1", "1/0/5")]


def test_multiplier_against_the_ports_own_baseline(conn, settings, clock):
    _history(conn, 20.0)                          # a trunk that always discards
    _sample(conn, T0 - 60, out_discards=1900.0)   # 95x: its normal chatter
    assert _canaries(conn, settings) == []
    _sample(conn, T0 - 30, out_discards=4200.0)   # 210x
    [it] = _canaries(conn, settings)
    assert it["text"] == "sw1 1/0/5 out discards at 4,200/s (baseline 20/s)"


def test_warm_up_judges_against_the_floor_alone(conn, settings, clock):
    _history(conn, 1.0, hours=10)                 # 10 h of history: warming up
    _sample(conn, T0 - 60, in_errors=0.0, out_discards=60.0)
    [it] = _canaries(conn, settings)              # 60x its baseline, above floor
    assert it["text"] == ("sw1 1/0/5 out discards at 60/s "
                          "(no baseline yet, floor 50/s)")


def test_hysteresis_holds_until_below_clear_times_baseline(conn, settings, clock):
    _history(conn, 10.0)
    _sample(conn, T0 - 60, out_discards=200.0)    # 20x: not enough to raise
    assert _canaries(conn, settings) == []
    conn.execute("INSERT INTO alerts (key, rule, category, severity, state, "
                 "polls, raised_at) VALUES "
                 "('port:canary:sw1:1/0/5:out_discards', 'port-canary', 'port', "
                 "'warn', 'active', 3, ?)", (T0 - H,))
    assert len(_canaries(conn, settings)) == 1    # ...but enough to hold
    _sample(conn, T0 - 30, out_discards=90.0)     # under 10x: clears
    assert _canaries(conn, settings) == []


def test_hysteresis_on_a_zero_baseline_port(conn, settings, clock):
    """A quiet port's effective baseline is floor/multiplier, so it raises
    at the floor and holds down to clear/multiplier of it, not to zero."""
    _history(conn, 0.0)
    conn.execute("INSERT INTO alerts (key, rule, category, severity, state, "
                 "polls, raised_at) VALUES "
                 "('port:canary:sw1:1/0/5:out_discards', 'port-canary', 'port', "
                 "'warn', 'active', 3, ?)", (T0 - H,))
    _sample(conn, T0 - 60, out_discards=6.0)      # 50 / 100 * 10 = 5
    assert len(_canaries(conn, settings)) == 1
    _sample(conn, T0 - 30, out_discards=4.0)
    assert _canaries(conn, settings) == []


def test_params_and_expected(conn, clean_env, clock):
    _history(conn, 0.0)
    _sample(conn, T0 - 60, in_errors=60.0)
    alerting.seed_rules(conn)
    conn.execute("UPDATE alert_rules SET params = ? WHERE name = 'port-canary'",
                 (json.dumps({"for": 2, "floor": 100}),))
    assert _canaries(conn, load_settings()) == []
    clean_env.setenv("PATCHBAY_EXPECT", "sw1:1/0/5")
    conn.execute("UPDATE alert_rules SET params = '{}' WHERE name = 'port-canary'")
    assert _canaries(conn, load_settings()) == []


def test_stale_sample_says_nothing(conn, settings, clock):
    _history(conn, 0.0)
    _sample(conn, T0 - 30 * 60, out_discards=5000.0)   # the poller stopped
    assert _canaries(conn, settings) == []


# --- through the engine ------------------------------------------------------

def _poll(conn, settings, clock, rate, *, at):
    clock["now"] = at
    _sample(conn, at - 10, out_discards=rate)
    return alerting.evaluate(conn, attention.attention_items(conn, settings)[0],
                             now=at)


def _state(conn):
    row = conn.execute("SELECT state FROM alerts WHERE rule = 'port-canary' "
                       "ORDER BY id DESC").fetchone()
    return row["state"] if row else None


def test_a_flood_raises_after_two_polls_and_clears_with_hysteresis(
        conn, settings, clock):
    """The issue's done-when: a discard rate that jumps from a quiet baseline
    to thousands per second raises a warn after two polls, naming the
    baseline."""
    _history(conn, 2.0)
    assert [n.kind for n in _poll(conn, settings, clock, 3000.0, at=T0)] == []
    assert _state(conn) == "pending"
    [note] = _poll(conn, settings, clock, 3200.0, at=T0 + 300)
    assert (note.kind, note.severity) == ("raise", "warn")
    assert "baseline 2/s" in note.text
    assert _poll(conn, settings, clock, 100.0, at=T0 + 600) == []   # held: > 20
    assert _state(conn) == "active"
    [note] = _poll(conn, settings, clock, 15.0, at=T0 + 900)        # < 10 x 2
    assert note.kind == "clear"


def test_a_single_bad_sample_never_notifies(conn, settings, clock):
    _history(conn, 2.0)
    assert _poll(conn, settings, clock, 3000.0, at=T0) == []
    assert _poll(conn, settings, clock, 1.0, at=T0 + 300) == []    # never active


def test_a_pending_alert_must_clear_the_raise_bar_again(conn, settings, clock):
    """`for` 2 means two polls above the raise bar: the hold threshold is
    an active alert's, so a spike followed by 25/s (above clear x 2, under
    the floor) never notifies."""
    _history(conn, 2.0)
    assert _poll(conn, settings, clock, 3000.0, at=T0) == []
    assert _poll(conn, settings, clock, 25.0, at=T0 + 300) == []
    assert _state(conn) == "cleared"


def test_canary_port_parses_colons_in_the_interface():
    assert attention._canary_port("port:canary:sw1:1/0/5:out_errors") == ("sw1", "1/0/5")
    assert attention._canary_port("port:canary:sw1:eth0:1:in_errors") == ("sw1", "eth0:1")
    assert attention._canary_port("port:canary:sw1:eth0:bogus") is None
    assert attention._canary_port("link:sw1:1:sw2:2") is None


def test_down_device_holds_the_canary(conn, settings, clock):
    pdb.upsert_device(conn, name="sw1", source="librenms", role="switch",
                      status="up")
    _history(conn, 2.0)
    _poll(conn, settings, clock, 3000.0, at=T0)
    _poll(conn, settings, clock, 3000.0, at=T0 + 300)
    assert _state(conn) == "active"
    # the switch dies: no more samples, and LibreNMS says down
    conn.execute("UPDATE devices SET status = 'down'")
    clock["now"] = T0 + 3 * H
    notes = alerting.evaluate(conn, attention.attention_items(conn, settings)[0],
                              now=T0 + 3 * H)
    assert [n.kind for n in notes if n.rule == "port-canary"] == []
    assert _state(conn) == "active"


# --- schema, catalog, Rules tab ----------------------------------------------

def test_migration_adds_counter_columns_to_an_old_rate_history():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE rate_history (device TEXT NOT NULL, interface TEXT "
              "NOT NULL, ts REAL NOT NULL, in_bps INTEGER, out_bps INTEGER)")
    c.execute("INSERT INTO rate_history VALUES ('sw1', '1/0/1', 1.0, 8, 16)")
    pdb.init(c)
    pdb.init(c)                                   # idempotent
    cols = [r[1] for r in c.execute("PRAGMA table_info(rate_history)")]
    assert cols[-4:] == list(pdb.RATE_COUNTERS)
    row = c.execute("SELECT * FROM rate_history").fetchone()
    assert (row["in_bps"], row["out_errors"]) == (8, None)


def test_catalog_row_and_category(conn):
    alerting.seed_rules(conn)
    params = json.loads(conn.execute(
        "SELECT params FROM alert_rules WHERE name = 'port-canary'").fetchone()[0])
    assert params == {"for": 2, "floor": 50, "multiplier": 100, "clear": 10,
                      "warmup_h": 24}
    assert alerting.RULES["port-canary"].category == "port"
    assert attention.CATEGORIES["port"] == "ports"


def test_rules_tab_edits_canary_params(clean_env, tmp_path):
    import patchbay.web as web

    client = TestClient(web.app)
    page = client.get("/alerts?tab=rules").text
    assert 'name="param_floor"' in page and 'name="param_warmup_h"' in page
    assert client.post("/alerts/rules/port-canary", data={
        "enabled": "1", "param_floor": "500", "param_multiplier": "1000",
        "param_for": "3"}).status_code == 200
    assert client.post("/alerts/rules/port-canary", data={
        "enabled": "1", "param_floor": "lots"}).status_code == 400
    with pdb.connect(str(tmp_path / "test.db")) as c:
        p = json.loads(c.execute("SELECT params FROM alert_rules "
                                 "WHERE name = 'port-canary'").fetchone()[0])
    assert (p["floor"], p["multiplier"], p["for"], p["clear"]) == (500, 1000, 3, 10)
