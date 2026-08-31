"""Snapshot generator: redaction correctness and offline generation."""

import os

from patchbay.snapshot import scrub_config


def test_scrub_config_redacts_secret_lines():
    cfg = """hostname sw1
snmp-server community s3cretRO ro
username admin password 7 0257552a6a44
enable super-user-password 8 $1$Qn$abcdef123
interface ethernet 1/1/1
 port-name uplink to core [3]
snmp-server user patchbay-ro auth sha authkey123 priv aes privkey456
tacacs-server key sharedkey
"""
    out, n = scrub_config(cfg)
    assert "s3cretRO" not in out
    o2, _ = scrub_config("username bob otp_seed GEZDGNBVGY3TQOJQ\nhostname sw1")
    assert "GEZDGNBVGY" not in o2 and "hostname sw1" in o2
    assert "0257552a6a44" not in out
    assert "$1$Qn$abcdef123" not in out
    assert "authkey123" not in out and "privkey456" not in out
    assert "sharedkey" not in out
    # context survives; innocent lines untouched
    assert "hostname sw1" in out
    assert "port-name uplink to core [3]" in out
    assert "snmp-server community <redacted>" in out
    assert n >= 5


def test_scrub_config_drops_private_key_blocks():
    cfg = ("interface 1\n-----BEGIN RSA PRIVATE KEY-----\nMIIabc\nMIIdef\n"
           "-----END RSA PRIVATE KEY-----\ninterface 2\n")
    out, n = scrub_config(cfg)
    assert "MIIabc" not in out and "MIIdef" not in out
    assert "interface 1" in out and "interface 2" in out
    assert "<redacted: private key block>" in out


def test_snapshot_generates_offline(clean_env, tmp_path):
    # no LibreNMS, no Oxidized: the snapshot still renders — no graphs and no
    # configs sections, everything else intact (graceful degradation)
    from tests.test_web import seed

    seed(str(tmp_path / "test.db"))
    from patchbay.config import load_settings
    from patchbay.snapshot import write_snapshot

    clean_env.setenv("PATCHBAY_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    path = write_snapshot(load_settings())
    t = path.read_text()
    assert "const SNAPSHOT = true" in t
    assert 'id="dev-sw1"' in t and 'id="dev-hyp1"' in t
    # no oxidized configured -> no configs SECTION (the intro prose mentions
    # the word, so match the heading, not the phrase)
    assert "<h2>Device configs (redacted)</h2>" not in t
    assert "forceSimulation" in t             # d3 inlined
    # no librenms -> no graphs, no error. Match the graph payload specifically:
    # the page also carries an inline SVG favicon, which is a data: URI too.
    assert "data:image/png" not in t
    assert (tmp_path / "snaps" / "patchbay-latest.html").exists()


def test_snapshot_retention(clean_env, tmp_path):
    from tests.test_web import seed

    seed(str(tmp_path / "test.db"))
    from patchbay.config import load_settings
    from patchbay import snapshot as snap

    clean_env.setenv("PATCHBAY_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    clean_env.setenv("PATCHBAY_SNAPSHOT_KEEP", "2")
    d = tmp_path / "snaps"
    d.mkdir()
    for name in ("patchbay-20250101-000000.html", "patchbay-20250102-000000.html",
                 "patchbay-20250103-000000.html"):
        (d / name).write_text("old")
    snap.write_snapshot(load_settings())
    kept = sorted(p.name for p in d.glob("patchbay-2*.html"))
    assert len(kept) == 2 and kept[-1].startswith("patchbay-2")
    assert "patchbay-20250101-000000.html" not in kept


def test_last_poll_recorded(conn):
    from patchbay import db as pdb

    pdb.save_last_poll(conn, ["[ok]   librenms: 5 devices", "[fail] unifi: boom"])
    import json
    data = json.loads(pdb.get_state(conn, "last_poll"))
    assert data["lines"][1].startswith("[fail]")
    assert data["ts"] > 0


def test_snapshot_delivery_copies_and_survives_failure(clean_env, tmp_path):
    from tests.test_web import seed

    seed(str(tmp_path / "test.db"))
    from patchbay.config import load_settings
    from patchbay.snapshot import DeliveryError, write_snapshot
    import pytest as _pytest

    clean_env.setenv("PATCHBAY_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    clean_env.setenv("PATCHBAY_SNAPSHOT_DELIVER_DIR", str(tmp_path / "offsite"))
    path = write_snapshot(load_settings())
    off = tmp_path / "offsite"
    assert (off / "patchbay-latest.html").exists()
    assert (off / path.name).exists()
    assert not list(off.glob(".*.part"))  # no half-written temporaries left

    # an unwritable destination must not cost us the local snapshot
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    clean_env.setenv("PATCHBAY_SNAPSHOT_DELIVER_DIR", str(blocked))
    with _pytest.raises(DeliveryError):
        write_snapshot(load_settings())
    assert len(list((tmp_path / "snaps").glob("patchbay-2*.html"))) >= 1


def test_daily_snapshot_fires_once(clean_env, conn):
    import time as _time
    from patchbay import snapshot as snap
    from patchbay.config import load_settings

    clean_env.setenv("PATCHBAY_SNAPSHOT_AT", "00:00")  # already passed today
    s = load_settings()
    assert snap.due_today(conn, s) is True
    snap.mark_done(conn)
    assert snap.due_today(conn, s) is False           # not twice in one day
    clean_env.setenv("PATCHBAY_SNAPSHOT_AT", "23:59")
    assert snap.due_today(conn, load_settings()) is False  # not yet today
    clean_env.setenv("PATCHBAY_SNAPSHOT_AT", "garbage")
    assert snap.due_today(conn, load_settings()) is False  # malformed = off


def test_snapshot_embeds_firewall_history_scrubbed(clean_env, tmp_path):
    # the latest patchbay-held firewall revision rides along, and the
    # snapshot's own scrubber runs over the already-redacted text anyway
    from tests.test_web import seed

    seed(str(tmp_path / "test.db"))
    import sqlite3

    from patchbay import db as pdb

    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.row_factory = sqlite3.Row
    pdb.init(conn)
    old = "<opnsense><hostname>fw1</hostname></opnsense>"
    new = ("<opnsense><hostname>fw1</hostname>"
           "<community>leftover-snmp-secret</community></opnsense>")
    conn.execute("INSERT INTO config_revisions (device, fetched_at, sha, message, "
                 "author, text) VALUES ('fw1', 1000, 'a', 'older', NULL, ?)", (old,))
    conn.execute("INSERT INTO config_revisions (device, fetched_at, sha, message, "
                 "author, text) VALUES ('fw1', 2000, 'b', 'newer', NULL, ?)", (new,))
    conn.commit()
    conn.close()

    from patchbay.config import load_settings
    from patchbay.snapshot import write_snapshot

    clean_env.setenv("PATCHBAY_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    t = write_snapshot(load_settings()).read_text()
    assert "<h2>Device configs (redacted)</h2>" in t
    assert "hostname&gt;fw1" in t                  # the latest revision is in
    assert "leftover-snmp-secret" not in t         # second scrub layer caught it
