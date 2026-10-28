"""Port-counter canaries (issue #64, ADR-0003 Decision 5): the baseline
math, the floor, the multiplier, warm-up, hysteresis held through the open
alert, `for` 2 through the engine, inhibition by a down device, and the
rate_history migration. Every sample here is synthetic."""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from patchbay import alerting, attention, silences
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
             counter="out_errors"):
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
        _sample(conn, T0 - 2 * H - i * 600, out_errors=float(i + 1))
    _sample(conn, T0 - 8 * 86400, out_errors=1e6)     # outside the week
    for m in (5, 20, 50):                     # the flood itself: last hour
        _sample(conn, T0 - m * 60, out_errors=1e6)
    assert attention.canary_baseline(conn, "sw1", "1/0/5", "out_errors",
                                     T0, 24 * H) == 95
    # each counter is its own series
    assert attention.canary_baseline(conn, "sw1", "1/0/5", "in_errors",
                                     T0, 24 * H) is None


def test_baseline_is_none_while_warming_up(conn):
    _history(conn, 3.0, hours=20)
    assert attention.canary_baseline(conn, "sw1", "1/0/5", "out_errors",
                                     T0, 24 * H) is None
    assert attention.canary_baseline(conn, "sw1", "1/0/5", "out_errors",
                                     T0, 12 * H) == 3.0


# --- the rule ----------------------------------------------------------------

def test_floor_keeps_a_quiet_port_quiet(conn, settings, clock):
    _history(conn, 0.0)
    _sample(conn, T0 - 60, out_errors=40.0)       # 0 -> 40: under the floor
    assert _canaries(conn, settings) == []
    _sample(conn, T0 - 30, out_errors=60.0)       # newest sample wins
    [it] = _canaries(conn, settings)
    assert it["key"] == "port:canary:sw1:1/0/5:out_errors"
    assert (it["category"], it["severity"]) == ("port", "warn")
    assert it["text"] == "sw1 1/0/5 out errors at 60/s (baseline 0/s)"
    assert it["href"] == "/device/sw1#port-1/0/5"
    assert it["ports"] == [("sw1", "1/0/5")]


def test_canary_href_encodes_the_device_name(conn, settings, clock):
    _history(conn, 0.0, dev="sw #1")
    _sample(conn, T0 - 30, dev="sw #1", out_errors=60.0)
    [it] = _canaries(conn, settings)
    assert it["href"] == "/device/sw%20%231#port-1/0/5"


def test_multiplier_against_the_ports_own_baseline(conn, settings, clock):
    _history(conn, 20.0)                          # a port that always errors
    _sample(conn, T0 - 60, out_errors=1900.0)     # 95x: its normal chatter
    assert _canaries(conn, settings) == []
    _sample(conn, T0 - 30, out_errors=4200.0)     # 210x
    [it] = _canaries(conn, settings)
    assert it["text"] == "sw1 1/0/5 out errors at 4,200/s (baseline 20/s)"


def test_warm_up_judges_against_the_floor_alone(conn, settings, clock):
    _history(conn, 1.0, hours=10)                 # 10 h of history: warming up
    _sample(conn, T0 - 60, in_errors=0.0, out_errors=60.0)
    [it] = _canaries(conn, settings)              # 60x its baseline, above floor
    assert it["text"] == ("sw1 1/0/5 out errors at 60/s "
                          "(no baseline yet, floor 50/s)")


def test_hysteresis_holds_until_below_clear_times_baseline(conn, settings, clock):
    _history(conn, 10.0)
    _sample(conn, T0 - 60, out_errors=200.0)      # 20x: not enough to raise
    assert _canaries(conn, settings) == []
    conn.execute("INSERT INTO alerts (key, rule, category, severity, state, "
                 "polls, raised_at) VALUES "
                 "('port:canary:sw1:1/0/5:out_errors', 'port-canary', 'port', "
                 "'warn', 'active', 3, ?)", (T0 - H,))
    assert len(_canaries(conn, settings)) == 1    # ...but enough to hold
    _sample(conn, T0 - 30, out_errors=90.0)       # under 10x: clears
    assert _canaries(conn, settings) == []


def test_hysteresis_on_a_zero_baseline_port(conn, settings, clock):
    """A quiet port's effective baseline is floor/multiplier, so it raises
    at the floor and holds down to clear/multiplier of it, not to zero."""
    _history(conn, 0.0)
    conn.execute("INSERT INTO alerts (key, rule, category, severity, state, "
                 "polls, raised_at) VALUES "
                 "('port:canary:sw1:1/0/5:out_errors', 'port-canary', 'port', "
                 "'warn', 'active', 3, ?)", (T0 - H,))
    _sample(conn, T0 - 60, out_errors=6.0)        # 50 / 100 * 10 = 5
    assert len(_canaries(conn, settings)) == 1
    _sample(conn, T0 - 30, out_errors=4.0)
    assert _canaries(conn, settings) == []


def test_params_and_silence(conn, clean_env, clock):
    _history(conn, 0.0)
    _sample(conn, T0 - 60, in_errors=60.0)
    alerting.seed_rules(conn)
    conn.execute("UPDATE alert_rules SET params = ? WHERE name = 'port-canary'",
                 (json.dumps({"for": 2, "floor": 100}),))
    assert _canaries(conn, load_settings()) == []
    silences.add(conn, {"kind": "port", "scope": "sw1:1/0/5", "until": None,
                        "reason": None, "created_by": "t", "created_at": T0})
    conn.execute("UPDATE alert_rules SET params = '{}' WHERE name = 'port-canary'")
    # computed, but silenced through the silence list (#62)
    [it] = _canaries(conn, load_settings())
    assert (it["silenced"]["kind"], it["silenced"]["scope"]) == ("port", "sw1:1/0/5")


def test_stale_sample_says_nothing(conn, settings, clock):
    _history(conn, 0.0)
    _sample(conn, T0 - 30 * 60, out_errors=5000.0)     # the poller stopped
    assert _canaries(conn, settings) == []


# --- through the engine ------------------------------------------------------

def _poll(conn, settings, clock, rate, *, at):
    clock["now"] = at
    _sample(conn, at - 10, out_errors=rate)
    return alerting.evaluate(conn, attention.attention_items(conn, settings)[0],
                             now=at)


def _state(conn):
    row = conn.execute("SELECT state FROM alerts WHERE rule = 'port-canary' "
                       "ORDER BY id DESC").fetchone()
    return row["state"] if row else None


def test_a_flood_raises_after_two_polls_and_clears_with_hysteresis(
        conn, settings, clock):
    """The issue's done-when: an error rate that jumps from a quiet baseline
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


def test_canary_port_resolves_colons_from_the_data(conn):
    # #110: either half may hold colons (a MAC-named UniFi device, an
    # interface numbered eth0:1); the split that has samples wins
    for dev, iface in (("sw1", "1/0/5"), ("sw1", "eth0:1"),
                       ("02:00:00:00:00:01", "Port 3")):
        conn.execute("INSERT INTO rate_history (device, interface, ts, in_errors) "
                     "VALUES (?, ?, ?, 0)", (dev, iface, T0))
    assert attention._canary_ports(conn, "port:canary:sw1:1/0/5:out_errors") == [("sw1", "1/0/5")]
    assert attention._canary_ports(conn, "port:canary:sw1:eth0:1:in_errors") == [("sw1", "eth0:1")]
    assert attention._canary_ports(
        conn, "port:canary:02:00:00:00:00:01:Port 3:in_errors") == [("02:00:00:00:00:01", "Port 3")]
    assert attention._canary_ports(conn, "port:canary:sw1:eth0:bogus") == []
    assert attention._canary_ports(conn, "port:canary:sw9:1:in_errors") == []
    assert attention._canary_ports(conn, "link:sw1:1:sw2:2") == []


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


def _evaluate(conn, settings, clock, at):
    """A poll that wrote no new sample: a failed or single-source poll."""
    clock["now"] = at
    return alerting.evaluate(conn, attention.attention_items(conn, settings)[0], now=at)


def test_one_sample_read_on_two_polls_is_one_look(conn, settings, clock):
    # #110: `for` 2 needs two bad samples, not one sample seen twice
    _history(conn, 2.0)
    assert _poll(conn, settings, clock, 3000.0, at=T0) == []
    assert _evaluate(conn, settings, clock, T0 + 300) == []
    assert conn.execute("SELECT state, polls FROM alerts").fetchone()[:] == ("pending", 1)
    [note] = _poll(conn, settings, clock, 3100.0, at=T0 + 600)
    assert note.kind == "raise"


def test_a_silenced_announced_canary_keeps_its_hysteresis(conn, settings, clock):
    # #110: a silence must not turn a flood between the clear and raise
    # bars into a clear sent mid-flood
    _history(conn, 2.0)
    _poll(conn, settings, clock, 3000.0, at=T0)
    [note] = _poll(conn, settings, clock, 3000.0, at=T0 + 300)
    assert note.kind == "raise"
    conn.execute("INSERT INTO alert_silences (kind, scope, until, created_at) "
                 "VALUES ('port', 'sw1:1/0/5', NULL, ?)", (T0,))
    for i in range(2, 5):          # 100/s: under 100 x 2, over 10 x 2
        assert _poll(conn, settings, clock, 100.0, at=T0 + i * 300) == []
    assert _state(conn) == "silenced"
    conn.execute("DELETE FROM alert_silences")
    _poll(conn, settings, clock, 100.0, at=T0 + 1500)    # re-raised, pending
    assert _state(conn) == "pending"
    [note] = _poll(conn, settings, clock, 100.0, at=T0 + 1800)
    assert note.kind == "raise"           # held: still flooding, never cleared


# --- discards are collected, never judged (#83) -------------------------------

def test_a_discard_flood_never_raises(conn, settings, clock):
    """#83: discard samples land in rate_history, but the canary watches
    error rates only until #86 decides otherwise. A week of quiet discards
    and then thousands per second, through the engine: no item, no alert."""
    _history(conn, 2.0, counter="out_discards")
    _history(conn, 2.0, counter="in_discards")
    for i, at in enumerate((T0, T0 + 300, T0 + 600)):
        clock["now"] = at
        _sample(conn, at - 10, in_bps=8_000, in_errors=0.0, out_errors=0.0,
                in_discards=5000.0, out_discards=9000.0 + i)
        assert _canaries(conn, settings) == []
        assert alerting.evaluate(conn, attention.attention_items(conn, settings)[0],
                                 now=at) == []
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


def test_a_discard_only_sample_changes_no_canary_state(conn, settings, clock):
    """A sample carrying discards and no error reading is not the port's
    newest sample to the canary: it neither counts as a second look for a
    pending alert nor clears or holds an active one."""
    _history(conn, 2.0)
    assert _poll(conn, settings, clock, 3000.0, at=T0) == []
    pending = conn.execute("SELECT state, polls, sample_ts FROM alerts").fetchone()[:]
    assert pending[:2] == ("pending", 1)
    clock["now"] = T0 + 300
    _sample(conn, T0 + 290, in_discards=0.0, out_discards=0.0)
    assert _evaluate(conn, settings, clock, T0 + 300) == []
    assert conn.execute("SELECT state, polls, sample_ts FROM alerts").fetchone()[:] == pending
    [note] = _poll(conn, settings, clock, 3100.0, at=T0 + 600)
    assert note.kind == "raise"
    # active: a discard-only sample of zero does not clear it either
    _sample(conn, T0 + 890, out_discards=0.0)
    assert _evaluate(conn, settings, clock, T0 + 900) == []
    assert _state(conn) == "active"


def test_down_mac_named_device_holds_the_canary(conn, settings, clock):
    # #110: a UniFi device with no name is stored as its MAC; the key's
    # colons must not lose its port, or the canary clears when it dies
    mac = "02:00:00:00:00:01"
    pdb.upsert_device(conn, name=mac, source="unifi", role="switch", status="up")
    _history(conn, 2.0, dev=mac, iface="Port 3")
    for at in (T0, T0 + 300):
        clock["now"] = at
        _sample(conn, at - 10, dev=mac, iface="Port 3", out_errors=3000.0)
        alerting.evaluate(conn, attention.attention_items(conn, settings)[0], now=at)
    assert _state(conn) == "active"
    conn.execute("UPDATE devices SET status = 'down'")
    notes = _evaluate(conn, settings, clock, T0 + 3 * H)
    assert [n.kind for n in notes if n.rule == "port-canary"] == []
    assert _state(conn) == "active"


def test_latest_sample_reads_the_ts_index(conn):
    plan = " ".join(str(tuple(r)) for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT device, interface, ts, in_errors "
        "FROM rate_history WHERE ts >= ?", (T0,)))
    assert "idx_rate_hist_ts" in plan


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
    assert params == {"for": 2, "remind": 0, "floor": 50, "multiplier": 100, "clear": 10,
                      "warmup_h": 24}
    assert alerting.RULES["port-canary"].category == "port"
    summary = alerting.RULES["port-canary"].summary
    assert summary.startswith("A port's error rate is at least")
    assert "Discard rates are collected but not judged (see #86)" in summary
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


# --- db.record_rates: the collector contract's way in --------------------------

def test_record_rates_writes_one_sample_in_the_table_columns(conn, clock):
    assert pdb.record_rates(conn, "sw1", "1/0/5", in_bps=4_000_000, out_bps=9,
                            in_errors=0.5, out_errors=0.0, in_discards=2.0,
                            out_discards=3.0) is True
    row = dict(conn.execute("SELECT * FROM rate_history").fetchone())
    assert row["device"] == "sw1" and row["interface"] == "1/0/5"
    assert row["ts"] == T0                                   # defaults to now
    assert (row["in_bps"], row["out_bps"]) == (4_000_000, 9)
    assert (row["in_errors"], row["out_errors"]) == (0.5, 0.0)   # 0 is a value
    assert (row["in_discards"], row["out_discards"]) == (2.0, 3.0)


def test_record_rates_omits_what_the_source_did_not_report(conn):
    pdb.record_rates(conn, "sw1", "1/0/5", in_errors=7.0, ts=T0 - 60)
    row = conn.execute("SELECT * FROM rate_history").fetchone()
    assert row["ts"] == T0 - 60
    assert row["in_errors"] == 7.0
    assert all(row[c] is None for c in ("in_bps", "out_bps", "out_errors",
                                        "in_discards", "out_discards"))


def test_record_rates_writes_nothing_when_nothing_is_live(conn):
    assert pdb.record_rates(conn, "sw1", "1/0/5") is False
    assert conn.execute("SELECT COUNT(*) FROM rate_history").fetchone()[0] == 0


def test_record_rates_leaves_the_commit_to_the_caller(conn):
    conn.commit()
    pdb.record_rates(conn, "sw1", "1/0/5", in_bps=1)
    assert conn.in_transaction


def test_samples_from_record_rates_feed_the_canary(conn, settings, clock):
    """What an out-of-tree collector does: a week of quiet samples, then a
    flood on one port, all through the helper."""
    ts = T0 - 48 * H
    while ts < T0 - 2 * H:
        pdb.record_rates(conn, "sw1", "1/0/5", in_bps=1_000_000,
                         in_errors=0.1, ts=ts)
        ts += 300
    pdb.record_rates(conn, "sw1", "1/0/5", in_bps=1_000_000, in_errors=900.0,
                     ts=T0 - 60)
    [item] = _canaries(conn, settings)
    assert item["key"] == "port:canary:sw1:1/0/5:in_errors"
    assert "baseline 0.1/s" in item["text"]
