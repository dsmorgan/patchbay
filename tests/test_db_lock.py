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
