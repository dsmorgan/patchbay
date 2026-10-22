"""Auth: hashing, signed sessions, redirect safety, and the three modes
end-to-end through the middleware."""

from fastapi.testclient import TestClient

from patchbay import auth


def test_password_hash_roundtrip():
    h = auth.hash_password("s3cret")
    assert auth.verify_password("s3cret", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("s3cret", "garbage")
    assert not auth.verify_password("s3cret", "pbkdf2$notanint$zz$zz")


def test_token_roundtrip_and_tampering():
    secret = b"k" * 32
    tok = auth.make_token(secret, "user@example.com", hours=1)
    assert auth.read_token(secret, tok) == "user@example.com"
    assert auth.read_token(b"other-secret", tok) is None
    assert auth.read_token(secret, tok[:-2] + "xx") is None
    assert auth.read_token(secret, None) is None
    expired = auth.make_token(secret, "u", hours=-1)
    assert auth.read_token(secret, expired) is None


def test_safe_next_rejects_offsite():
    assert auth.safe_next("/topology?x=1") == "/topology?x=1"
    assert auth.safe_next("//evil.example/x") == "/"
    assert auth.safe_next("/\\evil.example") == "/"
    assert auth.safe_next("https://evil.example") == "/"
    assert auth.safe_next(None) == "/"


def test_claim_path():
    assert auth.claim_path({"a": {"b": "x"}}, "a.b") == "x"
    assert auth.claim_path({"a": 3}, "a.b") is None
    assert auth.claim_path({}, "email") is None


def _client(clean_env, **env):
    for k, v in env.items():
        clean_env.setenv(k, v)
    import patchbay.web as web
    return TestClient(web.app, follow_redirects=False)


def test_mode_none_is_open(clean_env):
    c = _client(clean_env)
    assert c.get("/").status_code == 200
    assert c.get("/login").status_code == 303  # nothing to log into


def test_password_mode_gates_everything(clean_env):
    c = _client(clean_env, PATCHBAY_AUTH="password",
                PATCHBAY_PASSWORD_HASH=auth.hash_password("pw"))
    r = c.get("/topology")
    assert r.status_code == 303 and "/login" in r.headers["location"]
    assert c.post("/ops/poll").status_code == 401
    r = c.post("/login", data={"password": "nope", "next": "/"})
    assert "error" in r.headers["location"]
    r = c.post("/login", data={"password": "pw", "next": "/vlans"})
    assert r.headers["location"] == "/vlans"
    assert c.get("/").status_code == 200
    c.get("/logout")
    assert c.get("/").status_code == 303


def test_oidc_login_redirects_with_state(clean_env):
    c = _client(clean_env, PATCHBAY_AUTH="oidc",
                PATCHBAY_OIDC_CLIENT_ID="cid", PATCHBAY_OIDC_CLIENT_SECRET="cs",
                PATCHBAY_OIDC_AUTH_URL="https://idp.example/authorize",
                PATCHBAY_OIDC_TOKEN_URL="https://idp.example/token")
    r = c.get("/auth/oidc/login")
    assert r.status_code == 303
    assert r.headers["location"].startswith("https://idp.example/authorize?")
    assert "state=" in r.headers["location"]
    assert c.cookies.get(auth.STATE_COOKIE)
    # forged state on the callback bounces to login with an error
    r = c.get("/auth/oidc/callback?code=x&state=forged")
    assert r.status_code == 303 and "error" in r.headers["location"]


def test_cross_origin_posts_rejected_even_in_mode_none(clean_env):
    c = _client(clean_env)
    r = c.post("/api/positions/reset",
               headers={"origin": "https://evil.example", "host": "testserver"})
    assert r.status_code == 403
    r = c.post("/api/positions/reset",
               headers={"origin": "http://testserver", "host": "testserver"})
    assert r.status_code == 200


def test_state_cookie_never_verifies_as_session():
    # the OIDC state cookie is handed to unauthenticated visitors and is
    # signed with the same secret — kind separation must reject the swap
    secret = b"k" * 32
    state_tok = auth.make_token(secret, '{"state": "x"}', hours=1,
                                kind="oidc-state")
    assert auth.read_token(secret, state_tok) is None            # as session
    assert auth.read_token(secret, state_tok, kind="oidc-state")  # as itself
    sess = auth.make_token(secret, "user", hours=1)
    assert auth.read_token(secret, sess, kind="oidc-state") is None


def test_oidc_state_cookie_cannot_authenticate(clean_env):
    c = _client(clean_env, PATCHBAY_AUTH="oidc",
                PATCHBAY_OIDC_CLIENT_ID="cid", PATCHBAY_OIDC_CLIENT_SECRET="cs",
                PATCHBAY_OIDC_AUTH_URL="https://idp.example/authorize",
                PATCHBAY_OIDC_TOKEN_URL="https://idp.example/token")
    c.get("/auth/oidc/login")                       # public: hands out state
    state_value = c.cookies.get(auth.STATE_COOKIE)
    assert state_value
    c.cookies.set(auth.SESSION_COOKIE, state_value)  # attacker replays it
    r = c.get("/topology", follow_redirects=False)
    assert r.status_code == 303                      # still unauthenticated


# --- #94: auth hardening -----------------------------------------------------

import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

OIDC_ENV = dict(PATCHBAY_AUTH="oidc",
                PATCHBAY_OIDC_CLIENT_ID="cid", PATCHBAY_OIDC_CLIENT_SECRET="cs",
                PATCHBAY_OIDC_AUTH_URL="https://idp.example/authorize",
                PATCHBAY_OIDC_TOKEN_URL="https://idp.example/token")


@pytest.fixture()
def fresh_login_queue(monkeypatch):
    """Login pacing is process state; start each test with an empty queue."""
    monkeypatch.setattr(auth, "_login_next_slot", 0.0)
    return monkeypatch


async def _concurrently(*coros):
    return await asyncio.gather(*coros)


def _async_client():
    import patchbay.web as web
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
                             base_url="http://testserver", follow_redirects=False)


def test_failed_logins_do_not_stall_other_requests(clean_env, fresh_login_queue):
    for k, v in {"PATCHBAY_AUTH": "password",
                 "PATCHBAY_PASSWORD_HASH": auth.hash_password("pw")}.items():
        clean_env.setenv(k, v)
    fresh_login_queue.setattr(auth, "LOGIN_DELAY", 0.4)

    async def scenario():
        async with _async_client() as c:
            t0 = time.monotonic()

            async def bad_login():
                return await c.post("/login", data={"password": "nope"})

            async def static_fetch():
                await asyncio.sleep(0.05)  # let the logins get going first
                r = await c.get("/static/d3.v7.min.js")
                # measured from the start: a blocked loop delays the wake-up
                # from the sleep above as much as the request itself
                return r, time.monotonic() - t0

            *logins, (static, took) = await _concurrently(
                *(bad_login() for _ in range(4)), static_fetch())
            return logins, static, took

    logins, static, took = asyncio.run(scenario())
    assert all("error=password" in r.headers["location"] for r in logins)
    assert static.status_code == 200
    # four failures hold ~0.4 s each plus a queue; the static file must not wait
    assert took < 0.35


def test_login_attempts_are_paced_across_clients(clean_env, fresh_login_queue):
    for k, v in {"PATCHBAY_AUTH": "password",
                 "PATCHBAY_PASSWORD_HASH": auth.hash_password("pw")}.items():
        clean_env.setenv(k, v)
    fresh_login_queue.setattr(auth, "LOGIN_DELAY", 0.1)

    async def scenario():
        async with _async_client() as c:
            t0 = time.monotonic()
            rs = await _concurrently(*(c.post("/login", data={"password": "x"})
                                       for _ in range(5)))
            return rs, time.monotonic() - t0

    rs, took = asyncio.run(scenario())
    assert all("error=password" in r.headers["location"] for r in rs)
    # five attempts start one per 0.1 s, and the last still waits its 0.1 s
    assert took >= 0.45


def test_login_queue_turns_away_when_full(clean_env, fresh_login_queue):
    for k, v in {"PATCHBAY_AUTH": "password",
                 "PATCHBAY_PASSWORD_HASH": auth.hash_password("pw")}.items():
        clean_env.setenv(k, v)
    fresh_login_queue.setattr(auth, "LOGIN_DELAY", 0.1)
    fresh_login_queue.setattr(auth, "LOGIN_QUEUE_MAX", 0.15)

    async def scenario():
        async with _async_client() as c:
            return await _concurrently(*(c.post("/login", data={"password": "x"})
                                         for _ in range(5)))

    locations = [r.headers["location"] for r in asyncio.run(scenario())]
    assert sum("error=password" in loc for loc in locations) == 2
    assert sum("error=busy" in loc for loc in locations) == 3
    time.sleep(0.25)  # the queue drains
    c = _client(clean_env)
    r = c.post("/login", data={"password": "pw", "next": "/vlans"})
    assert r.headers["location"] == "/vlans"


def test_password_rotation_ends_sessions(clean_env):
    c = _client(clean_env, PATCHBAY_AUTH="password",
                PATCHBAY_PASSWORD_HASH=auth.hash_password("old"))
    c.post("/login", data={"password": "old", "next": "/"})
    assert c.get("/").status_code == 200
    clean_env.setenv("PATCHBAY_PASSWORD_HASH", auth.hash_password("new"))
    assert c.get("/").status_code == 303


def test_plain_password_rotation_ends_sessions(clean_env):
    c = _client(clean_env, PATCHBAY_AUTH="password", PATCHBAY_PASSWORD="old")
    c.post("/login", data={"password": "old", "next": "/"})
    assert c.get("/").status_code == 200
    clean_env.setenv("PATCHBAY_PASSWORD", "new")
    assert c.get("/").status_code == 303


def test_fingerprint_does_not_reveal_a_plain_password(clean_env):
    from patchbay.config import load_settings
    clean_env.setenv("PATCHBAY_AUTH", "password")
    clean_env.setenv("PATCHBAY_PASSWORD", "hunter2")
    settings = load_settings()
    payload = auth.session_token(settings, "operator").rpartition(".")[0]
    fp = json.loads(auth._unb64(payload))["fp"]
    import hashlib
    for guess in ("hunter2", json.dumps(["password", "", "hunter2"])):
        assert hashlib.sha256(guess.encode()).hexdigest()[:32] != fp


def test_auth_mode_switch_ends_sessions(clean_env):
    from patchbay.config import load_settings
    c = _client(clean_env, **OIDC_ENV)
    c.cookies.set(auth.SESSION_COOKIE,
                  auth.session_token(load_settings(), "mallory@example.com"))
    assert c.get("/").status_code == 200
    clean_env.setenv("PATCHBAY_AUTH", "password")
    clean_env.setenv("PATCHBAY_PASSWORD_HASH", auth.hash_password("pw"))
    assert c.get("/").status_code == 303


def test_oidc_provider_change_ends_sessions(clean_env):
    from patchbay.config import load_settings
    c = _client(clean_env, **OIDC_ENV)
    c.cookies.set(auth.SESSION_COOKIE,
                  auth.session_token(load_settings(), "alice@example.com"))
    assert c.get("/").status_code == 200
    clean_env.setenv("PATCHBAY_OIDC_IDENTITY_PATH", "preferred_username")
    assert c.get("/").status_code == 303


def test_allowlist_removal_ends_that_session_only(clean_env):
    from patchbay.config import load_settings
    c = _client(clean_env, PATCHBAY_OIDC_ALLOWED="alice@example.com,mallory@example.com",
                **OIDC_ENV)
    settings = load_settings()
    alice = auth.session_token(settings, "alice@example.com")
    mallory = auth.session_token(settings, "mallory@example.com")
    c.cookies.set(auth.SESSION_COOKIE, mallory)
    assert c.get("/").status_code == 200
    clean_env.setenv("PATCHBAY_OIDC_ALLOWED", "Alice@example.com,bob@example.com")
    assert c.get("/").status_code == 303
    c.cookies.set(auth.SESSION_COOKIE, alice)
    assert c.get("/").status_code == 200


def test_session_without_fingerprint_is_rejected(clean_env):
    # cookies minted before #94 carry no fingerprint: one sign-in on upgrade
    from patchbay.config import load_settings
    c = _client(clean_env, **OIDC_ENV)
    legacy = auth.make_token(auth.session_secret(load_settings()),
                             "alice@example.com", hours=1)
    c.cookies.set(auth.SESSION_COOKIE, legacy)
    assert c.get("/").status_code == 303


def _jwt(claims: dict) -> str:
    return ".".join([auth._b64(b'{"alg":"none"}'),
                     auth._b64(json.dumps(claims).encode()), "sig"])


def _oidc_callback(clean_env, monkeypatch, claims, **env):
    """Run the callback against a fake token endpoint answering claims in
    an id_token; return the callback response."""
    def handler(request):
        return httpx.Response(200, json={"id_token": _jwt(claims)})

    real_client = httpx.Client
    monkeypatch.setattr(auth.httpx, "Client", lambda **kw: real_client(
        transport=httpx.MockTransport(handler)))
    c = _client(clean_env, **{**OIDC_ENV, **env})
    r = c.get("/auth/oidc/login")
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    return c, c.get(f"/auth/oidc/callback?code=abc&state={state}")


@pytest.mark.parametrize("verified", [True, "true", "TRUE"])
def test_oidc_accepts_a_verified_email(clean_env, monkeypatch, verified):
    c, r = _oidc_callback(clean_env, monkeypatch,
                          {"email": "alice@example.com", "email_verified": verified})
    assert r.headers["location"] == "/"
    assert c.get("/").status_code == 200


@pytest.mark.parametrize("claims", [
    {"email": "alice@example.com", "email_verified": False},
    {"email": "alice@example.com", "email_verified": "false"},
    {"email": "alice@example.com"},                  # provider omits the claim
])
def test_oidc_rejects_an_unverified_email(clean_env, monkeypatch, claims):
    c, r = _oidc_callback(clean_env, monkeypatch, claims)
    assert r.headers["location"] == "/login?error=unverified"
    assert not c.cookies.get(auth.SESSION_COOKIE)
    assert c.get("/").status_code == 303


def test_oidc_other_identity_claims_need_no_email_verified(clean_env, monkeypatch):
    c, r = _oidc_callback(clean_env, monkeypatch, {"preferred_username": "alice"},
                          PATCHBAY_OIDC_IDENTITY_PATH="preferred_username")
    assert r.headers["location"] == "/"


def test_oidc_denied_identity_stays_off_the_url(clean_env, monkeypatch):
    _, r = _oidc_callback(clean_env, monkeypatch,
                          {"email": "mallory@example.com", "email_verified": True},
                          PATCHBAY_OIDC_ALLOWED="alice@example.com")
    assert r.headers["location"] == "/login?error=denied"


def test_oidc_missing_claim(clean_env, monkeypatch):
    _, r = _oidc_callback(clean_env, monkeypatch, {"sub": "123"})
    assert r.headers["location"] == "/login?error=noclaim"


def test_login_page_shows_only_known_messages(clean_env):
    c = _client(clean_env, PATCHBAY_AUTH="password", PATCHBAY_PASSWORD="pw")
    spoof = "Session expired, re-enter your password at https://evil.example"
    page = c.get("/login", params={"error": spoof}).text
    assert "evil.example" not in page and "Session expired" not in page
    assert "wrong password" in c.get("/login?error=password").text
    r = c.post("/login", data={"password": "nope", "next": "/"})
    assert "error=password" in r.headers["location"]


def test_oidc_provider_error_text_is_not_reflected(clean_env):
    c = _client(clean_env, **OIDC_ENV)
    r = c.get("/auth/oidc/callback", params={"error": "visit https://evil.example"})
    assert r.headers["location"] == "/login?error=incomplete"


def test_split_host():
    assert auth._split_host("Patchbay.Example.NET.:8080") == ("patchbay.example.net", "8080")
    assert auth._split_host("patchbay.example.net") == ("patchbay.example.net", None)
    assert auth._split_host("[2001:db8::1]:8443") == ("2001:db8::1", "8443")
    assert auth._split_host("[2001:db8::1]") == ("2001:db8::1", None)
    assert auth._split_host("2001:db8::1") == ("2001:db8::1", None)


def test_host_allowed_matching(clean_env):
    from patchbay.config import load_settings
    clean_env.setenv("PATCHBAY_ALLOWED_HOSTS",
                     "patchbay.example.net, 192.0.2.10:8080,[2001:db8::10]")
    s = load_settings()
    assert auth.host_allowed(s, "patchbay.example.net:8443")   # any port
    assert auth.host_allowed(s, "PATCHBAY.example.net")
    assert auth.host_allowed(s, "192.0.2.10:8080")
    assert not auth.host_allowed(s, "192.0.2.10:9000")          # port pinned
    assert not auth.host_allowed(s, "192.0.2.10")
    assert auth.host_allowed(s, "[2001:db8::10]:8080")
    for loop in ("localhost:8080", "127.0.0.1", "[::1]:8080"):
        assert auth.host_allowed(s, loop)
    assert not auth.host_allowed(s, "attacker.example")
    assert not auth.host_allowed(s, "patchbay.example.net.attacker.example")
    assert not auth.host_allowed(s, "")


def test_any_host_accepted_without_allowlist(clean_env):
    import patchbay.web as web
    c = TestClient(web.app, base_url="http://attacker.example:8080")
    assert c.get("/").status_code == 200


def test_allowlist_refuses_other_hosts(clean_env):
    import patchbay.web as web
    clean_env.setenv("PATCHBAY_ALLOWED_HOSTS", "patchbay.example.net")
    ok = TestClient(web.app, base_url="http://patchbay.example.net:8080")
    assert ok.get("/").status_code == 200
    assert TestClient(web.app, base_url="http://localhost:8080").get("/").status_code == 200
    assert TestClient(web.app, base_url="http://127.0.0.1").get("/").status_code == 200
    rebound = TestClient(web.app, base_url="http://attacker.example:8080")
    assert rebound.get("/").status_code == 400
    assert rebound.get("/static/d3.v7.min.js").status_code == 400
    assert rebound.post("/api/positions/reset",
                        headers={"origin": "http://attacker.example:8080"}
                        ).status_code == 400
    # every host the request names must pass, forwarded ones included
    assert ok.get("/", headers={"x-forwarded-host": "attacker.example"}).status_code == 400
    assert rebound.get("/", headers={"x-forwarded-host": "patchbay.example.net"}
                       ).status_code == 400
    assert ok.get("/", headers={"x-forwarded-host": "patchbay.example.net, localhost"}
                  ).status_code == 200


def test_ops_warns_when_open_to_any_host(clean_env):
    c = _client(clean_env)
    assert 'id="hostwarn"' in c.get("/ops").text
    assert "any host" in c.get("/ops").text   # the serving row
    clean_env.setenv("PATCHBAY_ALLOWED_HOSTS", "testserver")
    page = c.get("/ops").text
    assert 'id="hostwarn"' not in page and "PATCHBAY_ALLOWED_HOSTS" in page
    clean_env.delenv("PATCHBAY_ALLOWED_HOSTS")
    clean_env.setenv("PATCHBAY_AUTH", "password")
    clean_env.setenv("PATCHBAY_PASSWORD", "pw")
    c.post("/login", data={"password": "pw", "next": "/"})
    assert 'id="hostwarn"' not in c.get("/ops").text
