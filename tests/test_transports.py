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


def test_config_changed_style_rules_route_nowhere_by_default(conn):
    # owner decision on #61: an informational rule informs, never pages
    alerting.seed_rules(conn)
    routes = transports.rule_routes(conn)
    assert routes["config-changed"] == routes["slow-link"] == "none"
    assert routes["transport-failing"] is None
    assert alerting.RULES["transport-failing"].category == "source"


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


# -- review round 1: redaction edge cases, wall-clock limit, delete ---------

def _echo(body):
    """A receiver that fails and echoes `body(request)` back."""
    def handler(request):
        return httpx.Response(400, text=body(request))
    return httpx.Client(transport=httpx.MockTransport(handler))


def _one(url, body, kind="generic"):
    t = {"kind": kind, "url": url, "name": "t"}
    with _echo(body) as client:
        ok, result = transports._request(client, t, payload={})
    assert not ok
    return result


@pytest.mark.parametrize("url, secret", [
    ("https://hooks.example.com/hook?token=qTok12345", "qTok12345"),   # query value
    ("https://hooks.example.com/w/%7Etok%40en99", "%7Etok%40en99"),    # raw encoded
    ("https://hooks.example.com/w/%7Etok%40en99", "~tok@en99"),        # decoded
    ("https://user:pw0rdSecret@hooks.example.com/x", "pw0rdSecret"),   # password
    ("https://hooks.example.com/hook/ab12", "ab12"),                   # short token
])
def test_redaction_covers_each_url_part(url, secret):
    assert secret not in _one(url, lambda r: f"bad credential {secret}")


def test_redaction_happens_before_truncation():
    # a URL cut mid-token by the 120-character snippet must not leave the
    # token's front half behind
    body = "x" * 80 + " " + HOOK          # 120 characters falls inside the token
    assert "hookSec" in body[:120] and "hookSecret99" not in body[:120]
    result = _one(HOOK, lambda r: body)
    assert "hookSec" not in result and result.endswith("<redacted>")


def test_kuma_redacts_the_rebuilt_query_it_sent():
    kuma = "https://kuma.example.com/api/push/kTok42?ping=&msg=OK&status=up"
    t = {"kind": "kuma", "url": kuma, "name": "k"}
    with _echo(lambda r: f"no monitor at {r.url}") as client:
        ok, result = transports._request(client, t, status="down", msg="1 alert")
    assert not ok and "kTok42" not in result


class _Trickle(httpx.SyncByteStream):
    def __init__(self, delay):
        self.delay = delay

    def __iter__(self):
        import time
        while True:              # never finishes
            if self.delay:
                time.sleep(self.delay)
            yield b"x" * (1 if self.delay else 1024)


def _stream_client(delay):
    return httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=_Trickle(delay))))


def test_trickling_receiver_hits_the_wall_clock_limit(monkeypatch):
    import time
    monkeypatch.setattr(transports, "REQUEST_S", 0.3)
    t = {"kind": "generic", "url": HOOK, "name": "g"}
    start = time.monotonic()
    with _stream_client(0.02) as client:
        ok, result = transports._request(client, t, payload={})
    assert time.monotonic() - start < 2
    assert not ok and result.startswith("_Deadline")


def test_endless_body_is_capped():
    t = {"kind": "generic", "url": HOOK, "name": "g"}
    with _stream_client(0) as client:
        ok, result = transports._request(client, t, payload={})
    assert ok and result.startswith("HTTP 200 xxx") and len(result) < 200


def test_kuma_test_button_pushes_down_while_held_down(dbp):
    tid = _add(dbp, "kuma", "kuma", KUMA)
    _poll(dbp, [_item("source:stale")], T0, Recorder())
    rec = Recorder()
    c = _c(dbp)
    ok, _ = transports.send_test(c, tid, transport=rec.transport)
    c.close()
    assert ok
    assert rec.seen[-1].url.params["status"] == "down"
    assert rec.seen[-1].url.params["msg"].startswith("patchbay test: 1 alert")


@pytest.mark.parametrize("default", ["1:crit", "1"])
def test_delete_clears_routes_default_and_outbox(clean_env, tmp_path, default):
    import patchbay.web as web

    client = TestClient(web.app)
    client.get("/alerts")
    dbp = str(tmp_path / "test.db")
    tid = _add(dbp, "hook", "generic", HOOK)
    _route(dbp, "ipam-drift", str(tid))                  # a bare-id route
    _poll(dbp, [_item("source:stale")], T0, Recorder(status=503))
    c = _c(dbp)
    pdb.set_state(c, transports.DEFAULT_ROUTE_KEY, default)
    assert json.loads(pdb.get_state(c, transports.OUTBOX_KEY))
    c.commit(); c.close()

    assert client.post(f"/alerts/transports/{tid}/delete",
                       follow_redirects=False).status_code == 303
    c = _c(dbp)
    assert c.execute("SELECT route FROM alert_rules WHERE name='ipam-drift'").fetchone()[0] is None
    assert pdb.get_state(c, transports.DEFAULT_ROUTE_KEY) is None
    assert json.loads(pdb.get_state(c, transports.OUTBOX_KEY)) == []
    c.close()
    # SQLite hands the freed id to the next transport, which starts clean
    assert _add(dbp, "new", "generic", HOOK) == tid
    rec = Recorder()
    _poll(dbp, [_item("source:stale")], T0 + 300, rec)
    assert rec.seen == []


# -- integration with #60: escalate, disabled rules, the Rules tab ----------

def test_generic_sends_escalate_as_its_own_event(dbp):
    _add(dbp, "hook", "generic", HOOK)
    rec = Recorder()
    _poll(dbp, [_item("source:stale", severity="warn")], T0, rec)
    _poll(dbp, [_item("source:stale", severity="crit")], T0 + 300, rec)
    bodies = [json.loads(r.content) for r in rec.seen]
    assert [(b["event"], b["severity"]) for b in bodies] == [
        ("raise", "warn"), ("escalate", "crit")]


def test_kuma_crit_route_goes_down_on_escalation_to_crit(dbp):
    tid = _add(dbp, "kuma", "kuma", KUMA)
    c = _c(dbp)
    pdb.set_state(c, transports.DEFAULT_ROUTE_KEY, f"{tid}:crit")
    c.commit(); c.close()
    rec = Recorder()
    _poll(dbp, [_item("source:stale", severity="warn")], T0, rec)
    _poll(dbp, [_item("source:stale", severity="crit")], T0 + 300, rec)
    _poll(dbp, [_item("source:stale", severity="crit")], T0 + 600, rec)
    assert [r.url.params["status"] for r in rec.seen] == ["up", "down", "down"]
    [ev] = _events(dbp, "notified")
    assert ev["detail"] == "kuma: pushed down" and ev["severity"] == "crit"


def test_disabling_a_rule_clears_generic_and_releases_kuma(dbp):
    _add(dbp, "hook", "generic", HOOK)
    kuma = _add(dbp, "kuma", "kuma", KUMA)
    _route(dbp, "stale-source", "2:warn")       # held down on kuma
    _route(dbp, "ipam-drift", "1:warn")         # posted to the hook
    items = [_item("source:stale"),
             _item("ipam:conflicts", rule="ipam-drift", category="ipam")]
    rec = Recorder()
    _poll(dbp, items, T0, rec)
    assert [r.method for r in rec.seen] == ["POST", "GET"]
    assert rec.seen[1].url.params["status"] == "down"

    c = _c(dbp)
    c.execute("UPDATE alert_rules SET enabled = 0")
    c.commit(); c.close()
    rec = Recorder()
    _poll(dbp, items, T0 + 300, rec)
    # #110: the generic receiver heard the raise, so it hears a clear that
    # says why (the condition may still hold); Kuma follows state and is
    # released on the same push
    assert [r.method for r in rec.seen] == ["POST", "GET"]
    body = json.loads(rec.seen[0].content)
    assert (body["event"], body["key"], body["detail"]) == (
        "clear", "ipam:conflicts", "rule disabled")
    assert rec.seen[1].url.params["status"] == "up"
    assert [e["detail"] for e in _events(dbp, "cleared")] == ["rule disabled"] * 2


def test_rules_tab_shows_route_labels(clean_env, tmp_path):
    import patchbay.web as web

    client = TestClient(web.app)
    client.get("/alerts")
    dbp = str(tmp_path / "test.db")
    tid = _add(dbp, "kuma", "kuma", KUMA)
    _route(dbp, "ipam-drift", f"{tid}:crit")
    page = client.get("/alerts?tab=rules").text
    assert "default (kuma, warn+)" in page
    assert "kuma, crit+" in page
    assert "nowhere (attention list only)" in page
    assert f">{tid}:crit<" not in page


# -- #80: overlapping dispatches keep every queued note ---------------------

def test_interleaved_dispatches_keep_every_queued_note(dbp, monkeypatch):
    # the inner dispatch finds the lease held and queues behind it (#110)
    monkeypatch.setattr(transports, "LEASE_WAIT_S", 0)
    _add(dbp, "hook", "generic", HOOK)
    inner_done = []

    def handler(request):
        # while the outer dispatch's request is in flight (no transaction
        # open), a second dispatch runs to completion and queues its note
        if not inner_done:
            inner_done.append(True)
            inner = transports.WebhookDispatcher(
                dbp, transport=httpx.MockTransport(handler), now=T0)
            inner.send([_note("b:2")])
        return httpx.Response(500, text="boom")

    outer = transports.WebhookDispatcher(
        dbp, transport=httpx.MockTransport(handler), now=T0)
    outer.send([_note("a:1")])

    c = _c(dbp)
    kept = json.loads(pdb.get_state(c, transports.OUTBOX_KEY))
    c.close()
    assert sorted(e["note"]["key"] for e in kept) == ["a:1", "b:2"]


def _note(key):
    return alerting.Notification(kind="raise", key=key, rule="stale-source",
                                 severity="warn", text=f"{key} text", href="/ops",
                                 raised_at=T0, route="1:warn")


# -- #81: per-delivery build errors, hex-case redaction ---------------------

def _unbuildable(monkeypatch, bad):
    """Make `bad` pass validate_url but fail while the request is built, as a
    stored URL httpx or kuma_url rejects would."""
    real = transports.kuma_url

    def kuma_url(url, *a, **kw):
        if url == bad:
            raise ValueError(f"cannot build {url}")
        return real(url, *a, **kw)
    monkeypatch.setattr(transports, "kuma_url", kuma_url)
    assert transports.validate_url(bad) is None


def test_unbuildable_url_fails_one_delivery_not_the_cycle(dbp, monkeypatch):
    bad = "https://kuma.example.com/api/push/badSecret77"
    _unbuildable(monkeypatch, bad)
    bad_id = _add(dbp, "bad", "kuma", bad)
    _add(dbp, "good", "generic", HOOK)
    _route(dbp, "stale-source", "2:warn")
    good = Recorder()
    d = _poll(dbp, [_item("source:stale")], T0, good)
    assert len(good.seen) == 1                       # the healthy one still delivered
    row = _transport(dbp, bad_id)
    assert row["failures"] == 1 and "ValueError" in row["last_error"]
    assert "badSecret77" not in row["last_error"]
    assert not any("badSecret77" in line for line in d.lines)
    assert not any("badSecret77" in (e["detail"] or "") for e in _events(dbp))


def test_redaction_ignores_hex_case_of_percent_escapes():
    url = "https://hooks.example.com/w/caf%C3%A9Tok"
    out = _one(url, lambda r: "bad caf%c3%a9Tok and 100%c3 literal")
    assert "caf%c3%a9Tok" not in out
    assert "100%c3 literal" in out            # unrelated text is untouched


def test_overlapping_dispatches_failing_the_same_retry_keep_one_copy(dbp, monkeypatch):
    monkeypatch.setattr(transports, "LEASE_WAIT_S", 0)
    _add(dbp, "hook", "generic", HOOK)
    c = _c(dbp)
    # an entry stored before #110 carries `tries`; it is read the same way
    pdb.set_state(c, transports.OUTBOX_KEY, json.dumps(
        [{"t": 1, "note": transports._note_dict(_note("a:1")), "tries": 1}]))
    c.commit()
    c.close()
    inner_done = []

    def handler(request):
        if not inner_done:
            inner_done.append(True)
            transports.WebhookDispatcher(
                dbp, transport=httpx.MockTransport(handler), now=T0).send([])
        return httpx.Response(500, text="boom")

    transports.WebhookDispatcher(
        dbp, transport=httpx.MockTransport(handler), now=T0).send([])
    c = _c(dbp)
    kept = json.loads(pdb.get_state(c, transports.OUTBOX_KEY))
    c.close()
    assert [e["note"]["key"] for e in kept] == ["a:1"]


# -- #110: delivery order, the dispatch lease, a locked outcome write ----------

def _kind_note(kind, at, key="link:sw1:1"):
    return alerting.Notification(kind=kind, key=key, rule="link-down", severity="warn",
                                 text="x", href="/ops", raised_at=T0, route="1:warn",
                                 noted_at=at)


def test_a_clear_stored_before_its_raise_is_still_sent_after_it(dbp, monkeypatch):
    # the clear's dispatch read the outbox before the raise's dispatch stored
    # the raise; the receiver must still hear raise, then clear
    monkeypatch.setattr(transports, "LEASE_WAIT_S", 0)
    _add(dbp, "hook", "generic", HOOK)
    inner_done = []

    def handler(request):
        if not inner_done:
            inner_done.append(True)
            transports.WebhookDispatcher(
                dbp, transport=httpx.MockTransport(handler), now=T0).send(
                [_kind_note("raise", T0)])
        return httpx.Response(503)

    transports.WebhookDispatcher(
        dbp, transport=httpx.MockTransport(handler), now=T0 + 300).send(
        [_kind_note("clear", T0 + 300)])
    c = _c(dbp)
    kept = json.loads(pdb.get_state(c, transports.OUTBOX_KEY))
    c.close()
    assert [e["note"]["kind"] for e in kept] == ["raise", "clear"]
    assert all("tries" not in e for e in kept)
    rec = Recorder()
    transports.WebhookDispatcher(dbp, transport=rec.transport, now=T0 + 600).send([])
    assert [json.loads(r.content)["event"] for r in rec.seen] == ["raise", "clear"]


def test_the_lease_is_one_holder_and_expires(dbp, monkeypatch):
    c = _c(dbp)
    assert transports._claim(c, "a")
    assert not transports._claim(c, "b")
    assert transports._claim(c, "a")              # the holder may renew
    transports._release(c, "b")                   # not b's to release
    assert not transports._claim(c, "b")
    transports._release(c, "a")
    assert transports._claim(c, "b")
    monkeypatch.setattr(transports, "LEASE_TTL_S", -1)
    assert transports._claim(c, "b")              # b's lease is already stale...
    assert transports._claim(c, "c")              # ...so c takes it over
    c.close()


def test_overlapping_dispatches_take_turns(dbp):
    import threading

    _add(dbp, "hook", "generic", HOOK)
    gate, started = threading.Event(), threading.Event()
    seen = []

    def handler(request):
        key = json.loads(request.content)["key"]
        seen.append(key)
        if key == "a:1":
            started.set()
            gate.wait(5)
        return httpx.Response(200)

    def dispatch(note):
        transports.WebhookDispatcher(
            dbp, transport=httpx.MockTransport(handler), now=T0).send([note])

    first = threading.Thread(target=dispatch, args=(_note("a:1"),))
    first.start()
    assert started.wait(5)
    second = threading.Thread(target=dispatch, args=(_note("b:2"),))
    second.start()
    second.join(0.5)
    assert seen == ["a:1"]                        # b waits for a's lease
    gate.set()
    first.join(5)
    second.join(5)
    assert seen == ["a:1", "b:2"]


def test_a_locked_outcome_write_loses_nothing(dbp, monkeypatch):
    # the database stays locked through the outcome write: the failed
    # raise, its history, and the transport's failure count wait in a file
    # and land with the next cycle, which also retries the raise
    monkeypatch.setattr(transports, "OUTCOME_TRIES", 1)
    monkeypatch.setattr(transports, "LEASE_TTL_S", -1)   # the release fails too
    monkeypatch.setattr(transports, "DB_TIMEOUT_S", 0.05)
    tid = _add(dbp, "hook", "generic", HOOK)
    blocker = sqlite3.connect(dbp, check_same_thread=False)

    def locked(request):
        blocker.execute("BEGIN IMMEDIATE")       # a poll takes the write lock
        return httpx.Response(503)

    d = transports.WebhookDispatcher(dbp, transport=httpx.MockTransport(locked), now=T0)
    d.send([_note("a:1")])
    assert any("stayed locked" in line for line in d.lines)
    blocker.rollback()
    blocker.close()
    assert len(transports._pending_paths(dbp)) == 1
    assert _events(dbp) == [] and _transport(dbp, tid)["failures"] == 0

    rec = Recorder()
    transports.WebhookDispatcher(dbp, transport=rec.transport, now=T0 + 300).send([])
    assert [json.loads(r.content)["key"] for r in rec.seen] == ["a:1"]
    assert [e["event"] for e in _events(dbp)] == ["delivery_failed", "notified"]
    assert transports._pending_paths(dbp) == []
    c = _c(dbp)
    assert json.loads(pdb.get_state(c, transports.OUTBOX_KEY)) == []
    c.close()


def test_request_s_bounds_the_wait_for_headers(monkeypatch):
    import time

    monkeypatch.setattr(transports, "REQUEST_S", 0.3)

    def slow(request):
        time.sleep(1.0)                            # headers come late
        return httpx.Response(200)

    t = {"kind": "generic", "url": HOOK, "name": "g"}
    start = time.monotonic()
    with httpx.Client(transport=httpx.MockTransport(slow)) as client:
        ok, result = transports._request(client, t, payload={})
    assert time.monotonic() - start < 0.8
    assert not ok and result.startswith("_Deadline")


def test_a_database_locked_from_the_start_fails_closed(dbp, monkeypatch):
    # no lease, even because the database never answered: nothing is sent
    # and the shared queue is not touched; the notes wait for the next
    # lease holder, which delivers them in order
    monkeypatch.setattr(transports, "LEASE_WAIT_S", 0)
    monkeypatch.setattr(transports, "OUTCOME_TRIES", 1)
    monkeypatch.setattr(transports, "DB_TIMEOUT_S", 0.05)
    _add(dbp, "hook", "generic", HOOK)
    c = _c(dbp)
    queued = [transports._entry(1, _kind_note("raise", T0))]
    pdb.set_state(c, transports.OUTBOX_KEY, json.dumps(queued))
    c.commit(); c.close()
    blocker = sqlite3.connect(dbp)
    blocker.execute("BEGIN IMMEDIATE")
    rec = Recorder()
    d = transports.WebhookDispatcher(dbp, transport=rec.transport, now=T0 + 300)
    d.send([_kind_note("clear", T0 + 300)])
    blocker.rollback()
    blocker.close()
    assert rec.seen == []
    assert any("database is busy" in line for line in d.lines)
    c = _c(dbp)
    assert json.loads(pdb.get_state(c, transports.OUTBOX_KEY)) == queued
    c.close()
    assert len(transports._pending_paths(dbp)) == 1     # this cycle's clear
    transports.WebhookDispatcher(dbp, transport=rec.transport, now=T0 + 600).send([])
    assert [json.loads(r.content)["event"] for r in rec.seen] == ["raise", "clear"]
    assert transports._pending_paths(dbp) == []


def test_a_burst_over_the_outbox_cap_is_all_sent(dbp, monkeypatch):
    # the cap bounds what waits for a dead receiver, not what one cycle sends
    monkeypatch.setattr(transports, "OUTBOX_MAX", 2)
    _add(dbp, "hook", "generic", HOOK)
    rec = Recorder()
    _poll(dbp, [_item(f"a:{i}") for i in range(5)], T0, rec)
    assert len(rec.seen) == 5


# -- #110 review round 1: lease waiters, side-file hygiene ---------------------

def _cleared_alert(p, key="link:sw1:1"):
    c = _c(p)
    c.execute("INSERT INTO alerts (key, rule, category, severity, state, raised_at, "
              "cleared_at, last_notified_at) VALUES (?, 'link-down', 'link', 'warn', "
              "'cleared', ?, ?, ?)", (key, T0, T0 + 30, T0))
    c.commit()
    c.close()


@pytest.mark.parametrize("first", ["raise", "clear"])
def test_lease_waiters_never_send_a_stale_raise_after_its_clear(dbp, first):
    # a third dispatch holds the lease; the raise's and the clear's
    # dispatches both wait, and either may take it first. The raise's alert
    # has cleared by then, so it is dropped wherever it lands.
    import threading
    import time

    _add(dbp, "hook", "generic", HOOK)
    _cleared_alert(dbp)
    c = _c(dbp)
    pdb.set_state(c, transports.LEASE_KEY,
                  json.dumps({"owner": "H", "until": time.time() + 60}))
    c.commit(); c.close()
    seen, lock = [], threading.Lock()

    def ok(request):
        with lock:
            seen.append(json.loads(request.content)["event"])
        return httpx.Response(200)

    def go(kind, at):
        transports.WebhookDispatcher(dbp, transport=httpx.MockTransport(ok),
                                     now=at).send([_kind_note(kind, at)])

    order = [("raise", T0), ("clear", T0 + 30)]
    if first == "clear":
        order.reverse()
    threads = [threading.Thread(target=go, args=a) for a in order]
    threads[0].start(); time.sleep(0.1); threads[1].start(); time.sleep(0.1)
    c = _c(dbp)
    c.execute("DELETE FROM app_state WHERE key = ?", (transports.LEASE_KEY,))
    c.commit(); c.close()
    for t in threads:
        t.join(10)
    assert seen == ["clear"]
    [ev] = _events(dbp, "dropped")
    assert (ev["detail"], ev["key"]) == ("cleared since", "link:sw1:1")


def test_a_queued_raise_whose_alert_cleared_is_dropped(dbp):
    _add(dbp, "hook", "generic", HOOK)
    _cleared_alert(dbp)
    c = _c(dbp)
    pdb.set_state(c, transports.OUTBOX_KEY, json.dumps(
        [transports._entry(1, _kind_note("raise", T0))]))
    c.commit(); c.close()
    rec = Recorder()
    transports.WebhookDispatcher(dbp, transport=rec.transport, now=T0 + 60).send(
        [_kind_note("clear", T0 + 30)])
    assert [json.loads(r.content)["event"] for r in rec.seen] == ["clear"]


def _locked_cycle(dbp, monkeypatch):
    """One cycle whose outcome the database refuses: a held-over file."""
    monkeypatch.setattr(transports, "OUTCOME_TRIES", 1)
    monkeypatch.setattr(transports, "LEASE_TTL_S", -1)
    monkeypatch.setattr(transports, "DB_TIMEOUT_S", 0.05)
    blocker = sqlite3.connect(dbp, check_same_thread=False)

    def locked(request):
        blocker.execute("BEGIN IMMEDIATE")
        return httpx.Response(503)

    transports.WebhookDispatcher(dbp, transport=httpx.MockTransport(locked),
                                 now=T0).send([_note("a:1")])
    blocker.rollback()
    blocker.close()
    [path] = transports._pending_paths(dbp)
    return path


def test_held_over_outcome_is_private(dbp, monkeypatch):
    import os
    import stat

    _add(dbp, "hook", "generic", HOOK)
    path = _locked_cycle(dbp, monkeypatch)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not os.path.exists(path + ".tmp")


def test_a_crash_before_unlink_replays_nothing_twice(dbp, monkeypatch):
    import shutil

    tid = _add(dbp, "hook", "generic", HOOK)
    path = _locked_cycle(dbp, monkeypatch)
    keep = path + ".copy"
    shutil.copy(path, keep)
    rec = Recorder(status=503)
    transports.WebhookDispatcher(dbp, transport=rec.transport, now=T0 + 300).send([])
    shutil.move(keep, path)                  # as if the unlink never happened
    transports.WebhookDispatcher(dbp, transport=rec.transport, now=T0 + 600).send([])
    assert transports._pending_paths(dbp) == []
    # one failure per cycle that really ran: the held-over, then two retries
    assert _transport(dbp, tid)["failures"] == 3
    assert [e["event"] for e in _events(dbp)] == ["delivery_failed"] * 3


def test_a_file_folded_into_another_is_not_replayed(dbp, monkeypatch):
    import shutil

    tid = _add(dbp, "hook", "generic", HOOK)
    first = _locked_cycle(dbp, monkeypatch)
    keep = first + ".copy"
    shutil.copy(first, keep)
    # a second locked cycle folds the first into its own file...
    blocker = sqlite3.connect(dbp, check_same_thread=False)

    def locked(request):
        blocker.execute("BEGIN IMMEDIATE")
        return httpx.Response(503)

    transports.WebhookDispatcher(dbp, transport=httpx.MockTransport(locked),
                                 now=T0 + 300).send([])
    blocker.rollback(); blocker.close()
    shutil.move(keep, first)                 # ...and crashed before unlinking it
    assert len(transports._pending_paths(dbp)) == 2
    transports.WebhookDispatcher(dbp, transport=Recorder().transport, now=T0 + 600).send([])
    assert transports._pending_paths(dbp) == []
    assert [e["event"] for e in _events(dbp)] == ["delivery_failed"] * 2 + ["notified"]
    assert _transport(dbp, tid)["failures"] == 0


def test_corrupt_and_stale_side_files_are_removed(dbp):
    import os
    import time

    _add(dbp, "hook", "generic", HOOK)
    bad = f"{dbp}-dispatch-1-1-deadbeef.json"
    with open(bad, "w") as f:
        f.write("{not json")
    stale = f"{dbp}-dispatch-2-1-cafecafe.json.tmp"
    fresh = f"{dbp}-dispatch-3-1-feedfeed.json.tmp"
    for p in (stale, fresh):
        open(p, "w").close()
    old = time.time() - 2 * transports.TMP_STALE_S
    os.utime(stale, (old, old))
    d = transports.WebhookDispatcher(dbp, transport=Recorder().transport, now=T0)
    d.send([])
    assert not os.path.exists(bad) and not os.path.exists(stale)
    assert os.path.exists(fresh)            # may be a writer at work
    assert any("unreadable held-over outcome" in line for line in d.lines)


def test_a_disabled_default_transport_is_labeled_and_named_in_the_poll(dbp):
    """The first transport is the default route; when it is off, everything
    routed by default goes nowhere, and the page and the poll say why."""
    off = _add(dbp, "first", "generic", HOOK, enabled=0)
    on = _add(dbp, "second", "generic", HOOK)
    c = _c(dbp)
    assert transports.route_label(c, None) == "default (first (off), warn+)"
    assert transports.route_label(c, f"{on}:crit") == "second, crit+"
    assert transports.route_label(c, f"{off}:warn") == "first (off), warn+"
    c.close()

    rec = Recorder()
    d = _poll(dbp, [_item("a")], 1000.0, rec)
    assert rec.seen == []
    assert any("default route points at first, which is disabled" in line
               for line in d.lines)
    assert not any("hooks" in line for line in d.lines)     # no URL in the report
    # nothing routed by default this cycle: nothing to explain
    d = _poll(dbp, [_item("a")], 1100.0, rec)
    assert not any("default route" in line for line in d.lines)
    # a pinned default clears the warning
    c = _c(dbp)
    pdb.set_state(c, transports.DEFAULT_ROUTE_KEY, f"{on}:warn")
    c.commit()
    c.close()
    d = _poll(dbp, [_item("a"), _item("b")], 1200.0, rec)
    assert not any("default route" in line for line in d.lines)


def test_generic_payload_url_encodes_odd_device_names(clean_env):
    from patchbay import attention

    href = f"/device/{attention.quote('Sw #1 a?b&c', safe='')}"
    n = alerting.Notification(key="k", rule="device-down", kind="raise", severity="crit",
                              text="t", href=href, raised_at=0.0, route="all",
                              detail="")
    p = transports.generic_payload(n, transport="t", base="https://pb.example", now=1.0)
    assert p["url"] == "https://pb.example/device/Sw%20%231%20a%3Fb%26c"
