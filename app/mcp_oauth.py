"""OAuth 2.0 (authorization code + PKCE) for MCP servers, per user.

The admin configures a server's OAuth client once (authorize/token URLs, client id,
optional secret, scopes). Each member authorizes and receives their own access and
refresh tokens, stored encrypted in McpCredential. Works for Athom/Homey, Google and
any standard OAuth2 server. Discovery / dynamic client registration is not used; the
endpoints are configured explicitly.
"""
from __future__ import annotations

import base64
import hashlib
import secrets as pysecrets
from datetime import timedelta
from urllib.parse import urlencode

import httpx

from . import config
from .db import McpOAuthState, McpServer, now, session

STATE_TTL_S = 600


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


async def _token_request(server: McpServer, data: dict) -> dict:
    body = {**data, "client_id": server.oauth_client_id, "redirect_uri": redirect_uri()}
    secret = config.decrypt(server.oauth_client_secret_enc) if server.oauth_client_secret_enc else ""
    if secret:
        body["client_secret"] = secret
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(server.oauth_token_url, data=body,
                                  headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise OAuthError(f"cannot reach the token endpoint: {e.__class__.__name__}") from e
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
