"""Minimal MCP client (streamable HTTP). Each call opens a short-lived session."""
import json
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from . import config
from .db import McpServer

_cache: dict[int, tuple[float, list[dict]]] = {}


def _headers(server: McpServer) -> dict:
    return json.loads(config.decrypt(server.headers_enc)) if server.headers_enc else {}


async def list_tools(server: McpServer, ttl: float = 120) -> list[dict]:
    """Return [{'name', 'description', 'parameters'}]; cached for `ttl` seconds."""
    hit = _cache.get(server.id)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    async with streamablehttp_client(server.url, headers=_headers(server)) as (read, write, _):
        async with ClientSession(read, write) as sess:
            await sess.initialize()
            res = await sess.list_tools()
    tools = [{"name": t.name, "description": t.description or "",
              "parameters": t.inputSchema or {"type": "object", "properties": {}}} for t in res.tools]
    _cache[server.id] = (time.time(), tools)
    return tools


async def call_tool(server: McpServer, name: str, arguments: dict) -> str:
    async with streamablehttp_client(server.url, headers=_headers(server)) as (read, write, _):
        async with ClientSession(read, write) as sess:
            await sess.initialize()
            res = await sess.call_tool(name, arguments)
    parts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
    text = "\n".join(parts) or "(no text content)"
    return ("error: " + text) if res.isError else text


def invalidate(server_id: int) -> None:
    _cache.pop(server_id, None)
