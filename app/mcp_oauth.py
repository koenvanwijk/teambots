"""OAuth 2.0 (authorization code + PKCE) for MCP servers, per user.

The admin configures a server's OAuth client once (authorize/token URLs, client id,
optional secret, scopes). Each member authorizes and receives their own access and
refresh tokens, stored encrypted in McpCredential. Works for Athom/Homey, Google and
any standard OAuth2 server. Authorization-server metadata (.well-known) is fetched
when available, so we can honour form_post and client_secret_basic. Dynamic client
registration is not used; the endpoints are configured explicitly.
"""
from __future__ import annotations

import base64
import hashlib
import secrets as pysecrets
import time
from datetime import timedelta
from urllib.parse import urlencode, urlparse

import httpx

from . import config
from .db import McpOAuthState, McpServer, now, session

STATE_TTL_S = 600
_META_TTL_S = 3600
_META_FAIL_TTL_S = 60
_metadata_cache: dict[str, tuple[float, dict]] = {}
_discovery_enabled = True


def normalize_scopes(value) -> str:
    """Accept a list or a space/comma-separated string; store as a single space-separated string."""
    if value is None or value == "":
        return ""
    if isinstance(value, (list, tuple)):
        parts = [str(x).strip() for x in value if str(x).strip()]
        return " ".join(parts)
    text = str(value).replace(",", " ")
    return " ".join(text.split())


_REFRESH_MARGIN_S = 60


class OAuthError(Exception):
    pass


def redirect_uri() -> str:
    base = (config.PUBLIC_URL or "").rstrip("/")
    return f"{base}/api/mcp/oauth/callback"


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _origins(server: McpServer) -> list[str]:
    origins: list[str] = []
    for raw in (getattr(server, "oauth_authorize_url", ""), getattr(server, "oauth_token_url", ""),
                getattr(server, "url", "")):
        if not raw:
            continue
        p = urlparse(raw)
        if p.scheme and p.netloc:
            origin = f"{p.scheme}://{p.netloc}"
            if origin not in origins:
                origins.append(origin)
    return origins


def clear_metadata_cache() -> None:
    _metadata_cache.clear()


def prime_metadata(origin: str, data: dict, ttl: float = _META_TTL_S) -> None:
    """Test helper: pre-load authorization-server metadata for an origin."""
    _metadata_cache[origin.rstrip("/")] = (time.time() + ttl, dict(data or {}))


def authorization_server_metadata(server: McpServer) -> dict:
    """RFC 8414 metadata for this server's issuer, cached. Empty dict if discovery fails."""
    now_ts = time.time()
    skip = set()
    for origin in _origins(server):
        hit = _metadata_cache.get(origin)
        if hit and hit[0] > now_ts:
            if hit[1]:
                return hit[1]
            skip.add(origin)
    for origin in _origins(server):
        if origin in skip or not _discovery_enabled:
            continue
        url = origin + "/.well-known/oauth-authorization-server"
        try:
            with httpx.Client(timeout=5.0) as client:
                r = client.get(url, headers={"Accept": "application/json"})
            if r.status_code >= 400:
                _metadata_cache[origin] = (now_ts + _META_FAIL_TTL_S, {})
                continue
            data = r.json()
            if not isinstance(data, dict):
                data = {}
            _metadata_cache[origin] = (now_ts + _META_TTL_S, data)
            if data:
                return data
        except (httpx.HTTPError, ValueError):
            _metadata_cache[origin] = (now_ts + _META_FAIL_TTL_S, {})
    return {}


def _wants_form_post(meta: dict) -> bool:
    modes = [str(m).lower() for m in (meta.get("response_modes_supported") or [])]
    if not modes:
        return False
    return "form_post" in modes and "query" not in modes


def _token_auth_methods(meta: dict) -> list[str]:
    return [str(m).lower() for m in (meta.get("token_endpoint_auth_methods_supported") or [])]


def _prefer_basic(meta: dict, has_secret: bool) -> bool:
    if not has_secret:
        return False
    methods = _token_auth_methods(meta)
    if not methods:
        return False
    return "client_secret_basic" in methods and "client_secret_post" not in methods


def build_authorize_url(server: McpServer, user_id: int) -> str:
    """Create a one-time state + PKCE verifier and return the provider authorize URL."""
    if not config.PUBLIC_URL:
        raise OAuthError("TB_PUBLIC_URL must be set for OAuth redirects")
    if not (server.oauth_authorize_url and server.oauth_token_url and server.oauth_client_id):
        raise OAuthError("this server has no OAuth client configured")
    verifier, challenge = _pkce()
    state = pysecrets.token_urlsafe(24)
    with session() as s:
        s.add(McpOAuthState(state=state, mcp_server_id=server.id, user_id=user_id, code_verifier=verifier))
        s.commit()
    params = {
        "response_type": "code",
        "client_id": server.oauth_client_id,
        "redirect_uri": redirect_uri(),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    scopes = normalize_scopes(server.oauth_scopes)
    if scopes:
        params["scope"] = scopes
    if _wants_form_post(authorization_server_metadata(server)):
        params["response_mode"] = "form_post"
    sep = "&" if "?" in server.oauth_authorize_url else "?"
    return server.oauth_authorize_url + sep + urlencode(params)


def take_state(state: str) -> McpOAuthState | None:
    """Consume a state row (one use), returning it if present and not expired."""
    with session() as s:
        row = s.exec(_select_state(state)).first()
        if not row:
            return None
        age = (now() - _aware(row.created_at)).total_seconds()
        s.delete(row)
        s.commit()
    return row if age <= STATE_TTL_S else None


def _select_state(state: str):
    from .db import select
    return select(McpOAuthState).where(McpOAuthState.state == state)


def _aware(dt):
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_token_response(data: dict) -> tuple[str, str, object]:
    """Return (access_token, refresh_token, expires_at|None). Raises if no access token."""
    if not isinstance(data, dict):
        raise OAuthError("token endpoint returned a non-object response")
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        raise OAuthError(f"token endpoint returned no access_token: {str(data)[:200]}")
    refresh = data.get("refresh_token") or ""
    expires_at = None
    exp = data.get("expires_in")
    try:
        if exp is not None:
            expires_at = now() + timedelta(seconds=int(exp))
    except (TypeError, ValueError):
        expires_at = None
    return access, refresh, expires_at


def _basic_header(client_id: str, secret: str) -> str:
    raw = f"{client_id}:{secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _token_headers(basic: bool, client_id: str, secret: str) -> dict:
    headers = {"Accept": "application/json"}
    if basic and secret:
        headers["Authorization"] = _basic_header(client_id, secret)
    return headers


def _token_body(data: dict, client_id: str, secret: str, basic: bool) -> dict:
    body = {**data, "client_id": client_id, "redirect_uri": redirect_uri()}
    if secret and not basic:
        body["client_secret"] = secret
    return body


async def _post_token(url: str, body: dict, headers: dict) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            return await client.post(url, data=body, headers=headers)
    except httpx.HTTPError as e:
        raise OAuthError(f"cannot reach the token endpoint: {e.__class__.__name__}") from e


async def _token_request(server: McpServer, data: dict) -> dict:
    secret = config.decrypt(server.oauth_client_secret_enc) if server.oauth_client_secret_enc else ""
    client_id = server.oauth_client_id
    meta = authorization_server_metadata(server)
    use_basic = _prefer_basic(meta, bool(secret))
    body = _token_body(data, client_id, secret, use_basic)
    headers = _token_headers(use_basic, client_id, secret)
    r = await _post_token(server.oauth_token_url, body, headers)
    if r.status_code in (400, 401) and secret and not use_basic:
        # Some servers (Homey MCP) only accept HTTP Basic; retry once.
        body = _token_body(data, client_id, secret, basic=True)
        headers = _token_headers(True, client_id, secret)
        r = await _post_token(server.oauth_token_url, body, headers)
    if r.status_code >= 400:
        raise OAuthError(f"token endpoint returned HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except ValueError as e:
        raise OAuthError("token endpoint returned non-JSON") from e


async def exchange_code(server: McpServer, code: str, code_verifier: str) -> tuple[str, str, object]:
    data = await _token_request(server, {"grant_type": "authorization_code", "code": code,
                                         "code_verifier": code_verifier})
    return parse_token_response(data)


async def refresh(server: McpServer, refresh_token: str) -> tuple[str, str, object]:
    data = await _token_request(server, {"grant_type": "refresh_token", "refresh_token": refresh_token})
    access, new_refresh, expires_at = parse_token_response(data)
    return access, (new_refresh or refresh_token), expires_at


def needs_refresh(expires_at, refresh_token: str) -> bool:
    if not refresh_token or not expires_at:
        return False
    return (_aware(expires_at) - now()).total_seconds() <= _REFRESH_MARGIN_S
