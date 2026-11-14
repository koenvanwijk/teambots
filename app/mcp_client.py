"""Minimal MCP client (streamable HTTP). Each call opens a short-lived session.

Auth is resolved per call: a server can be anonymous, use a shared static token, or
use a per-user token (each member connects their own, so a shared team bot calls the
MCP server as the requester). Tokens live encrypted in the database and never reach
the sandbox or the model.
"""
import json
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from . import config
from .db import McpCredential, McpServer, now, select, session  # noqa: F401 (McpServer for type context)

_cache: dict[tuple[int, int], tuple[float, list[dict]]] = {}


class McpAuthError(Exception):
    """A token is required but missing (not configured, or the user has not connected)."""


def _static_headers(server: McpServer) -> dict:
    return json.loads(config.decrypt(server.headers_enc)) if server.headers_enc else {}


def user_token(server_id: int, user_id: int) -> str:
    with session() as s:
        cred = s.exec(select(McpCredential).where(
            McpCredential.mcp_server_id == server_id, McpCredential.user_id == user_id)).first()
    return config.decrypt(cred.value_enc) if cred and cred.value_enc else ""


def resolve_headers(server: McpServer, user_id: int | None = None) -> dict:
    """Headers for a request, including auth. Raises McpAuthError if a needed token is missing."""
    headers = _static_headers(server)
    mode = getattr(server, "auth_mode", "none") or "none"
    if mode == "token":
        scope = getattr(server, "auth_scope", "shared") or "shared"
        if scope == "user":
            if not user_id:
                raise McpAuthError(f"'{server.name}' needs your personal token")
            value = user_token(server.id, user_id)
            if not value:
                raise McpAuthError(f"connect your account for '{server.name}' in the bot's settings")
        else:
            value = config.decrypt(server.token_enc) if getattr(server, "token_enc", "") else ""
            if not value:
                raise McpAuthError(f"'{server.name}' has no token configured")
        header = getattr(server, "auth_header", None) or "Authorization"
        prefix = (getattr(server, "auth_prefix", "") or "").strip()
        headers[header] = f"{prefix} {value}" if prefix else value
    return headers


async def _oauth_access_token(server: McpServer, user_id: int | None) -> str:
    """A valid OAuth access token for this user, refreshing it when it is about to expire."""
    if not user_id:
        raise McpAuthError(f"'{server.name}' needs you to connect your account")
    with session() as s:
        cred = s.exec(select(McpCredential).where(
            McpCredential.mcp_server_id == server.id, McpCredential.user_id == user_id)).first()
        access = config.decrypt(cred.value_enc) if cred and cred.value_enc else ""
        refresh_tok = config.decrypt(cred.refresh_enc) if cred and cred.refresh_enc else ""
        expires_at = cred.expires_at if cred else None
    if not access:
        raise McpAuthError(f"connect your account for '{server.name}' in the bot's settings")
    from . import mcp_oauth
    if mcp_oauth.needs_refresh(expires_at, refresh_tok):
        try:
            access, refresh_tok, expires_at = await mcp_oauth.refresh(server, refresh_tok)
        except mcp_oauth.OAuthError as e:
            raise McpAuthError(f"'{server.name}': session expired, reconnect your account ({e})") from e
        with session() as s:
            cred = s.exec(select(McpCredential).where(
                McpCredential.mcp_server_id == server.id, McpCredential.user_id == user_id)).first()
            if cred:
                cred.value_enc = config.encrypt(access)
                cred.refresh_enc = config.encrypt(refresh_tok or "")
                cred.expires_at = expires_at
                cred.updated_at = now()
                s.add(cred)
                s.commit()
    return access


async def headers_for(server: McpServer, user_id: int | None) -> dict:
    """Request headers including auth. OAuth refreshes as needed; token/none are resolved inline."""
    if (getattr(server, "auth_mode", "none") or "none") != "oauth":
        return resolve_headers(server, user_id)
    headers = _static_headers(server)
    token = await _oauth_access_token(server, user_id)
    headers[getattr(server, "auth_header", None) or "Authorization"] = f"Bearer {token}"
    return headers


async def list_tools(server: McpServer, user_id: int | None = None, ttl: float = 120) -> list[dict]:
    """Return [{'name', 'description', 'parameters'}]; cached for `ttl` seconds per (server, user)."""
    key = (server.id, user_id or 0)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    headers = await headers_for(server, user_id)
    async with streamablehttp_client(server.url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as sess:
            await sess.initialize()
            res = await sess.list_tools()
    tools = [{"name": t.name, "description": t.description or "",
              "parameters": t.inputSchema or {"type": "object", "properties": {}}} for t in res.tools]
    _cache[key] = (time.time(), tools)
    return tools


async def call_tool(server: McpServer, name: str, arguments: dict, user_id: int | None = None) -> str:
    headers = await headers_for(server, user_id)
    async with streamablehttp_client(server.url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as sess:
            await sess.initialize()
            res = await sess.call_tool(name, arguments)
    parts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
    text = "\n".join(parts) or "(no text content)"
    return ("error: " + text) if res.isError else text


def invalidate(server_id: int) -> None:
    for key in [k for k in _cache if k[0] == server_id]:
        _cache.pop(key, None)
