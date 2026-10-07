"""Alert transports (ADR-0003 Decision 3, issue #61): routes, the kuma and
generic presets, retry and the three-strike item, the Transports tab, and
the secret's containment. No test touches the network: every request goes
to an httpx.MockTransport that records it."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from patchbay import alerting, attention, transports
from patchbay import db as pdb

T0 = 1_800_000_000.0
KUMA = "https://kuma.example.com/api/push/s3cr3tPushTok?status=up&msg=OK&ping="
HOOK = "https://hooks.example.com/w/hookSecret99"
SECRETS = ("s3cr3tPushTok", "hookSecret99")


class Recorder:
    """A mock receiver: answers with `status` (or raises `exc`) and keeps
    every request it saw."""

    def __init__(self, status=200, exc=None, body='{"ok":true}'):
        self.status, self.exc, self.body = status, exc, body
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self.exc:
            raise self.exc(f"failed talking to {request.url}", request=request)
        return httpx.Response(self.status, text=self.body)

    @property
    def transport(self):
        return httpx.MockTransport(self)


@pytest.fixture()
def dbp(tmp_path):
    p = str(tmp_path / "t.db")
    c = sqlite3.connect(p)
    pdb.init(c)
    c.commit()
    c.close()
    return p


def _c(p):
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    return c


def _add(p, name, kind, url, enabled=1):
    c = _c(p)
    cur = c.execute("INSERT INTO alert_transports (name, kind, url, enabled) "
                    "VALUES (?,?,?,?)", (name, kind, url, enabled))
    c.commit()
    c.close()
    return cur.lastrowid


def _item(key, rule="stale-source", category="source", severity="warn"):
    return {"key": key, "rule": rule, "category": category, "severity": severity,
            "text": f"{key} text", "href": "/ops"}


def _route(p, rule, route):
    c = _c(p)
    alerting.seed_rules(c)
    c.execute("UPDATE alert_rules SET route = ? WHERE name = ?", (route, rule))
    c.commit()
    c.close()


def _poll(p, items, now, rec, **kw):
    """One cycle the way the poller runs it: evaluate, commit, dispatch."""
    c = _c(p)
    notes = alerting.evaluate(c, items, now=now)
    c.commit()
    c.close()
    d = transports.WebhookDispatcher(p, transport=rec.transport, now=now, **kw)
    alerting.dispatch(notes, d)
    return d


def _events(p, event=None):
    c = _c(p)
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM alert_events" + (" WHERE event = ?" if event else "")
        + " ORDER BY id", (event,) if event else ())]
    c.close()
    return rows


def _transport(p, tid):
    c = _c(p)
    row = dict(c.execute("SELECT * FROM alert_transports WHERE id = ?", (tid,)).fetchone())
    c.close()
    return row


# -- routes ---------------------------------------------------------------

def test_route_format_and_default(conn):
    assert transports.parse_route(None) == "default"
    assert transports.parse_route("none") is None
    assert transports.parse_route("3:crit") == transports.Route(3, "crit")
    assert transports.parse_route("3") == transports.Route(3, "warn")
    assert transports.parse_route("3:loud") is None     # a typo pages nobody
    assert transports.parse_route("kuma") is None

    # fresh install: no transport, no default
    assert transports.default_route(conn) is None
    assert transports.route_label(conn, None) == "default (nowhere)"
    conn.execute("INSERT INTO alert_transports (name, kind, url) VALUES ('k', 'kuma', ?)", (KUMA,))
    conn.execute("INSERT INTO alert_transports (name, kind, url) VALUES ('g', 'generic', ?)", (HOOK,))
    # the first transport at warn+ is the default until the site picks one
    assert transports.default_route(conn) == transports.Route(1, "warn")
    assert transports.route_label(conn, None) == "default (k, warn+)"
    pdb.set_state(conn, transports.DEFAULT_ROUTE_KEY, "2:crit")
    assert transports.route_label(conn, None) == "default (g, crit+)"
    pdb.set_state(conn, transports.DEFAULT_ROUTE_KEY, "none")
    assert transports.default_route(conn) is None
    assert transports.route_label(conn, "9:warn") == "missing transport 9, warn+"


def test_config_changed_style_rules_route_nowhere_by_default():
    # owner decision on #61: an informational rule informs, never pages
    assert alerting.RULES["slow-link"].route == "none"
    assert alerting.RULES["transport-failing"].route is None


# -- kuma -----------------------------------------------------------------

def test_kuma_down_while_active_up_when_clear_every_poll(dbp):
    tid = _add(dbp, "kuma", "kuma", KUMA)
    rec = Recorder()
    _poll(dbp, [], T0, rec)                       # nothing wrong: still pushes
    _poll(dbp, [_item("source:stale")], T0 + 300, rec)
    _poll(dbp, [_item("source:stale")], T0 + 600, rec)   # no new note, still down
    _poll(dbp, [], T0 + 900, rec)

    params = [dict(r.url.params) for r in rec.seen]
    assert [p["status"] for p in params] == ["up", "down", "down", "up"]
    assert all(r.method == "GET" for r in rec.seen)
    assert all("ping" not in p for p in params)   # Kuma's pasted ping= is dropped
    assert params[0]["msg"] == "OK"
    assert params[2]["msg"].startswith("1 alert: [warn] stale-source: source:stale text (for 5 min)")
    assert rec.seen[0].url.path == "/api/push/s3cr3tPushTok"

    # the raise and the clear are history; the steady-state push is not
    ev = _events(dbp, "notified")
    assert [(e["key"], e["detail"]) for e in ev] == [
        ("source:stale", "kuma: pushed down"), ("source:stale", "kuma: pushed up")]
    assert ev[0]["category"] == "source"
    t = _transport(dbp, tid)
    assert (t["failures"], t["last_sent_at"], t["last_error"]) == (0, T0 + 900, None)


def test_kuma_ignores_alerts_below_the_route_severity(dbp):
    tid = _add(dbp, "kuma", "kuma", KUMA)
    c = _c(dbp)
    pdb.set_state(c, transports.DEFAULT_ROUTE_KEY, f"{tid}:crit")
    c.commit(); c.close()
    rec = Recorder()
    _poll(dbp, [_item("source:stale", severity="warn")], T0, rec)
    _poll(dbp, [_item("source:stale", severity="warn"),
                _item("ipam:conflicts", rule="ipam-drift", category="ipam",
                      severity="crit")], T0 + 300, rec)
    assert [r.url.params["status"] for r in rec.seen] == ["up", "down"]
    assert "ipam:conflicts" in rec.seen[1].url.params["msg"]


def test_kuma_link_uses_the_link_base(dbp):
    _add(dbp, "kuma", "kuma", KUMA)
    c = _c(dbp)
    pdb.set_state(c, transports.LINK_BASE_KEY, "https://patchbay.example.com")
    c.commit(); c.close()
    rec = Recorder()
    _poll(dbp, [_item("source:stale")], T0, rec)
    assert rec.seen[0].url.params["msg"].endswith("https://patchbay.example.com/ops")


def test_kuma_refusing_a_bad_token_is_a_failure(dbp):
    tid = _add(dbp, "kuma", "kuma", KUMA)
    _poll(dbp, [], T0, Recorder(status=404, body='{"ok":false,"msg":"Monitor not found"}'))
    assert _transport(dbp, tid)["failures"] == 1
    _poll(dbp, [], T0, Recorder(status=200, body='{"ok": false}'))
    assert _transport(dbp, tid)["failures"] == 2


# -- generic --------------------------------------------------------------

def test_generic_posts_raise_and_clear(dbp):
    _add(dbp, "hook", "generic", HOOK)
    rec = Recorder()
    _poll(dbp, [_item("source:stale")], T0, rec)
    _poll(dbp, [_item("source:stale")], T0 + 300, rec)   # still firing: quiet
    _poll(dbp, [], T0 + 600, rec)
    bodies = [json.loads(r.content) for r in rec.seen]
    assert [r.method for r in rec.seen] == ["POST", "POST"]
    assert [b["event"] for b in bodies] == ["raise", "clear"]
    b = bodies[1]
    assert (b["key"], b["rule"], b["category"], b["severity"], b["text"]) == (
        "source:stale", "stale-source", "source", "warn", "source:stale text")
    assert (b["firing_s"], b["href"], b["source"]) == (600, "/ops", "patchbay")
    assert [e["event"] for e in _events(dbp) if e["event"] == "notified"] == ["notified"] * 2


def test_generic_honors_route_none_and_minimum_severity(dbp):
    _add(dbp, "hook", "generic", HOOK)
    rec = Recorder()
    # slow-link routes nowhere; an info item is under the default warn+
    _poll(dbp, [_item("link:a", rule="slow-link", category="link"),
                _item("x:info", rule="ipam-drift", category="ipam", severity="info")],
          T0, rec)
    assert rec.seen == []


def test_per_rule_override_picks_another_transport(dbp):
    _add(dbp, "kuma", "kuma", KUMA)
    hook = _add(dbp, "hook", "generic", HOOK)
    c = _c(dbp)
    alerting.seed_rules(c)
    c.execute("UPDATE alert_rules SET route = ? WHERE name = 'ipam-drift'", (f"{hook}:warn",))
    c.commit(); c.close()
    rec = Recorder()
    _poll(dbp, [_item("ipam:conflicts", rule="ipam-drift", category="ipam")], T0, rec)
    posts = [r for r in rec.seen if r.method == "POST"]
    [kuma] = [r for r in rec.seen if r.method == "GET"]
    assert len(posts) == 1 and kuma.url.params["status"] == "up"


# -- failure, retry, three strikes ----------------------------------------

def test_failed_delivery_retries_next_cycle_then_raises_a_source_item(dbp):
    tid = _add(dbp, "hook", "generic", HOOK)
    down = Recorder(status=500, body="boom")
    _poll(dbp, [_item("source:stale")], T0, down)
    [failed] = _events(dbp, "delivery_failed")
    assert failed["detail"] == "hook: HTTP 500 boom"
    assert _transport(dbp, tid)["failures"] == 1

    # the raise waits in the outbox and is retried, not lost
    _poll(dbp, [_item("source:stale")], T0 + 300, down)
    _poll(dbp, [_item("source:stale")], T0 + 600, down)
    assert len(down.seen) == 3
    assert _transport(dbp, tid)["failures"] == 3

    c = _c(dbp)
    items, checked = attention.attention_items(c, _settings())
    [it] = [i for i in items if i["rule"] == "transport-failing"]
    assert it["key"] == f"source:transport:{tid}" and it["category"] == "source"
    assert "hook failing (3 polls): HTTP 500 boom" in it["text"]
    assert "alert transports delivering" in checked
    c.close()

    up = Recorder()
    _poll(dbp, [_item("source:stale")], T0 + 900, up)
    [body] = [json.loads(r.content) for r in up.seen]
    assert (body["event"], body["key"]) == ("raise", "source:stale")
    assert _transport(dbp, tid)["failures"] == 0
    c = _c(dbp)
    assert json.loads(pdb.get_state(c, transports.OUTBOX_KEY)) == []
    items, _ = attention.attention_items(c, _settings())
    assert not [i for i in items if i["rule"] == "transport-failing"]
    c.close()


def test_failed_receiver_is_not_hammered_within_a_cycle(dbp):
    _add(dbp, "hook", "generic", HOOK)
    down = Recorder(exc=httpx.ConnectError)
    _poll(dbp, [_item("a:1"), _item("a:2"), _item("a:3")], T0, down)
    assert len(down.seen) == 1                    # the rest wait for next cycle
    c = _c(dbp)
    assert len(json.loads(pdb.get_state(c, transports.OUTBOX_KEY))) == 3
    c.close()


def test_outbox_is_capped(dbp, monkeypatch):
    monkeypatch.setattr(transports, "OUTBOX_MAX", 2)
    _add(dbp, "hook", "generic", HOOK)
    _poll(dbp, [_item("a:1"), _item("a:2"), _item("a:3")], T0, Recorder(status=503))
    c = _c(dbp)
    kept = json.loads(pdb.get_state(c, transports.OUTBOX_KEY))
    assert [e["note"]["key"] for e in kept] == ["a:2", "a:3"]   # newest kept
    c.close()


def test_disabled_transport_sends_nothing(dbp):
    _add(dbp, "kuma", "kuma", KUMA, enabled=0)
    rec = Recorder()
    _poll(dbp, [_item("source:stale")], T0, rec)
    assert rec.seen == []


def test_slow_receiver_is_bounded_by_timeout_and_cycle_budget(dbp):
    # every request carries a timeout...
    assert transports.TIMEOUT.read and transports.TIMEOUT.connect
    assert transports._client(None).timeout.read == transports.TIMEOUT.read
    # ...and a timed-out receiver is a failed delivery, not a hung poll
    k1 = _add(dbp, "kuma", "kuma", KUMA)
    hook = _add(dbp, "hook", "generic", HOOK)
    _route(dbp, "stale-source", f"{hook}:warn")
    ticks = iter([0.0, 0.0, 25.0, 25.0, 25.0, 25.0])
    rec = Recorder(exc=httpx.ReadTimeout)
    d = _poll(dbp, [_item("source:stale")], T0, rec, clock=lambda: next(ticks), budget=20)
    assert len(rec.seen) == 1                     # kuma tried; budget then spent
    assert _transport(dbp, k1)["last_error"].startswith("ReadTimeout")
    assert _transport(dbp, hook)["failures"] == 0    # deferred is not failed
    assert any("deferred" in line for line in d.lines)
    c = _c(dbp)
    [queued] = json.loads(pdb.get_state(c, transports.OUTBOX_KEY))
    assert queued["t"] == hook
    c.close()


# -- secrets --------------------------------------------------------------

def test_mask_url_shows_only_scheme_and_host():
    assert transports.mask_url(KUMA) == "https://kuma.example.com/…"
    assert transports.mask_url("http://user:pw@192.0.2.10:3001/x?y=z") == "http://192.0.2.10:3001/…"


def test_secret_never_reaches_errors_events_or_poll_lines(dbp):
    k = _add(dbp, "kuma", "kuma", KUMA)
    h = _add(dbp, "hook", "generic", HOOK)
    _route(dbp, "ipam-drift", f"{h}:warn")
    # httpx puts the URL in its own messages; Recorder does the same
    d = _poll(dbp, [_item("source:stale"),
                    _item("ipam:conflicts", rule="ipam-drift", category="ipam")],
              T0, Recorder(exc=httpx.ConnectError))
    c = _c(dbp)
    stored = json.dumps([dict(r) for r in c.execute(
        "SELECT * FROM alert_events")]) + json.dumps(
        [(t["last_error"], t["last_result"]) for t in
         c.execute("SELECT * FROM alert_transports")]) + "\n".join(d.lines)
    c.close()
    assert "<redacted>" in _transport(dbp, k)["last_error"]
    assert "<redacted>" in _transport(dbp, h)["last_error"]
    for s in SECRETS:
        assert s not in stored


def test_receiver_echoing_the_url_is_redacted(dbp):
    tid = _add(dbp, "hook", "generic", HOOK)
    _poll(dbp, [_item("source:stale")], T0, Recorder(status=400, body=f"bad hook {HOOK}"))
    assert "hookSecret99" not in _transport(dbp, tid)["last_error"]


def _settings():
    from patchbay.config import load_settings
    return load_settings()


def test_secret_never_in_a_snapshot(clean_env, tmp_path):
    from tests.test_web import seed

    seed(str(tmp_path / "test.db"))
    _add(str(tmp_path / "test.db"), "kuma", "kuma", KUMA)
    _add(str(tmp_path / "test.db"), "hook", "generic", HOOK)
    from patchbay.snapshot import generate

    html = generate(_settings())
    for s in SECRETS:
        assert s not in html
    assert "kuma.example.com" not in html and "hooks.example.com" not in html


# -- the Transports tab ---------------------------------------------------

def test_transports_tab_add_edit_toggle_test_delete(clean_env, tmp_path, monkeypatch):
    import patchbay.web as web

    rec = Recorder()
    monkeypatch.setattr(transports, "HTTP_TRANSPORT", rec.transport)
    client = TestClient(web.app)
    form = {"content-type": "application/x-www-form-urlencoded"}
    page = client.get("/alerts?tab=transports").text
    assert "No transports yet" in page

    def post(path, **fields):
        return client.post(path, headers=form, content=str(httpx.QueryParams(fields)),
                           follow_redirects=False)

    r = post("/alerts/transports", name="kuma", kind="kuma", url=KUMA)
    assert r.status_code == 303 and r.headers["location"] == "/alerts?tab=transports"
    page = client.get("/alerts?tab=transports").text
    assert "https://kuma.example.com/…" in page
    for s in SECRETS:
        assert s not in page

    # bad input names the problem, never echoes the URL
    bad = post("/alerts/transports", name="x", kind="slack", url=HOOK)
    assert bad.status_code == 400 and "hookSecret99" not in bad.text
    assert post("/alerts/transports", name="x", kind="generic",
                url="file:///etc/passwd").status_code == 400
    assert post("/alerts/transports", name="kuma", kind="generic", url=HOOK).status_code == 409

    # a blank URL on edit keeps the stored secret
    assert post("/alerts/transports/1", name="kuma-main", kind="kuma", url="").status_code == 303
    dbp = str(tmp_path / "test.db")
    t = _transport(dbp, 1)
    assert (t["name"], t["url"]) == ("kuma-main", KUMA)

    # the test button sends one sample and records the result
    assert post("/alerts/transports/1/test").status_code == 303
    assert rec.seen[-1].url.params["msg"].startswith("patchbay test: OK")
    assert _transport(dbp, 1)["last_result"].startswith("test: HTTP 200")
    assert "test: HTTP 200" in client.get("/alerts?tab=transports").text

    assert post("/alerts/transports/1/enabled", enabled="0").status_code == 303
    assert _transport(dbp, 1)["enabled"] == 0
    assert "(off)" in client.get("/alerts?tab=transports").text
    post("/alerts/transports/1/enabled", enabled="1")

    # routes: a per-rule override and an explicit default, then delete
    post("/alerts/transports", name="hook", kind="generic", url=HOOK)
    assert post("/alerts/routes", **{"default": "1:crit", "route:ipam-drift": "2:warn",
                                     "route:stale-source": "",
                                     "link_base": "https://patchbay.example.com/"}
                ).status_code == 303
    c = _c(dbp)
    assert c.execute("SELECT route FROM alert_rules WHERE name='ipam-drift'").fetchone()[0] == "2:warn"
    assert pdb.get_state(c, transports.DEFAULT_ROUTE_KEY) == "1:crit"
    assert pdb.get_state(c, transports.LINK_BASE_KEY) == "https://patchbay.example.com"
    c.close()
    assert post("/alerts/routes", **{"route:ipam-drift": "7:loud"}).status_code == 400

    assert post("/alerts/transports/2/delete").status_code == 303
    c = _c(dbp)
    # the rule that named the deleted transport falls back to the default
    assert c.execute("SELECT route FROM alert_rules WHERE name='ipam-drift'").fetchone()[0] is None
    c.close()


def test_poll_dispatches_through_the_transports(clean_env, tmp_path, monkeypatch):
    import patchbay.web as web

    rec = Recorder()
    monkeypatch.setattr(transports, "HTTP_TRANSPORT", rec.transport)
    client = TestClient(web.app)
    client.get("/alerts")   # creates the schema
    _add(str(tmp_path / "test.db"), "kuma", "kuma", KUMA)
    lines = client.post("/ops/poll").json()["lines"]
    assert any(line.startswith("[ok]   notify kuma: up") for line in lines)
    assert rec.seen and rec.seen[0].url.params["status"] == "up"


def test_poll_output_never_carries_the_url(clean_env, tmp_path, monkeypatch):
    # the poll's lines are printed by the poller and shown on /ops: a
    # receiver that fails with the URL in its error must not leak it there
    import patchbay.web as web

    monkeypatch.setattr(transports, "HTTP_TRANSPORT",
                        Recorder(exc=httpx.ConnectError).transport)
    client = TestClient(web.app)
    client.get("/alerts")
    _add(str(tmp_path / "test.db"), "kuma", "kuma", KUMA)
    lines = client.post("/ops/poll").json()["lines"]
    assert any(line.startswith("[warn] notify kuma: ConnectError") for line in lines)
    for s in SECRETS:
        assert s not in "\n".join(lines)
        assert s not in client.get("/ops").text
        assert s not in client.get("/alerts").text
