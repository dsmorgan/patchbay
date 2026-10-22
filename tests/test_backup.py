"""`patchbay backup` (#112): a consistent, private copy of the live database
that restores to a working instance."""

import os
import sqlite3
import stat
import threading

import pytest

from patchbay import cli, db as pdb


def _live(clean_env):
    """A seeded database at PATCHBAY_DB; returns its path."""
    path = os.environ["PATCHBAY_DB"]
    with pdb.connect(path) as c:
        pdb.init(c)
        pdb.set_state(c, "webhook_secret", "s3cret")
        pdb.upsert_device(c, name="core1", source="test", role="switch")
    return path


def _run(*argv):
    return cli.main(["backup", *argv])


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_backup_is_private_and_restores(clean_env, tmp_path, capsys):
    live = _live(clean_env)
    dest = tmp_path / "out.db"
    assert _run(str(dest)) == 0
    assert _mode(dest) == 0o600
    out = capsys.readouterr().out
    assert str(dest) in out and "bytes" in out and "secrets" in out
    # restore round-trip: the copy opens as a patchbay database with its data
    with pdb.connect(str(dest)) as c:
        pdb.init(c)
        assert c.execute("SELECT name FROM devices").fetchall()[0][0] == "core1"
        assert pdb.get_state(c, "webhook_secret") == "s3cret"
    assert os.path.exists(live)
    assert not [p for p in tmp_path.iterdir() if "partial" in p.name]


def test_backup_ignores_a_loose_umask(clean_env, tmp_path):
    _live(clean_env)
    old = os.umask(0)
    try:
        assert _run(str(tmp_path / "out.db")) == 0
    finally:
        os.umask(old)
    assert _mode(tmp_path / "out.db") == 0o600


def test_backup_during_an_open_write_transaction(clean_env, tmp_path):
    live = _live(clean_env)
    writer = sqlite3.connect(live, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO devices(name, source, last_seen) "
                   "VALUES ('uncommitted', 'test', 0)")
    try:
        assert _run(str(tmp_path / "out.db")) == 0
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    with pdb.connect(str(tmp_path / "out.db")) as c:
        names = [r[0] for r in c.execute("SELECT name FROM devices")]
    assert names == ["core1"]  # the committed row, not the open transaction's


def test_backup_while_a_poller_commits(clean_env, tmp_path):
    live = _live(clean_env)
    stop = threading.Event()

    def poller():
        c = sqlite3.connect(live, timeout=30)
        i = 0
        while not stop.is_set():  # every transaction adds two rows together
            with c:
                for tag in ("a", "b"):
                    c.execute("INSERT INTO devices(name, source, last_seen) "
                              "VALUES (?, 'test', 0)", (f"d{i}{tag}", ))
            i += 1
        c.close()

    t = threading.Thread(target=poller)
    t.start()
    try:
        for n in range(5):
            assert _run(str(tmp_path / f"out{n}.db")) == 0
    finally:
        stop.set()
        t.join()
    for n in range(5):
        c = sqlite3.connect(tmp_path / f"out{n}.db")
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        extra = c.execute("SELECT count(*) FROM devices WHERE name != 'core1'").fetchone()[0]
        assert extra % 2 == 0  # never half a transaction
        c.close()


def test_refuses_to_overwrite_without_force(clean_env, tmp_path, capsys):
    _live(clean_env)
    dest = tmp_path / "out.db"
    dest.write_text("precious")
    assert _run(str(dest)) == 2
    assert "--force" in capsys.readouterr().err
    assert dest.read_text() == "precious"
    assert _run(str(dest), "--force") == 0
    assert _mode(dest) == 0o600
    with pdb.connect(str(dest)) as c:
        assert c.execute("SELECT count(*) FROM devices").fetchone()[0] == 1


def test_refuses_the_delivery_dir(clean_env, tmp_path, capsys):
    _live(clean_env)
    share = tmp_path / "offsite"
    share.mkdir()
    (tmp_path / "alias").symlink_to(share)
    clean_env.setenv("PATCHBAY_SNAPSHOT_DELIVER_DIR", str(share))
    for target in (share / "out.db", share / "sub" / "out.db", tmp_path / "alias" / "out.db", share):
        assert _run(str(target)) == 2, target
        assert "DELIVER_DIR" in capsys.readouterr().err
    assert list(share.iterdir()) == []
    # a sibling with a shared name prefix is not inside it
    assert _run(str(tmp_path / "offsite-private.db")) == 0


def test_directory_target_gets_a_timestamped_name(clean_env, tmp_path):
    _live(clean_env)
    assert _run(str(tmp_path)) == 0
    made = [p for p in tmp_path.iterdir() if p.name.startswith("patchbay-")]
    assert len(made) == 1 and made[0].suffix == ".db"
    assert _mode(made[0]) == 0o600


def test_failures_exit_nonzero_and_leave_nothing(clean_env, tmp_path, capsys):
    # no source database: do not invent an empty one
    assert _run(str(tmp_path / "out.db")) == 1
    assert "no database" in capsys.readouterr().err
    assert not os.path.exists(os.environ["PATCHBAY_DB"])
    # missing destination directory
    _live(clean_env)
    assert _run(str(tmp_path / "nope" / "out.db")) == 1
    assert "backup failed" in capsys.readouterr().err
    # a source that is not a database
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not sqlite" * 200)
    clean_env.setenv("PATCHBAY_DB", str(bad))
    assert _run(str(tmp_path / "out.db")) == 1
    assert not (tmp_path / "out.db").exists()
    assert not [p for p in tmp_path.iterdir() if "partial" in p.name]
