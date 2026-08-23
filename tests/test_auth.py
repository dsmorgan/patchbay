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
