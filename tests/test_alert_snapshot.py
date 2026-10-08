"""Snapshot on critical (#66, ADR-0003 Decision 7): trigger, cooldown, own
keep count, naming, separation from the tiers, failure path, and the
roadmap's done-when on the demo network."""

import json
import sqlite3
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from patchbay import db as pdb
from patchbay import retention, snapshot, transports
from patchbay.alerting import Notification
from patchbay.config import load_settings

T0 = 1_800_000_000.0


def _note(kind="raise", severity="crit", key="device:ap1", route=None, text="ap1 is down"):
    return Notification(kind=kind, key=key, rule="device-down", severity=severity,
                        text=text, href="/device/ap1", raised_at=T0, route=route)


@pytest.fixture()
def env(clean_env, tmp_path, monkeypatch):
    """A schema'd DB, a snapshot dir, and a generator that renders nothing:
    these tests are about when and where, not what the page holds."""
    with pdb.connect(str(tmp_path / "test.db")) as c:
        pdb.init(c)
    clean_env.setenv("PATCHBAY_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    clean_env.setenv("PATCHBAY_ALERT_SNAPSHOT", "on")
    monkeypatch.setattr(snapshot, "generate", lambda s: "<html>snap</html>")
    return tmp_path


def _alert_files(d):
    return sorted(p.name for p in d.glob("*-alert.html")) if d.exists() else []


def _state(tmp_path, key):
    with pdb.connect(str(tmp_path / "test.db")) as c:
        return pdb.get_state(c, key)


# -- naming and retention ----------------------------------------------------

def test_alert_name_is_its_own_pattern():
    assert retention.alert_stamp_of("patchbay-20261007-120000-alert.html") == \
        datetime(2026, 10, 7, 12, 0, 0)
    assert retention.alert_stamp_of("patchbay-20261007-120000.html") is None
    assert retention.alert_stamp_of("patchbay-latest.html") is None
    assert retention.alert_stamp_of("patchbay-20261399-120000-alert.html") is None
    assert retention.stamp_of("patchbay-20261007-120000-alert.html") is None


def test_alert_keep_count_is_newest_n():
    names = [f"patchbay-2026100{d}-120000-alert.html" for d in range(1, 6)]
    tiered = "patchbay-20261001-000000.html"
    assert retention.alert_prunable(names + [tiered], 2) == names[:3]
    assert retention.alert_prunable(names, 0) == []     # 0 = unlimited
    assert retention.alert_prunable([tiered], 1) == []  # never a tiered file


def test_tiers_and_alert_count_never_touch_each_other(env, clean_env):
    """Both directions, on disk: a tier spec that would keep almost nothing
    leaves alert snapshots alone, and an alert count of 1 leaves tiered
    snapshots alone. Local and delivery directories alike."""
    clean_env.setenv("PATCHBAY_SNAPSHOT_KEEP", "1")
    clean_env.setenv("PATCHBAY_SNAPSHOT_DELIVER_DIR", str(env / "offsite"))
    s = load_settings()
    days = [datetime.now() - timedelta(days=n) for n in (5, 4, 3)]
    tiered = [d.strftime("patchbay-%Y%m%d-000000.html") for d in days]
    alerts = [d.strftime("patchbay-%Y%m%d-010000-alert.html") for d in days]
    for d in (env / "snaps", env / "offsite"):
        d.mkdir()
        for n in tiered + alerts:
            (d / n).write_text("old")

    # a plain snapshot: the tiers keep only today's, every alert file stays
    path = snapshot.write_snapshot(s)
    for d in (env / "snaps", env / "offsite"):
        assert _alert_files(d) == alerts
        assert sorted(p.name for p in d.glob("patchbay-2*.html")
                      if retention.stamp_of(p.name)) == [path.name]

    # put the tiered files back, then an alert snapshot with keep 1: only
    # alert files go, and the tiers still run their own rule on plain ones
    for d in (env / "snaps", env / "offsite"):
        for n in tiered:
            (d / n).write_text("old")
    clean_env.setenv("PATCHBAY_SNAPSHOT_KEEP", "0")   # keep every tiered file
    apath = snapshot.write_snapshot(load_settings(), alert_keep=1)
    assert apath.name.endswith("-alert.html")
    for d in (env / "snaps", env / "offsite"):
        assert _alert_files(d) == [apath.name]
        assert set(tiered) <= {p.name for p in d.iterdir()}
        assert (d / "patchbay-latest.html").exists()


# -- the trigger -------------------------------------------------------------

def test_crit_raise_takes_a_named_snapshot(env):
    lines = snapshot.take_alert_snapshot(load_settings(), [_note()], now=T0)
    [name] = _alert_files(env / "snaps")
    assert lines == [f"[ok]   alert snapshot: {env / 'snaps' / name}"]
    assert retention.alert_stamp_of(name) is not None
    log = json.loads(_state(env, snapshot.ALERT_LOG_KEY))
    assert log[0]["name"] == name
    assert log[0]["alerts"] == [{"key": "device:ap1", "rule": "device-down",
                                 "kind": "raise", "text": "ap1 is down"}]


def test_escalation_to_crit_triggers(env):
    snapshot.take_alert_snapshot(load_settings(), [_note(kind="escalate")], now=T0)
    assert len(_alert_files(env / "snaps")) == 1


@pytest.mark.parametrize("note", [
    _note(severity="warn"),                 # a warning never does
    _note(kind="remind"),                   # a reminder is not news
    _note(kind="clear"),
    _note(kind="event"),
])
def test_non_triggers_take_nothing(env, note):
    assert snapshot.take_alert_snapshot(load_settings(), [note], now=T0) == []
    assert _alert_files(env / "snaps") == []
    assert _state(env, snapshot.ALERT_LAST_KEY) is None


def test_a_crit_routed_nowhere_still_snapshots(env):
    """The owner's call on #66: every new crit takes one, routed or not.
    Silenced alerts (#62) produce no notification, so they never do."""
    snapshot.take_alert_snapshot(load_settings(), [_note(route="none")], now=T0)
    assert len(_alert_files(env / "snaps")) == 1


def test_concurrent_callers_take_one_snapshot(env, monkeypatch):
    """The poller and /ops/poll can finish at once: the cooldown claim is
    atomic, so exactly one writes and the other reports the cooldown."""
    import threading

    s = load_settings()
    gate = threading.Barrier(2)
    real_claim = snapshot._claim_cooldown

    def claim(conn, now, cooldown):
        gate.wait()            # both callers reach the claim together
        return real_claim(conn, now, cooldown)

    monkeypatch.setattr(snapshot, "_claim_cooldown", claim)
    out: list[list[str]] = []
    threads = [threading.Thread(target=lambda: out.append(
        snapshot.take_alert_snapshot(s, [_note()], now=T0))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_alert_files(env / "snaps")) == 1
    assert sorted(lines[0].split(":")[0] for lines in out) == [
        "[ok]   alert snapshot", "[ok]   alert snapshot"]
    assert sum("in cooldown" in lines[0] for lines in out) == 1
    with pdb.connect(str(env / "test.db")) as c:
        assert len(snapshot.alert_log(c)) == 1


def test_same_second_with_no_cooldown_never_overwrites(env, clean_env, monkeypatch):
    """Cooldown 0, two callers in one second: the file is named from the
    claimed time and a claim needs a fresh second, so one wins and the other
    reports the cooldown. One file, one sidecar entry, nothing overwritten."""
    import threading

    s = load_settings_with(clean_env, PATCHBAY_ALERT_SNAPSHOT_COOLDOWN="0")
    gate = threading.Barrier(2)
    real_claim = snapshot._claim_cooldown

    def claim(conn, now, cooldown):
        gate.wait()
        return real_claim(conn, now, cooldown)

    monkeypatch.setattr(snapshot, "_claim_cooldown", claim)
    out: list[list[str]] = []
    threads = [threading.Thread(target=lambda n=n: out.append(snapshot.take_alert_snapshot(
        s, [_note(key=f"device:ap{n}", text=f"ap{n} is down")], now=T0 + n / 10)))
        for n in (1, 2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    [name] = _alert_files(env / "snaps")
    assert name == datetime.fromtimestamp(T0).strftime("patchbay-%Y%m%d-%H%M%S-alert.html")
    assert sum("in cooldown" in lines[0] for lines in out) == 1
    with pdb.connect(str(env / "test.db")) as c:
        log = snapshot.alert_log(c)
        assert list(log) == [name]
        winner = [lines for lines in out if "in cooldown" not in lines[0]][0]
        assert len(log[name]["alerts"]) == 1 and winner[0].endswith(name)
    # a second later is a new second: a second file
    monkeypatch.setattr(snapshot, "_claim_cooldown", real_claim)
    snapshot.take_alert_snapshot(s, [_note()], now=T0 + 1.5)
    assert len(_alert_files(env / "snaps")) == 2


def test_cooldown_whatever_raised_it(env, clean_env):
    s = load_settings()
    snapshot.take_alert_snapshot(s, [_note()], now=T0)
    # a different alert ten minutes later: inside the default hour
    lines = snapshot.take_alert_snapshot(s, [_note(key="device:ap2")], now=T0 + 600)
    assert lines[0].startswith("[ok]   alert snapshot: in cooldown until")
    assert len(_alert_files(env / "snaps")) == 1
    assert float(_state(env, snapshot.ALERT_LAST_KEY)) == T0
    # past the window, the next one is taken
    snapshot.take_alert_snapshot(s, [_note(key="device:ap3")], now=T0 + 3601)
    assert float(_state(env, snapshot.ALERT_LAST_KEY)) == T0 + 3601

    # cooldown 0 = none
    s = load_settings_with(clean_env, PATCHBAY_ALERT_SNAPSHOT_COOLDOWN="0")
    assert snapshot.take_alert_snapshot(s, [_note()], now=T0 + 3602)[0].startswith(
        "[ok]   alert snapshot: /")


def test_keep_count_from_settings(env, clean_env):
    s = load_settings_with(clean_env, PATCHBAY_ALERT_SNAPSHOT_KEEP="2")
    d = env / "snaps"
    d.mkdir()
    for n in range(1, 4):
        (d / f"patchbay-2020010{n}-000000-alert.html").write_text("old")
    snapshot.take_alert_snapshot(s, [_note()], now=T0)
    files = _alert_files(d)
    assert len(files) == 2 and files[0] == "patchbay-20200103-000000-alert.html"


def load_settings_with(mp, **env):
    for k, v in env.items():
        mp.setenv(k, v)
    return load_settings()


# -- the flag and its settings (env-style declarations) ------------------------

def test_off_by_default_takes_nothing(env, clean_env):
    clean_env.delenv("PATCHBAY_ALERT_SNAPSHOT")
    s = load_settings()
    assert s.alert_snapshot is False
    assert snapshot.take_alert_snapshot(s, [_note()], now=T0) == []
    assert _alert_files(env / "snaps") == []
    assert _state(env, snapshot.ALERT_LAST_KEY) is None   # no cooldown claim


def test_flag_from_env_or_db_and_env_wins(env, clean_env):
    clean_env.delenv("PATCHBAY_ALERT_SNAPSHOT")
    with pdb.connect(str(env / "test.db")) as c:
        pdb.set_state(c, "cfg:PATCHBAY_ALERT_SNAPSHOT", "yes")
        pdb.set_state(c, "cfg:PATCHBAY_ALERT_SNAPSHOT_COOLDOWN", "15")
        pdb.set_state(c, "cfg:PATCHBAY_ALERT_SNAPSHOT_KEEP", "3")
    s = load_settings()
    assert (s.alert_snapshot, s.alert_snapshot_cooldown, s.alert_snapshot_keep) == (True, 15, 3)
    assert s.declaration_sources["PATCHBAY_ALERT_SNAPSHOT"] == "db"
    clean_env.setenv("PATCHBAY_ALERT_SNAPSHOT", "off")
    clean_env.setenv("PATCHBAY_ALERT_SNAPSHOT_KEEP", "0")
    s = load_settings()
    assert (s.alert_snapshot, s.alert_snapshot_keep) == (False, 0)
    assert s.declaration_sources["PATCHBAY_ALERT_SNAPSHOT"] == "env"
    for word in ("true", "ON", "1", "yes"):
        clean_env.setenv("PATCHBAY_ALERT_SNAPSHOT", word)
        assert load_settings().alert_snapshot is True


def test_bad_values_warn_and_fall_back(clean_env):
    clean_env.setenv("PATCHBAY_ALERT_SNAPSHOT", "maybe")
    clean_env.setenv("PATCHBAY_ALERT_SNAPSHOT_COOLDOWN", "-5")
    clean_env.setenv("PATCHBAY_ALERT_SNAPSHOT_KEEP", "ten")
    s = load_settings()
    assert (s.alert_snapshot, s.alert_snapshot_cooldown, s.alert_snapshot_keep) == (False, 60, 10)
    warned = " ".join(s.parse_warnings)
    for var in ("PATCHBAY_ALERT_SNAPSHOT:", "PATCHBAY_ALERT_SNAPSHOT_COOLDOWN:",
                "PATCHBAY_ALERT_SNAPSHOT_KEEP:"):
        assert var in warned


# -- failure -------------------------------------------------------------------

def test_failure_records_an_alert_trigger_and_never_raises(env, monkeypatch):
    def boom(settings, *a, **kw):
        raise RuntimeError("renderer crashed")

    monkeypatch.setattr(snapshot, "generate", boom)
    lines = snapshot.take_alert_snapshot(load_settings(), [_note()], now=T0)
    assert lines == ["[warn] alert snapshot failed: renderer crashed"]
    with pdb.connect(str(env / "test.db")) as c:
        [f] = pdb.snapshot_failures(c)
        assert (f["kind"], f["trigger"]) == ("failed", "alert")
        # the snapshot-failed rule reads it on the next poll
        from patchbay.attention import attention_items
        items, _ = attention_items(c, load_settings())
        assert any(i["rule"] == "snapshot-failed" and i["text"].startswith("alert snapshot failed")
                   for i in items)


def test_undelivered_is_recorded_and_still_listed(env, clean_env):
    blocked = env / "blocked"
    blocked.write_text("not a directory")
    clean_env.setenv("PATCHBAY_SNAPSHOT_DELIVER_DIR", str(blocked))
    lines = snapshot.take_alert_snapshot(load_settings(), [_note()], now=T0)
    assert lines[0].startswith("[warn] alert snapshot written but delivery failed")
    [name] = _alert_files(env / "snaps")
    with pdb.connect(str(env / "test.db")) as c:
        [f] = pdb.snapshot_failures(c)
        assert (f["kind"], f["trigger"]) == ("undelivered", "alert")
        assert name in snapshot.alert_log(c)


def test_poll_survives_an_alert_snapshot_failure(env, monkeypatch):
    import argparse

    from patchbay import alerting, cli

    class Quiet:
        def collect(self, settings, conn):
            return "nothing"

    def boom(settings, *a, **kw):
        raise RuntimeError("renderer crashed")

    order = []
    monkeypatch.setattr(cli, "available", lambda s: {"quiet": Quiet()})
    monkeypatch.setattr(alerting, "run", lambda conn, s: [_note()])
    real_dispatch = alerting.dispatch
    monkeypatch.setattr(alerting, "dispatch",
                        lambda notes, d=None: order.append("dispatch") or real_dispatch(notes, d))
    monkeypatch.setattr(snapshot, "write_snapshot",
                        lambda *a, **kw: order.append("snapshot") or boom(*a))
    assert cli.cmd_poll(argparse.Namespace(source=None)) == 0
    assert order == ["dispatch", "snapshot"]
    with pdb.connect(str(env / "test.db")) as c:
        assert pdb.snapshot_failures(c)[0]["trigger"] == "alert"


# -- the UI ------------------------------------------------------------------

def test_pages_state_the_flag(env, clean_env):
    import patchbay.web as web

    client = TestClient(web.app)
    assert "at most one per 60 min" in client.get("/alerts?tab=rules").text
    assert "Snapshot on critical:\non · at most one per 60 min" in client.get("/snapshots").text
    assert "PATCHBAY_ALERT_SNAPSHOT (+ _COOLDOWN, _KEEP)" in client.get("/ops").text
    assert client.post("/alerts/snapshot", data={"cooldown": "1"}).status_code in (404, 405)
    clean_env.delenv("PATCHBAY_ALERT_SNAPSHOT")
    page = client.get("/alerts?tab=rules").text
    assert 'id="alert-snapshot"' in page and "Off: turn it on" in page
    assert "Snapshot on critical:\noff" in client.get("/snapshots").text


def test_snapshots_page_lists_alert_snapshots_with_their_cause(env):
    import patchbay.web as web

    snapshot.take_alert_snapshot(load_settings(), [_note(text="ap1 is down")], now=T0)
    [name] = _alert_files(env / "snaps")
    client = TestClient(web.app)
    page = client.get("/snapshots").text
    assert name in page and "ap1 is down" in page
    assert client.get(f"/snapshots/{name}").status_code == 200
    assert client.post(f"/snapshots/{name}/delete",
                       follow_redirects=False).status_code == 303
    assert _alert_files(env / "snaps") == []


# -- done when ---------------------------------------------------------------

def test_done_when_unplugged_aps_on_the_demo(clean_env, tmp_path, monkeypatch):
    """Unplugging a demo AP produces a notification and an -alert snapshot
    within one poll; unplugging a second one ten minutes later produces a
    notification only."""
    import patchbay.web as web
    from patchbay import demo

    dbp = str(tmp_path / "test.db")
    with pdb.connect(dbp) as c:
        demo.seed(c)
    clean_env.setenv("PATCHBAY_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    clean_env.setenv("PATCHBAY_ALERT_SNAPSHOT", "on")
    monkeypatch.setattr(snapshot, "generate", lambda s: "<html>snap</html>")
    seen: list[httpx.Request] = []
    monkeypatch.setattr(transports, "HTTP_TRANSPORT", httpx.MockTransport(
        lambda r: seen.append(r) or httpx.Response(200)))
    with pdb.connect(dbp) as c:
        c.execute("INSERT INTO alert_transports (name, kind, url) VALUES (?, ?, ?)",
                  ("hook", "generic", "https://hooks.example.com/w/t"))
    client = TestClient(web.app)
    client.post("/ops/poll")   # whatever the demo already has open is old news

    def unplug(ap):
        with pdb.connect(dbp) as c:
            c.execute("UPDATE devices SET status = 'down' WHERE name = ?", (ap,))
            # the clock between polls: the last attempt is pushed back
            last = float(pdb.get_state(c, snapshot.ALERT_LAST_KEY) or 0)
            pdb.set_state(c, snapshot.ALERT_LAST_KEY,
                          repr(last - (7200 if ap == "ap-attic" else 600)))
        seen.clear()
        before = set(_alert_files(tmp_path / "snaps"))
        lines = client.post("/ops/poll").json()["lines"]
        sent = [r for r in seen if ap.encode() in r.content]
        return sent, set(_alert_files(tmp_path / "snaps")) - before, lines

    sent, new, _ = unplug("ap-attic")
    assert sent and len(new) == 1
    with pdb.connect(dbp) as c:
        [cause] = snapshot.alert_log(c)[new.pop()]["alerts"]
        assert "ap-attic" in cause["text"]

    sent, new, lines = unplug("ap-den")
    assert sent and not new
    assert any("in cooldown" in line for line in lines)
