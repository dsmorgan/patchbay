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
