"""Snapshot generator: redaction correctness and offline generation."""

import os

import httpx
import pytest

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


def test_scrub_config_covers_oxidized_secret_forms():
    """#93: these forms came through with a redaction count of 0 — XML
    elements in a firewall config.xml Oxidized backed up, IOS key chains,
    ISAKMP and OSPF MD5 keys, SNMP host communities, any PEM key block, and
    MikroTik `key=value` pairs whose value the keyword pattern swallowed."""
    cases = {
        "<prv>FAKEPRVB64</prv>": "FAKEPRVB64",
        "<privatekey>FAKEWGKEY=</privatekey>": "FAKEWGKEY",
        "<tls>FAKETLSB64==</tls>": "FAKETLSB64",
        "<apikey>fakeapikey0123</apikey>": "fakeapikey0123",
        "<Key>FAKE-psk</Key>": "FAKE-psk",
        "<ldap_bindpw>FAKE-bind</ldap_bindpw>": "FAKE-bind",
        "  key-string 7 0000FAKE0000": "0000FAKE0000",
        " ip ospf message-digest-key 1 md5 7 1111FAKE1111": "1111FAKE1111",
        "snmp-server host 192.0.2.9 version 2c FakeHostComm": "FakeHostComm",
        "snmp-server host 192.0.2.9 traps version 2c FakeTrapComm": "FakeTrapComm",
        "snmp-server host 192.0.2.9 FakeV1Comm": "FakeV1Comm",
        "crypto isakmp key 0 FakeIsakmpKey address 192.0.2.1": "FakeIsakmpKey",
        " tunnel key 8675309": "8675309",
        "-----BEGIN OpenVPN Static key V1-----\nfeedfacefeedface\n"
        "-----END OpenVPN Static key V1-----": "feedface",
        "set [ find default=yes ] wpa2-pre-shared-key=FakeWifiKey":
            "FakeWifiKey",
    }
    for line, secret in cases.items():
        out, n = scrub_config(line)
        assert secret not in out, line
        assert n >= 1, line


def test_scrub_config_one_line_key_block_does_not_swallow_the_rest():
    cfg = ("a -----BEGIN PRIVATE KEY-----FAKE-----END PRIVATE KEY-----\n"
           "hostname sw1\n")
    out, _ = scrub_config(cfg)
    assert "FAKE" not in out and "hostname sw1" in out


def test_scrub_config_block_state_follows_the_last_marker():
    """An END followed by a new BEGIN on one line stays inside a block."""
    cfg = ("-----BEGIN PRIVATE KEY-----\nFAKE1\n"
           "-----END PRIVATE KEY----- -----BEGIN RSA PRIVATE KEY-----\n"
           "FAKE2\n-----END RSA PRIVATE KEY-----\nhostname sw1")
    out, _ = scrub_config(cfg)
    assert "FAKE1" not in out and "FAKE2" not in out
    assert out.endswith("hostname sw1")


def test_scrub_config_carries_no_stamps_off_host():
    """A stamp of a weak secret is a guessing oracle; the snapshot leaves
    the host, so stored stamps and fresh redactions both become the
    constant placeholder."""
    out, _ = scrub_config("<a><Key>redacted:0123abcd</Key><psk>FAKE</psk></a>")
    assert "0123abcd" not in out and "FAKE" not in out
    assert "redacted:" not in out


def test_scrub_config_leaves_ordinary_lines_alone():
    cfg = ("hostname sw1\n"
           "interface GigabitEthernet0/1\n"
           " description uplink to core\n"
           " switchport mode trunk\n"
           "router ospf 1\n"
           " network 192.0.2.0 0.0.0.255 area 0\n"
           "ntp server 192.0.2.123\n"
           "snmp-server location rack 2\n"
           "<interfaces><lan><if>em0</if><descr>LAN</descr></lan></interfaces>\n"
           "<system><hostname>fw1</hostname><domain>example.net</domain></system>\n"
           "-----BEGIN CERTIFICATE-----")
    out, n = scrub_config(cfg)
    assert out == cfg and n == 0


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
    assert "4 devices, 4 up, 0 down" in t     # rail totals, frozen (#46)
    # the routed view rides along (#50): its own svg, graph, and a VLAN row
    # anchor for its tags to jump to; the two map scripts share the page
    assert 'id="routed"' in t and "const rg = " in t
    assert '"key": "v24"' in t
    assert 'id="vlan-24"' in t
    assert t.count("const SNAPSHOT = true") == 2
    # no oxidized configured -> no configs SECTION (the intro prose mentions
    # the word, so match the heading, not the phrase)
    assert "<h2>Device configs (redacted)</h2>" not in t
    assert "forceSimulation" in t             # d3 inlined
    # no librenms -> no graphs, no error. Match the graph payload specifically:
    # the page also carries an inline SVG favicon, which is a data: URI too.
    assert "data:image/png" not in t
    assert (tmp_path / "snaps" / "patchbay-latest.html").exists()


def test_snapshot_carries_vpn_tunnels(clean_env, tmp_path):
    """Tunnels ride the break-glass file: on the map (build_topology_graph is
    shared) and as a table under the terminating firewall's device section."""
    import sqlite3

    from tests.test_web import seed

    db_path = str(tmp_path / "test.db")
    seed(db_path)
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO tunnels (device, type, name, peer, interface, "
              "status, last_handshake, detail, source, last_seen) VALUES "
              "('fw1', 'wireguard', 'site-b', '192.0.2.200:51820', 'wg1', "
              "'up', strftime('%s','now') - 90, 'allowed 172.16.44.0/24', "
              "'opnsense', strftime('%s','now'))")
    c.commit(); c.close()
    from patchbay.config import load_settings
    from patchbay.snapshot import write_snapshot

    clean_env.setenv("PATCHBAY_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    t = write_snapshot(load_settings()).read_text()
    assert "192.0.2.200:51820" in t
    assert "WireGuard" in t
    assert "before generation" in t          # handshake age is humanized


def test_snapshot_retention(clean_env, tmp_path):
    from tests.test_web import seed

    seed(str(tmp_path / "test.db"))
    from patchbay.config import load_settings
    from patchbay import snapshot as snap

    clean_env.setenv("PATCHBAY_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    clean_env.setenv("PATCHBAY_SNAPSHOT_KEEP", "2")
    d = tmp_path / "snaps"
    d.mkdir()
    # nightlies on the three days before today: a bare 2 is the first of
    # each of the last 2 calendar days, so yesterday's survives with today's
    from datetime import datetime, timedelta
    old = [(datetime.now() - timedelta(days=n)).strftime("patchbay-%Y%m%d-000000.html")
           for n in (3, 2, 1)]
    for name in old:
        (d / name).write_text("old")
    path = snap.write_snapshot(load_settings())
    kept = sorted(p.name for p in d.glob("patchbay-2*.html"))
    assert kept == [old[-1], path.name]


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
           "<community>leftover-snmp-secret</community>"
           "<Key>redacted:0123abcd</Key></opnsense>")
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
    assert "0123abcd" not in t                     # no stamp leaves the host


def _librenms_down(clean_env, monkeypatch, tmp_path, exc=None):
    """A seeded DB with LibreNMS configured, every httpx.Client wired to a
    MockTransport whose handler raises `exc` — no socket is ever opened.
    Returns the list of requests the handler saw."""
    import httpx
    from tests.test_web import seed

    seed(str(tmp_path / "test.db"))
    clean_env.setenv("PATCHBAY_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    clean_env.setenv("LIBRENMS_URL", "http://librenms.example.test")
    clean_env.setenv("LIBRENMS_TOKEN", "t0ken")
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        raise exc or httpx.ConnectError("refused", request=request)

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real(
        *a, **{**k, "transport": httpx.MockTransport(handler)}))
    return seen


def test_snapshot_survives_unreachable_librenms(clean_env, monkeypatch, tmp_path):
    """The outage the break-glass file exists for: LibreNMS is down, so the
    graphs are skipped with a note and the snapshot is still written."""
    seen = _librenms_down(clean_env, monkeypatch, tmp_path)
    from patchbay.config import load_settings
    from patchbay.snapshot import write_snapshot

    path = write_snapshot(load_settings())
    t = path.read_text()
    assert (tmp_path / "snaps" / "patchbay-latest.html").exists()
    assert 'id="dev-sw1"' in t and 'id="dev-hyp1"' in t       # tables intact
    assert "const SNAPSHOT = true" in t                        # topology intact
    assert "Traffic graphs unavailable" in t and "ConnectError" in t
    assert "data:image/png" not in t
    assert "librenms.example.test" not in t and "t0ken" not in t   # no URL or token leaks
    assert len(seen) == 1                      # two linked ports, one attempt


def test_snapshot_graph_timeouts_fail_fast_on_connect_but_wait_on_reads(
        clean_env, monkeypatch, tmp_path):
    """Short connect timeout so a blackholed host costs seconds; a generous
    read timeout so a slow-rendering LibreNMS keeps its graphs. The timeout
    is passed explicitly, never left at the 20 s default."""
    _librenms_down(clean_env, monkeypatch, tmp_path)
    from patchbay import snapshot, web
    from patchbay.config import load_settings

    got = []
    real = web.fetch_graph_image

    def spy(*a, **k):
        got.append(k.get("timeout"))
        return real(*a, **k)

    monkeypatch.setattr(web, "fetch_graph_image", spy)
    snapshot.generate(load_settings())
    assert got == [snapshot.GRAPH_TIMEOUT]
    assert snapshot.GRAPH_TIMEOUT.connect == 3 and snapshot.GRAPH_TIMEOUT.read == 15


def test_alert_snapshot_survives_unreachable_librenms(clean_env, monkeypatch, tmp_path):
    """snapshot-on-critical goes through the same generate()."""
    _librenms_down(clean_env, monkeypatch, tmp_path)
    from patchbay import snapshot
    from patchbay.config import load_settings

    path = snapshot.write_snapshot(load_settings(), alert_keep=5, stamp=1_700_000_000)
    assert path.exists() and "Traffic graphs unavailable" in path.read_text()


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"),
    httpx.ReadTimeout("slow"), httpx.DecodingError("bad gzip"),
    httpx.TooManyRedirects("loop")], ids=lambda e: type(e).__name__)
def test_fetch_graph_image_wraps_every_request_error(clean_env, monkeypatch, tmp_path, exc):
    """httpx.RequestError, not just TransportError: a bad Content-Encoding
    (DecodingError) or a redirect loop is as unusable as a refused connection."""
    from patchbay import web
    from patchbay.config import load_settings

    _librenms_down(clean_env, monkeypatch, tmp_path, exc=exc)
    with pytest.raises(web.GraphSourceError) as ei:
        web.fetch_graph_image(load_settings(), "sw1", "1/0/1", "port_bits")
    assert str(ei.value) == type(exc).__name__


def test_fetch_graph_image_unconfigured_is_no_graph_not_an_error(clean_env, tmp_path):
    from patchbay import web
    from patchbay.config import load_settings

    assert web.fetch_graph_image(load_settings(), "sw1", "1/0/1", "port_bits") is None
