"""Authentication for the web UI.

Three modes (PATCHBAY_AUTH): none (default), password (one shared secret),
oidc (authorization-code flow against a generic OAuth2/OIDC provider).
patchbay is read-only, so this is a gate, not an identity system: no user
table, no roles. Sessions are HMAC-signed cookies; the signing secret is
auto-generated and persisted in the DB unless PATCHBAY_SESSION_SECRET is set.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import db
from .config import Settings

PBKDF2_ITERATIONS = 260_000
SESSION_COOKIE = "patchbay_session"
STATE_COOKIE = "patchbay_oidc_state"

# paths reachable without a session (login flow itself + static assets)
PUBLIC_PATHS = {"/login", "/logout", "/auth/oidc/login", "/auth/oidc/callback"}
PUBLIC_PREFIXES = ("/static/",)


# --- password hashing --------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt_hex, digest_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


def check_password(settings: Settings, candidate: str) -> bool:
    if settings.password_hash:
        return verify_password(candidate, settings.password_hash)
    if settings.password_plain:
        return hmac.compare_digest(candidate, settings.password_plain)
    return False


# --- signed session tokens ---------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


_secret_cache: dict[str, bytes] = {}


def session_secret(settings: Settings) -> bytes:
    """The signing secret: explicit env value, else one generated once and
    persisted in the DB so sessions survive restarts with zero config."""
    if settings.session_secret:
        return settings.session_secret.encode()
    cached = _secret_cache.get(settings.db_path)
    if cached:
        return cached
    with db.connect(settings.db_path) as conn:
        db.init(conn)
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = 'session_secret'").fetchone()
        if row:
            secret = row["value"].encode()
        else:
            generated = secrets.token_hex(32)
            # a concurrent first request may have raced us there — theirs wins
            conn.execute(
                "INSERT INTO app_state (key, value) VALUES ('session_secret', ?) "
                "ON CONFLICT(key) DO NOTHING", (generated,))
            secret = conn.execute(
                "SELECT value FROM app_state WHERE key = 'session_secret'"
            ).fetchone()["value"].encode()
    _secret_cache[settings.db_path] = secret
    return secret


def make_token(secret: bytes, identity: str, hours: float,
               kind: str = "session") -> str:
    """kind domain-separates token types: the OIDC state cookie is handed to
    UNAUTHENTICATED visitors, so a state token must never verify as a session
    token (same secret, so the type has to live in the signed claims)."""
    payload = _b64(json.dumps(
        {"sub": identity, "use": kind,
         "exp": int(time.time() + hours * 3600)}).encode())
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def read_token(secret: bytes, token: str | None,
               kind: str = "session") -> str | None:
    """Return the identity if the token is genuine, unexpired, and of the
    expected kind, else None."""
    if not token or "." not in token:
        return None
    payload, _, sig = token.rpartition(".")
    expect = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        claims = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if claims.get("exp", 0) < time.time() or claims.get("use") != kind:
        return None
    sub = claims.get("sub")
    return sub if isinstance(sub, str) else None


def _cookie_secure(request: Request) -> bool:
    return (request.headers.get("x-forwarded-proto", request.url.scheme)) == "https"


def set_session(response: Response, request: Request, settings: Settings,
                identity: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        make_token(session_secret(settings), identity, settings.session_hours),
        max_age=int(settings.session_hours * 3600),
        httponly=True, samesite="lax", secure=_cookie_secure(request))


# --- request gate ------------------------------------------------------------

def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def authenticated_identity(request: Request, settings: Settings) -> str | None:
    if settings.auth_mode == "none":
        return "-"
    return read_token(session_secret(settings),
                      request.cookies.get(SESSION_COOKIE))


def gate(request: Request, settings: Settings) -> Response | None:
    """Middleware body: None = let the request through (request.state.auth is
    set for templates), a Response = intercept with it."""
    # cross-origin writes are rejected in every auth mode: patchbay has no
    # cross-site API consumers, and this closes CSRF (layout reset, poll
    # triggers) even when the UI runs open on a trusted LAN
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        # behind a proxy the browser's Origin carries the PUBLIC host, which
        # arrives in X-Forwarded-Host, not Host — compare against whichever
        # the deployment presents
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        if origin and host and urlsplit(origin).netloc != host:
            return Response("cross-origin request rejected", status_code=403)
    if settings.auth_mode == "none" or is_public(request.url.path):
        request.state.auth = None
        return None
    identity = authenticated_identity(request, settings)
    if identity:
        request.state.auth = identity
        return None
    if request.method == "GET":
        nxt = quote(str(request.url.path) +
                    (f"?{request.url.query}" if request.url.query else ""))
        return RedirectResponse(f"/login?next={nxt}", status_code=303)
    return Response("authentication required", status_code=401)


def safe_next(raw: str | None) -> str:
    r"""Only same-site relative paths — anything else is an open redirect.
    Rejects // and /\ (browsers normalize backslash to slash)."""
    if raw and raw.startswith("/") and not (len(raw) > 1 and raw[1] in "/\\"):
        return raw
    return "/"


# --- OIDC helpers ------------------------------------------------------------

def redirect_url(request: Request, settings: Settings) -> str:
    if settings.oidc_redirect_url:
        return settings.oidc_redirect_url
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    return f"{scheme}://{host}/auth/oidc/callback"


def claim_path(data: dict, path: str):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _decode_jwt_claims(token: str) -> dict:
    """Claims of a JWT received directly from the token endpoint over TLS —
    the direct-channel response needs no signature check to be trusted."""
    try:
        return json.loads(_unb64(token.split(".")[1]))
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}


def oidc_exchange(request: Request, settings: Settings, code: str) -> str | None:
    """Exchange the auth code; return the identity claim or None."""
    verify = settings.tls_verify
    with httpx.Client(timeout=15, verify=verify) as client:
        token_resp = client.post(settings.oidc_token_url, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_url(request, settings),
            "client_id": settings.oidc_client_id,
            "client_secret": settings.oidc_client_secret,
        }, headers={"Accept": "application/json"})
        token_resp.raise_for_status()
        tokens = token_resp.json()
        claims: dict = {}
        if settings.oidc_userinfo_url and tokens.get("access_token"):
            ui = client.get(settings.oidc_userinfo_url, headers={
                "Authorization": f"Bearer {tokens['access_token']}",
                "Accept": "application/json"})
            ui.raise_for_status()
            claims = ui.json()
        elif tokens.get("id_token"):
            claims = _decode_jwt_claims(tokens["id_token"])
    identity = claim_path(claims, settings.oidc_identity_path)
    return identity if isinstance(identity, str) and identity else None


def oidc_allowed(settings: Settings, identity: str) -> bool:
    return not settings.oidc_allowed or identity.lower() in settings.oidc_allowed


# --- routes ------------------------------------------------------------------

def build_router(templates, get_settings) -> APIRouter:
    """Login/logout/OIDC routes. get_settings is a callable so the web app
    controls settings lifetime (fresh per request keeps tests simple)."""
    router = APIRouter()

    @router.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str | None = None, error: str | None = None):
        settings = get_settings()
        if settings.auth_mode == "none":
            return RedirectResponse("/", status_code=303)
        if authenticated_identity(request, settings):
            return RedirectResponse(safe_next(next), status_code=303)
        return templates.TemplateResponse(request, "login.html", {
            "mode": settings.auth_mode, "next": safe_next(next),
            "error": error})

    @router.post("/login")
    async def login_submit(request: Request):
        settings = get_settings()
        if settings.auth_mode != "password":
            return Response("password login not enabled", status_code=403)
        # one tiny urlencoded form — parse it directly rather than pulling in
        # python-multipart for starlette's form() machinery
        body = parse_qs((await request.body()).decode())
        form = {k: v[0] for k, v in body.items()}
        if not check_password(settings, form.get("password", "")):
            time.sleep(0.6)  # blunt the brute force
            nxt = quote(safe_next(form.get("next") or "/"))
            return RedirectResponse(
                f"/login?error=wrong+password&next={nxt}", status_code=303)
        resp = RedirectResponse(safe_next(form.get("next") or "/"),
                                status_code=303)
        set_session(resp, request, settings, "operator")
        return resp

    @router.get("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    @router.get("/auth/oidc/login")
    def oidc_login(request: Request, next: str | None = None):
        settings = get_settings()
        if settings.auth_mode != "oidc":
            return Response("oidc login not enabled", status_code=403)
        state = secrets.token_urlsafe(24)
        params = urlencode({
            "response_type": "code",
            "client_id": settings.oidc_client_id,
            "redirect_uri": redirect_url(request, settings),
            "scope": settings.oidc_scopes,
            "state": state,
        })
        resp = RedirectResponse(f"{settings.oidc_auth_url}?{params}",
                                status_code=303)
        # state + post-login destination ride one short-lived signed cookie
        resp.set_cookie(
            STATE_COOKIE,
            make_token(session_secret(settings),
                       json.dumps({"state": state, "next": safe_next(next)}),
                       hours=0.17, kind="oidc-state"),
            max_age=600, httponly=True, samesite="lax",
            secure=_cookie_secure(request))
        return resp

    @router.get("/auth/oidc/callback")
    def oidc_callback(request: Request, code: str | None = None,
                      state: str | None = None, error: str | None = None):
        settings = get_settings()
        if settings.auth_mode != "oidc":
            return Response("oidc login not enabled", status_code=403)
        stored = read_token(session_secret(settings),
                            request.cookies.get(STATE_COOKIE), kind="oidc-state")
        try:
            expected = json.loads(stored) if stored else None
        except (ValueError, TypeError):
            expected = None
        if error or not code or not expected or expected.get("state") != state:
            return RedirectResponse(
                f"/login?error={quote(error or 'sign-in was not completed')}",
                status_code=303)
        try:
            identity = oidc_exchange(request, settings, code)
        except httpx.HTTPError:
            return RedirectResponse(
                "/login?error=identity+provider+unreachable", status_code=303)
        if not identity:
            return RedirectResponse(
                "/login?error=provider+response+had+no+identity+claim",
                status_code=303)
        if not oidc_allowed(settings, identity):
            return RedirectResponse(
                f"/login?error={quote(identity + ' is not on the allowed list')}",
                status_code=303)
        resp = RedirectResponse(expected.get("next") or "/", status_code=303)
        resp.delete_cookie(STATE_COOKIE)
        set_session(resp, request, settings, identity)
        return resp

    return router
