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
