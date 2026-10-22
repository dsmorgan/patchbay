"""#109: the SQLite write lock. A poll holding it must not freeze the UI,
a GET must never need it, and two polls must not collide on it."""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from patchbay import alerting
from patchbay import db as pdb


def _db(tmp_path) -> str:
    path = str(tmp_path / "test.db")
    with pdb.connect(path) as c:
        pdb.init(c)
    return path


def _hold(path: str, mode: str = "IMMEDIATE") -> sqlite3.Connection:
    """Another process mid-write: a poll inside a collector's transaction
    (IMMEDIATE, readers still read) or committing (EXCLUSIVE, they wait)."""
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute(f"BEGIN {mode}")
    holder.execute("INSERT INTO app_state (key, value) VALUES ('holder', '1')")
    return holder


def test_rules_tab_is_read_only_and_answers_under_a_held_write_lock(clean_env, tmp_path):
    import patchbay.web as web

    path = _db(tmp_path)
    holder = _hold(path)
    try:
        client = TestClient(web.app)
        t0 = time.monotonic()
        r = client.get("/alerts?tab=rules")
        assert r.status_code == 200
        assert time.monotonic() - t0 < 2      # was 5 s and then a 500
        for name in alerting.RULES:           # catalog defaults, shown unseeded
            assert f'action="/alerts/rules/{name}"' in r.text
    finally:
        holder.rollback()
        holder.close()
    with pdb.connect(path) as c:
        assert c.execute("SELECT COUNT(*) FROM alert_rules").fetchone()[0] == 0


@pytest.mark.parametrize("tab", ["active", "history", "rules", "tunnels",
                                 "transports", "silenced", "silences"])
def test_alerts_tabs_never_write(clean_env, tmp_path, tab):
    import patchbay.web as web

    path = _db(tmp_path)
    holder = _hold(path)
    try:
        assert TestClient(web.app).get(f"/alerts?tab={tab}").status_code == 200
    finally:
        holder.rollback()
        holder.close()


def test_a_save_waiting_on_the_lock_does_not_stall_other_requests(clean_env, tmp_path):
    """The old async handlers waited out SQLite's busy timeout on the event
    loop: every other request, static files included, froze behind one form
    post. Now the save waits in a worker thread, and lands once the poll's
    short write phase commits."""
    import patchbay.web as web

    path = _db(tmp_path)
    holder = _hold(path)
    saved: dict = {}
    with TestClient(web.app) as client:   # one event loop for every request
        def save():
            saved["r"] = client.post("/alerts/silences", data={
                "kind": "device", "scope": "sw1", "hours": "1", "reason": "x"},
                follow_redirects=False)

        writer = threading.Thread(target=save)
        writer.start()
        try:
            time.sleep(0.3)                   # the save is now in its busy wait
            t0 = time.monotonic()
            assert client.get("/static/d3.v7.min.js").status_code == 200
            assert client.get("/alerts").status_code == 200
            assert time.monotonic() - t0 < 2
            assert writer.is_alive()          # still waiting on the lock
        finally:
            holder.rollback()                 # the poll's write phase ends
            holder.close()
        writer.join(10)
    assert saved["r"].status_code == 303, saved["r"].text
    with pdb.connect(path) as c:
        assert c.execute("SELECT scope FROM alert_silences").fetchone()[0] == "sw1"


def test_auth_gate_reads_no_database(clean_env, tmp_path):
    """The gate ran load_settings on the event loop for every request, and
    that read the stored declarations with a 10 s busy timeout: a commit in
    progress froze even static files. The gate reads env-only settings."""
    import patchbay.web as web

    path = _db(tmp_path)
    holder = _hold(path, "EXCLUSIVE")         # readers wait on this one
    try:
        t0 = time.monotonic()
        r = TestClient(web.app).get("/static/d3.v7.min.js")
        assert r.status_code == 200
        assert time.monotonic() - t0 < 2
    finally:
        holder.rollback()
        holder.close()


def test_auth_gate_still_rechecks_settings_per_request(clean_env, tmp_path):
    """Off the loop, not cached: a password rotation still ends a session
    on its next request (#94's fingerprint), without a restart."""
    import patchbay.web as web

    _db(tmp_path)
    clean_env.setenv("PATCHBAY_AUTH", "password")
    clean_env.setenv("PATCHBAY_PASSWORD", "first-secret")
    client = TestClient(web.app)
    r = client.post("/login", data={"password": "first-secret", "next": "/"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/ops", follow_redirects=False).status_code == 200
    clean_env.setenv("PATCHBAY_PASSWORD", "second-secret")
    r = client.get("/ops", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


# --- the poll lease: one poll at a time across processes ---------------------

def test_poll_lease_claim_renew_release(tmp_path):
    path = _db(tmp_path)
    a = sqlite3.connect(path)
    b = sqlite3.connect(path)
    try:
        assert pdb.claim_poll_lease(a, "a")
        assert not pdb.claim_poll_lease(b, "b")        # held
        assert pdb.claim_poll_lease(a, "a")             # renewal
        pdb.release_poll_lease(b, "b")                  # not b's to release
        assert not pdb.claim_poll_lease(b, "b")
        pdb.release_poll_lease(a, "a")
        assert pdb.claim_poll_lease(b, "b")
    finally:
        a.close()
        b.close()


def test_a_dead_pollers_lease_lapses(tmp_path):
    path = _db(tmp_path)
    with pdb.connect(path) as c:
        assert pdb.claim_poll_lease(c, "dead", ttl=-1)   # already lapsed
        assert pdb.claim_poll_lease(c, "next")
        pdb.set_state(c, pdb.POLL_LEASE, "not json")      # damaged: holds nothing
        c.commit()
        assert pdb.claim_poll_lease(c, "after")


def test_lease_claim_does_not_wait_forever_on_a_locked_database(tmp_path):
    path = _db(tmp_path)
    holder = _hold(path)
    c = sqlite3.connect(path, timeout=0.2)
    try:
        assert not pdb.claim_poll_lease(c, "x")
    finally:
        holder.rollback()
        holder.close()
        c.close()


class _SlowCollector:
    """Shaped like a third-party collector that writes, then keeps talking
    to its API inside the transaction: the worst case for the write lock."""
    name = "slow"

    def __init__(self, started: threading.Event | None = None, seconds: float = 1.5):
        self.started, self.seconds = started, seconds

    def configured(self, settings):
        return True

    def collect(self, settings, conn):
        pdb.save_raw(conn, source="slow", endpoint="devices", payload=[])
        if self.started:
            self.started.set()
        time.sleep(self.seconds)
        pdb.upsert_device(conn, name="sw1", source="slow", status="up")
        return "1 device"


def _poll(monkeypatch, collector):
    import argparse

    from patchbay import cli

    monkeypatch.setattr(cli, "available", lambda s: {"slow": collector})
    return cli.cmd_poll(argparse.Namespace(source=None))


def test_overlapping_scheduled_polls_both_complete(clean_env, tmp_path, monkeypatch):
    """Two polls used to collide on the write lock: the second's collector
    failed with 'database is locked' after the busy timeout. The second
    now waits for the lease and then runs."""
    from patchbay import cli

    monkeypatch.setattr(cli, "POLL_LEASE_RETRY", 0.05)
    _db(tmp_path)
    started = threading.Event()
    rcs: dict = {}
    first = threading.Thread(target=lambda: rcs.__setitem__(
        "first", _poll(monkeypatch, _SlowCollector(started, seconds=6))))
    first.start()
    assert started.wait(5)
    rcs["second"] = _poll(monkeypatch, _SlowCollector(seconds=0))
    first.join(20)
    assert rcs == {"first": 0, "second": 0}


def test_ops_poll_answers_409_while_the_poller_runs(clean_env, tmp_path, monkeypatch):
    import patchbay.web as web
    from patchbay import collectors

    _db(tmp_path)
    started = threading.Event()
    rcs: dict = {}
    poller = threading.Thread(target=lambda: rcs.__setitem__(
        "rc", _poll(monkeypatch, _SlowCollector(started))))
    poller.start()
    assert started.wait(5)
    monkeypatch.setattr(collectors, "available", lambda s: {"slow": _SlowCollector()})
    t0 = time.monotonic()
    r = TestClient(web.app).post("/ops/poll")
    assert r.status_code == 409 and "already running" in r.text
    assert time.monotonic() - t0 < 2
    poller.join(20)
    assert rcs["rc"] == 0
    # and once it is done, the button polls
    r = TestClient(web.app).post("/ops/poll")
    assert r.status_code == 200
    assert r.json()["lines"][0].startswith("[ok]   slow")


def test_poll_gives_up_on_a_lease_that_never_frees(clean_env, tmp_path, monkeypatch,
                                                   capsys):
    from patchbay import cli

    path = _db(tmp_path)
    with pdb.connect(path) as c:
        assert pdb.claim_poll_lease(c, "web:1:busy")
    monkeypatch.setattr(cli, "POLL_LEASE_WAIT", 0.2)
    monkeypatch.setattr(cli, "POLL_LEASE_RETRY", 0.05)
    assert _poll(monkeypatch, _SlowCollector(seconds=0)) == 1
    assert "another poll held the lease" in capsys.readouterr().err
    with pdb.connect(path) as c:
        assert pdb.get_state(c, "last_poll") is None      # nothing ran
        assert c.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0


def test_poll_releases_the_lease(clean_env, tmp_path, monkeypatch):
    path = _db(tmp_path)
    assert _poll(monkeypatch, _SlowCollector(seconds=0)) == 0
    with pdb.connect(path) as c:
        assert pdb.get_state(c, pdb.POLL_LEASE) is None
        assert c.execute("SELECT name FROM devices").fetchone()[0] == "sw1"


def test_a_save_that_outwaits_the_busy_timeout_is_a_503(clean_env, tmp_path, monkeypatch):
    import patchbay.web as web

    path = _db(tmp_path)
    real = web._conn

    def quick():   # the 5 s busy timeout, shortened for the test
        conn = real()
        conn.execute("PRAGMA busy_timeout = 200")
        return conn

    monkeypatch.setattr(web, "_conn", quick)
    holder = _hold(path)
    try:
        r = TestClient(web.app, raise_server_exceptions=False).post(
            "/alerts/silences", data={"kind": "device", "scope": "sw1"})
        assert r.status_code == 503
        assert r.headers["retry-after"] == "5"
    finally:
        holder.rollback()
        holder.close()
