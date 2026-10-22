"""The Alerts rail badge (#118): the engine's count of open alerts, one read
of the alerts table, equal to the Active tab's count wherever the two can
legitimately be compared. Every model is synthetic."""

from __future__ import annotations

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from patchbay import alerting, silences
from patchbay import db as pdb
from patchbay.config import load_settings
from tests.test_web import seed


@pytest.fixture()
def net(clean_env, tmp_path):
    """A seeded database and a client; `poll()` runs the alert lifecycle the
    way a poll does, so the table the badge reads is current."""
    import patchbay.web as web

    path = str(tmp_path / "test.db")
    seed(path)

    class Net:
        db = path
        client = TestClient(web.app)

        @staticmethod
        def sql(stmt, *args):
            c = sqlite3.connect(path)
            c.execute(stmt, args)
            c.commit(); c.close()

        @staticmethod
        def poll():
            c = sqlite3.connect(path)
            c.row_factory = sqlite3.Row
            alerting.run(c, load_settings())
            c.commit(); c.close()

    return Net


def _silence(net, device):
    net.sql("INSERT INTO alert_silences (kind, scope, until, created_by, created_at) "
            "VALUES ('device', ?, NULL, 't', 0)", device)


def _badge(html):
    m = re.search(r'<a class="rail-tab[^"]*" href="/alerts"[^>]*>.*?</a>', html, re.S)
    assert m, "no Alerts entry in the rail"
    b = re.search(r'<span class="rail-badge (\w+)">(\d+)</span>', m.group(0))
    return (b.group(1), int(b.group(2)), m.group(0)) if b else None


def _active_rows(client):
    """The Active tab's rows, counted from its table."""
    return client.get("/alerts").text.count('<td class="muted">')


def _slow_links(net):
    net.sql("UPDATE interfaces SET speed_bps = 10000000 WHERE name IN ('1/0/1', 'vmnic0')")


def test_no_badge_at_zero_on_any_page(net):
    net.poll()
    assert _active_rows(net.client) == 0
    for page in ("/", "/alerts", "/topology", "/ops"):
        html = net.client.get(page).text
        assert _badge(html) is None, page


def test_badge_equals_the_active_tab_and_takes_the_worst_severity(net):
    net.sql("UPDATE devices SET status = 'down' WHERE name = 'sw1'")
    _slow_links(net)                                    # a second, link-category item
    net.poll()
    sev, n, link = _badge(net.client.get("/").text)
    rows = _active_rows(net.client)
    assert (sev, n) == ("crit", rows) and n >= 2
    # the link's own name carries the count: its label is hidden when the
    # rail collapses, and a badge alone says nothing to a screen reader
    assert f'aria-label="Alerts, {n} active, ' in link
    assert 'href="/alerts"' in link and f">{n}</span>" in link
    assert _badge(net.client.get("/alerts?tab=history").text)[:2] == ("crit", n)


def test_badge_is_warn_when_no_open_alert_is_crit(net):
    net.sql("UPDATE interfaces SET speed_bps = 100000000 WHERE name IN ('1/0/1', 'vmnic0')")
    net.poll()
    sev, n, link = _badge(net.client.get("/").text)
    assert (sev, n) == ("warn", 1) == ("warn", _active_rows(net.client))
    assert 'aria-label="Alerts, 1 active"' in link   # nothing critical to name


def test_badge_follows_the_alert_in_and_out(net):
    net.sql("UPDATE devices SET status = 'down' WHERE name = 'sw1'")
    net.poll()
    assert _badge(net.client.get("/").text)[:2] == ("crit", _active_rows(net.client))
    net.sql("UPDATE devices SET status = 'up' WHERE name = 'sw1'")
    net.poll()
    assert _badge(net.client.get("/").text) is None
    assert _active_rows(net.client) == 0


def test_silenced_alerts_leave_the_badge_with_the_tab(net, clean_env):
    net.sql("UPDATE devices SET status = 'down' WHERE name = 'sw1'")
    _slow_links(net)
    net.poll()
    before = _badge(net.client.get("/").text)[1]
    assert before == _active_rows(net.client)

    # a silence from the Active tab: the badge catches up on the next poll,
    # the tab at once; after it, the two agree again
    c = sqlite3.connect(net.db)
    c.row_factory = sqlite3.Row
    silences.add(c, {"kind": "device", "scope": "sw1", "until": None, "reason": None,
                     "created_by": "t", "created_at": pdb.now()})
    c.commit(); c.close()
    net.poll()
    after = _badge(net.client.get("/").text)
    assert after is None or after[1] < before
    assert (after[1] if after else 0) == _active_rows(net.client)

    # everything silenced: the badge is gone, the Silenced tab has them
    for dev in ("sw1", "hyp1"):
        _silence(net, dev)
    net.poll()
    assert _badge(net.client.get("/").text) is None
    assert _active_rows(net.client) == 0


def test_disabled_rule_leaves_the_badge_before_the_next_poll(net):
    net.sql("UPDATE devices SET status = 'down' WHERE name = 'sw1'")
    _slow_links(net)
    net.poll()
    both = _badge(net.client.get("/").text)[1]
    assert both == _active_rows(net.client) >= 2

    # the rule is switched off on the Rules tab: its alerts are hidden at once
    # (the engine only closes them on the next poll), as on the Active tab
    net.sql("UPDATE alert_rules SET enabled = 0 WHERE name = 'device-down'")
    assert _badge(net.client.get("/").text)[1] == both - 1 == _active_rows(net.client)
    net.poll()
    assert _badge(net.client.get("/").text)[1] == both - 1 == _active_rows(net.client)

    net.sql("UPDATE alert_rules SET enabled = 0 WHERE name = 'slow-link'")
    assert _badge(net.client.get("/").text) is None
    assert _active_rows(net.client) == 0


def test_severity_override_colours_the_badge(net):
    net.sql("UPDATE interfaces SET speed_bps = 100000000 WHERE name IN ('1/0/1', 'vmnic0')")
    net.poll()
    assert _badge(net.client.get("/").text)[0] == "warn"
    net.sql("UPDATE alert_rules SET severity = 'crit' WHERE name = 'slow-link'")
    assert _badge(net.client.get("/").text)[0] == "crit"      # before any poll
    assert 'class="dot down"' in net.client.get("/alerts").text


def test_pending_alerts_count_because_the_tab_lists_them(net):
    net.sql("UPDATE interfaces SET speed_bps = 100000000 WHERE name IN ('1/0/1', 'vmnic0')")
    net.sql("INSERT INTO alert_rules (name, params) VALUES ('slow-link', '{\"for\": 3}')")
    net.poll()
    assert "pending" in net.client.get("/alerts").text
    assert _badge(net.client.get("/").text)[1] == _active_rows(net.client) == 1


def _config_change(net, now):
    for sha, msg, age in (("a1", "base", 7200), ("b2", "edit", 3600)):
        net.sql("INSERT INTO config_revisions (device, fetched_at, sha, message, author, "
                "text) VALUES ('sw1', ?, ?, ?, 'x', '')", now - age, sha, msg)


def test_one_shot_events_count_while_the_tab_lists_them(net, clean_env):
    # the engine stores a config change as already cleared; the tab lists it
    # for 24 hours, so the badge does too, in the muted "info" color
    _config_change(net, pdb.now())
    net.poll()
    assert "sw1 config changed" in net.client.get("/alerts").text
    assert _badge(net.client.get("/").text)[:2] == ("info", _active_rows(net.client)) \
        == ("info", 1)

    # past the window the badge drops it, as the tab does when the revision ages
    net.sql("UPDATE alerts SET raised_at = raised_at - 90000")
    assert _badge(net.client.get("/").text) is None

    # switching its rule off hides it at once, on both
    net.sql("UPDATE alerts SET raised_at = raised_at + 90000")
    assert _badge(net.client.get("/").text) is not None
    net.sql("UPDATE alert_rules SET enabled = 0 WHERE name = 'config-changed'")
    assert _badge(net.client.get("/").text) is None
    assert _active_rows(net.client) == 0


def test_an_event_silenced_when_it_fired_never_counts(net):
    _silence(net, "sw1")
    _config_change(net, pdb.now())
    net.poll()
    assert _active_rows(net.client) == 0
    assert _badge(net.client.get("/").text) is None


def test_an_event_follows_its_silence_in_both_directions(net):
    # silenced after it fired: the next poll takes it off the badge, as the
    # tab drops it at once
    _config_change(net, pdb.now())
    net.poll()
    assert _badge(net.client.get("/").text)[1] == _active_rows(net.client) == 1
    _silence(net, "sw1")
    assert _active_rows(net.client) == 0
    net.poll()
    assert _badge(net.client.get("/").text) is None

    # unsilenced again: it is back on both, and no notification replays it
    net.sql("DELETE FROM alert_silences")
    assert _active_rows(net.client) == 1
    net.poll()
    assert _badge(net.client.get("/").text)[:2] == ("info", 1)
    c = sqlite3.connect(net.db)
    assert c.execute("SELECT COUNT(*) FROM alert_events WHERE event = 'raised'").fetchone()[0] == 1
    c.close()


def test_an_event_silenced_when_it_fired_returns_when_unsilenced(net):
    _silence(net, "sw1")
    _config_change(net, pdb.now())
    net.poll()
    assert _badge(net.client.get("/").text) is None
    net.sql("DELETE FROM alert_silences")
    net.poll()
    assert _badge(net.client.get("/").text)[1] == _active_rows(net.client) == 1


def test_held_items_are_tagged_so_the_two_counts_differ_visibly(net):
    net.sql("UPDATE devices SET status = 'down' WHERE name = 'sw1'")
    net.sql("UPDATE interfaces SET oper_status = 'down' WHERE name = 'vmnic0'")
    net.poll()
    page = net.client.get("/alerts").text
    assert page.count("held: sw1 is down") == 1      # the cable, not the device
    overview = net.client.get("/").text
    assert overview.count("held: sw1 is down") == 1
    assert 'class="attn-held"' in overview
    # the badge counts what the engine pages about: tab rows less the held one
    assert _badge(page)[1] == _active_rows(net.client) - 1

    net.sql("UPDATE devices SET status = 'up' WHERE name = 'sw1'")
    net.poll()
    assert "held:" not in net.client.get("/alerts").text
    assert "held:" not in net.client.get("/").text.split("</section>")[0]


def test_badge_read_uses_the_indexes(net):
    c = sqlite3.connect(net.db)
    now = pdb.now()
    events = tuple(n for n, r in alerting.RULES.items() if r.event)
    plan = " ".join(r[3] for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM alerts a WHERE a.state = 'cleared' "
        f"AND a.raised_at >= ? AND a.rule IN ({','.join('?' * len(events))})",
        (now, *events)))
    assert "idx_alerts_raised" in plan
    assert alerting.open_count(c, now) == {}
    c.close()


def test_badge_never_fails_a_page(net, monkeypatch):
    # an unreadable alerts table: the rail renders without the count
    net.sql("DROP TABLE alerts")
    r = net.client.get("/ops")
    assert r.status_code == 200 and _badge(r.text) is None


def test_cables_held_by_a_down_device_are_the_other_difference(net):
    """The engine holds a cable alert while an end's device is down (one
    switch down is one alert, not one per cable), so it never opens one; the
    Active tab, which is the live attention list, still lists the cable."""
    net.sql("UPDATE devices SET status = 'down' WHERE name = 'sw1'")
    net.sql("UPDATE interfaces SET oper_status = 'down' WHERE name = 'vmnic0'")
    net.poll()
    assert "is down (hyp1 vmnic0 reports down)" in net.client.get("/alerts").text
    assert _active_rows(net.client) == 2
    assert _badge(net.client.get("/").text)[:2] == ("crit", 1)
