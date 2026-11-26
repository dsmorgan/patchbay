"""The read-only MCP server (ADR-0004): the /mcp gate, the tool list, each
tool's answer on the demo network, and what never leaves the host."""

import asyncio
import hashlib
import html
import json
import logging
import os
import sqlite3
import stat
import sys
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from patchbay import auth, db, demo, mcp_server
from patchbay.config import load_settings

TOKEN = "mcp-test-token-4f9c2a"
PROTO = "2025-11-25"
HEADERS = {"Authorization": f"Bearer {TOKEN}",
           "Accept": "application/json, text/event-stream",
           "MCP-Protocol-Version": PROTO}


def _rpc(method, params=None, id_=1):
    msg = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


INIT = _rpc("initialize", {"protocolVersion": PROTO, "capabilities": {},
                           "clientInfo": {"name": "pytest", "version": "0"}})


@pytest.fixture()
def demo_db(clean_env, tmp_path):
    """The demo network at PATCHBAY_DB."""
    path = tmp_path / "test.db"
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    demo.seed(c)
    c.commit()
    c.close()
    return path


@pytest.fixture()
def mcp_client(clean_env, demo_db):
    """A web client with the app's lifespan running and the token set."""
    clean_env.setenv("PATCHBAY_MCP_TOKEN", TOKEN)
    from patchbay import web
    with TestClient(web.app, follow_redirects=False) as c:
        yield c


def _call(c, name, arguments=None):
    r = c.post("/mcp", headers=HEADERS, json=_rpc(
        "tools/call", {"name": name, "arguments": arguments or {}}))
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


# --- the gate -----------------------------------------------------------------

def test_mcp_is_404_until_a_token_is_set(clean_env, demo_db):
    from patchbay import web
    with TestClient(web.app) as c:
        assert c.post("/mcp", headers=HEADERS, json=INIT).status_code == 404
        assert c.post("/mcp/x", headers=HEADERS, json=INIT).status_code == 404
        assert c.get("/mcp", headers=HEADERS).status_code == 404


def test_mcp_needs_the_right_bearer_token(mcp_client):
    for headers in ({}, {"Authorization": "Bearer wrong"},
                    {"Authorization": f"Basic {TOKEN}"},
                    {"Authorization": TOKEN},
                    {"Authorization": f"Bearer {TOKEN}x"}):
        r = mcp_client.post("/mcp", headers={**HEADERS, **headers}
                            if headers else {"Accept": HEADERS["Accept"]}, json=INIT)
        assert r.status_code == 401, headers
        assert r.headers["www-authenticate"].startswith("Bearer")
    r = mcp_client.post("/mcp", headers={**HEADERS, "Authorization": f"bearer {TOKEN}"},
                        json=INIT)
    assert r.status_code == 200   # the scheme is case-insensitive


def test_bearer_check_is_constant_time_and_off_without_a_token(clean_env):
    clean_env.setenv("PATCHBAY_MCP_TOKEN", TOKEN)
    s = load_settings(read_db=False)
    assert auth.bearer_ok(s, f"Bearer {TOKEN}")
    assert auth.bearer_ok(s, f"  Bearer   {TOKEN} ")
    assert not auth.bearer_ok(s, "Bearer ")
    assert not auth.bearer_ok(s, None)
    clean_env.delenv("PATCHBAY_MCP_TOKEN")
    assert not auth.bearer_ok(load_settings(read_db=False), "Bearer ")


def test_session_cookie_never_opens_mcp_and_token_opens_nothing_else(clean_env, demo_db):
    clean_env.setenv("PATCHBAY_MCP_TOKEN", TOKEN)
    clean_env.setenv("PATCHBAY_AUTH", "password")
    clean_env.setenv("PATCHBAY_PASSWORD_HASH", auth.hash_password("pw"))
    from patchbay import web
    with TestClient(web.app, follow_redirects=False) as c:
        c.cookies.set(auth.SESSION_COOKIE, auth.session_token(load_settings(), "operator"))
        assert c.get("/topology").status_code == 200        # the cookie works
        r = c.post("/mcp", headers={"Accept": HEADERS["Accept"]}, json=INIT)
        assert r.status_code == 401                          # but not on /mcp
        c.cookies.clear()
        r = c.get("/topology", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 303 and "/login" in r.headers["location"]
        assert c.post("/ops/poll", headers={"Authorization": f"Bearer {TOKEN}"}
                      ).status_code == 401
        assert c.post("/mcp", headers=HEADERS, json=INIT).status_code == 200


def test_mcp_keeps_the_host_allowlist_and_origin_check(clean_env, demo_db):
    clean_env.setenv("PATCHBAY_MCP_TOKEN", TOKEN)
    clean_env.setenv("PATCHBAY_ALLOWED_HOSTS", "patchbay.example.net:8080")
    from patchbay import web
    with TestClient(web.app, base_url="http://patchbay.example.net:8080") as c:
        assert c.post("/mcp", headers=HEADERS, json=INIT).status_code == 200
        r = c.post("/mcp", headers={**HEADERS, "Host": "attacker.example"}, json=INIT)
        assert r.status_code == 400
        r = c.post("/mcp", headers={**HEADERS, "Origin": "http://attacker.example"},
                   json=INIT)
        assert r.status_code == 403
        # the SDK's own DNS-rebinding check got the same list
        sdk = mcp_server.http_endpoint._manager.security_settings
        assert sdk.enable_dns_rebinding_protection
        assert "patchbay.example.net:8080" in sdk.allowed_hosts
        assert "attacker.example" not in sdk.allowed_hosts


def test_transport_security_mirrors_the_allowlist():
    off = mcp_server.transport_security(None)
    assert not off.enable_dns_rebinding_protection
    s = mcp_server.transport_security(("patchbay.example.net", "192.0.2.10:8080",
                                       "[2001:db8::10]", "web.example.net:443"))
    assert s.enable_dns_rebinding_protection
    for h in ("patchbay.example.net", "patchbay.example.net:*", "192.0.2.10:8080",
              "[2001:db8::10]:*", "web.example.net:443", "web.example.net",
              "localhost:*", "127.0.0.1:*", "[::1]:*"):
        assert h in s.allowed_hosts, h
    assert "192.0.2.10" not in s.allowed_hosts          # the port is pinned
    assert "http://patchbay.example.net:*" in s.allowed_origins


def test_ops_says_whether_mcp_is_on_never_the_token(mcp_client):
    body = mcp_client.get("/ops").text
    assert "PATCHBAY_MCP_TOKEN" in body and "/mcp answers" in body
    assert TOKEN not in body


def test_the_token_is_never_logged(mcp_client, caplog):
    caplog.set_level(logging.DEBUG)
    mcp_client.post("/mcp", headers={**HEADERS, "Authorization": "Bearer wrong"}, json=INIT)
    mcp_client.post("/mcp", headers=HEADERS, json=INIT)
    _call(mcp_client, "freshness")
    assert TOKEN not in caplog.text


# --- the tools ----------------------------------------------------------------

def test_tools_list_is_exactly_three_read_only_tools(mcp_client):
    r = mcp_client.post("/mcp", headers=HEADERS, json=INIT)
    assert r.status_code == 200
    assert r.json()["result"]["serverInfo"]["name"] == "patchbay"
    r = mcp_client.post("/mcp", headers=HEADERS, json=_rpc("tools/list"))
    tools = r.json()["result"]["tools"]
    assert sorted(t["name"] for t in tools) == ["alerts", "find", "freshness"]
    for t in tools:
        assert t["annotations"]["readOnlyHint"] is True, t["name"]
        assert t["annotations"]["destructiveHint"] is False, t["name"]
        assert t["outputSchema"]["type"] == "object", t["name"]


def test_freshness(mcp_client):
    out = _call(mcp_client, "freshness")
    conn = sqlite3.connect(os.environ["PATCHBAY_DB"])
    conn.row_factory = sqlite3.Row
    ts = db.last_poll_ts(conn)
    conn.close()
    assert out["last_poll"] == datetime.fromtimestamp(ts, timezone.utc).isoformat(
        timespec="seconds")
    assert out["last_poll_age_minutes"] < 5
    assert out["stale_after_minutes"] == 15
    names = {s["source"] for s in out["sources"]}
    assert {"librenms", "opnsense", "phpipam"} <= names
    assert not any(s["stale"] for s in out["sources"])
    # the top bar's endpoint and the tool read the same fact
    assert mcp_client.get("/api/freshness").json()["last_poll"] == ts


def test_find_over_http(mcp_client):
    out = _call(mcp_client, "find", {"query": "02-D3-00-00-02-C7"})
    assert out["kind"] == "mac" and out["found"]
    assert out["mac_table"][0]["device"] == "edge1"
    assert out["maps"]["topology"] == ["nas1"]
    out = _call(mcp_client, "find", {"query": "no-such-host"})
    assert not out["found"]


def test_alerts_match_the_alerts_page(mcp_client):
    out = _call(mcp_client, "alerts")
    assert out["history"] is None
    assert out["checked"]
    assert out["active"], "the demo seeds open alerts"
    page = mcp_client.get("/alerts").text
    for it in out["active"]:
        assert it["severity"] in ("crit", "warn", "info")
        assert html.escape(it["text"], quote=False) in page, it["text"]
    canary = next(it for it in out["active"] if it["rule"] == "port-canary")
    assert canary["first_seen"] and canary["state"] == "active"
    assert canary["firing_for"] != "new"
    assert out["silenced"] and out["silenced"][0]["silence"].startswith("alert ")
    silenced = mcp_client.get("/alerts?tab=silenced").text
    for it in out["silenced"]:
        assert html.escape(it["text"], quote=False) in silenced
    hist = _call(mcp_client, "alerts", {"history": True})["history"]
    assert hist and {"raised", "cleared"} <= {e["event"] for e in hist}
    assert hist == sorted(hist, key=lambda e: e["at"], reverse=True)


def test_tools_fail_cleanly_without_a_database(clean_env):
    clean_env.setenv("PATCHBAY_MCP_TOKEN", TOKEN)
    from patchbay import web
    with TestClient(web.app) as c:
        r = c.post("/mcp", headers=HEADERS, json=_rpc(
            "tools/call", {"name": "freshness", "arguments": {}}))
        result = r.json()["result"]
        assert result["isError"] and "no database yet" in result["content"][0]["text"]
    assert not os.path.exists(os.environ["PATCHBAY_DB"])   # nothing created it


def test_in_process_client_gets_structured_results(clean_env, demo_db):
    from mcp import Client

    async def run():
        async with Client(mcp_server.build_server()) as client:
            tools = await client.list_tools()
            assert [t.name for t in tools.tools] == ["freshness", "find", "alerts"]
            r = await client.call_tool("find", {"query": "ap-den"})
            assert not r.is_error
            return r.structured_content

    out = asyncio.run(run())
    assert out["devices"][0]["role"] == "ap"


# --- what never leaves the host -------------------------------------------------

SECRETS = {
    "PATCHBAY_MCP_TOKEN": TOKEN,
    "LIBRENMS_TOKEN": "librenms-secret-7d1e",
    "IPAM_TOKEN": "ipam-secret-0b3a",
    "UNIFI_PASS": "unifi-secret-55c2",
    "PATCHBAY_PASSWORD": "ui-password-91aa",
    "PATCHBAY_OIDC_CLIENT_SECRET": "oidc-secret-e4f0",
}


def test_no_tool_returns_a_secret(clean_env, demo_db):
    for k, v in SECRETS.items():
        clean_env.setenv(k, v)
    clean_env.setenv("LIBRENMS_URL", "https://nms-user:nms-pass-3c9d@librenms.example.net")
    conn = sqlite3.connect(demo_db)
    conn.row_factory = sqlite3.Row
    session = auth.session_secret(load_settings())       # generated and stored
    redaction = db.redaction_key(conn).decode()
    conn.execute("INSERT INTO app_state (key, value) VALUES "
                 "('cfg:PATCHBAY_SNAPSHOT_KEEP', 'stored-setting-6a2f')")
    conn.execute("INSERT INTO raw_payloads (source, endpoint, fetched_at, payload) "
                 "VALUES ('librenms', 'devices', ?, ?)",
                 (db.now(), json.dumps({"secret": "raw-payload-8e71"})))
    transport_url = conn.execute("SELECT url FROM alert_transports").fetchone()[0]
    conn.commit()
    conn.close()
    hidden = [*SECRETS.values(), "nms-pass-3c9d", session.decode(), transport_url,
              "stored-setting-6a2f", "raw-payload-8e71", "pbkdf2$", redaction]

    from patchbay import web
    outputs = []
    with TestClient(web.app) as c:
        outputs.append(c.post("/mcp", headers=HEADERS, json=INIT).text)
        outputs.append(c.post("/mcp", headers=HEADERS, json=_rpc("tools/list")).text)
        calls = [("freshness", {}), ("alerts", {"history": True}),
                 *[("find", {"query": q}) for q in (
                     "fw1", "core1", "hyp1", "nas1", "192.0.2.1", "ap-den",
                     "02:d3:00:00:02:c7", "demo-webhook", "librenms")]]
        for name, args in calls:
            r = c.post("/mcp", headers=HEADERS, json=_rpc(
                "tools/call", {"name": name, "arguments": args}))
            assert r.status_code == 200
            outputs.append(r.text)
    blob = "\n".join(outputs)
    for s in hidden:
        assert s not in blob, s


def test_tools_never_write_the_database(clean_env, demo_db):
    clean_env.setenv("PATCHBAY_MCP_TOKEN", TOKEN)
    before = hashlib.sha256(demo_db.read_bytes()).hexdigest()
    mtime = demo_db.stat().st_mtime_ns
    # a read-only file: any write attempt would fail the call, not pass quietly
    demo_db.chmod(stat.S_IRUSR | stat.S_IRGRP)
    try:
        from patchbay import web
        with TestClient(web.app) as c:
            for name, args in (("freshness", {}), ("alerts", {"history": True}),
                               ("find", {"query": "nas1"}),
                               ("find", {"query": "02d3.0000.02c7"}),
                               ("find", {"query": "198.51.100.11"})):
                _call(c, name, args)
    finally:
        demo_db.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert hashlib.sha256(demo_db.read_bytes()).hexdigest() == before
    assert demo_db.stat().st_mtime_ns == mtime
    assert not list(demo_db.parent.glob("test.db-*"))     # no journal, no WAL
