"""Secrets stay outside the sandbox and out of the model's context.

The model only knows a secret's name. It writes the placeholder {{secret:NAME}} into an
`http_request` call; this module, running in the app, swaps in the real value just before the
request leaves, and only when the destination host is one the secret is bound to. Everything
that flows back to the model is masked.
"""
import ipaddress
import re
import socket
from urllib.parse import urlsplit

import httpx

from . import config
from .db import Secret, select, session

PLACEHOLDER = re.compile(r"\{\{secret:([A-Z_][A-Z0-9_]*)\}\}")
MAX_BODY = 6000


def load(bot_id: int) -> dict[str, tuple[str, list[str]]]:
    """name -> (value, bound hosts)"""
    with session() as s:
        rows = s.exec(select(Secret).where(Secret.bot_id == bot_id)).all()
    return {r.name: (config.decrypt(r.value_enc), [h.strip().lower() for h in r.hosts.split(",") if h.strip()])
            for r in rows}


def mask(text: str, bot_id: int) -> str:
    """Replace any secret value that shows up in tool output before the model sees it."""
    for name, (value, _) in load(bot_id).items():
        if len(value) >= 4 and value in text:
            text = text.replace(value, f"[secret:{name}]")
    return text


def _host_allowed(host: str, bound: list[str]) -> bool:
    return any(host == b or host.endswith("." + b) for b in bound)


def _is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


async def http_request(bot_id: int, method: str, url: str, headers: dict | None = None, body: str = "") -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return "error: url must be http(s)"
    if not config.HTTP_ALLOW_PRIVATE and not _is_public(host):
        return "error: this host is not a public address; internal addresses are blocked"
    secrets = load(bot_id)
    used: set[str] = set()

    def fill(text: str) -> str:
        def repl(m: re.Match) -> str:
            name = m.group(1)
            if name not in secrets:
                raise PermissionError(f"unknown secret {name}")
            value, bound = secrets[name]
            if not _host_allowed(host, bound):
                raise PermissionError(f"secret {name} is not bound to host {host}")
            if parts.scheme != "https" and not config.HTTP_ALLOW_PRIVATE:
                raise PermissionError("secrets are only sent over https")
            used.add(name)
            return value
        return PLACEHOLDER.sub(repl, text)

    try:
        real_headers = {k: fill(str(v)) for k, v in (headers or {}).items()}
        real_url, real_body = fill(url), fill(body or "")
    except PermissionError as e:
        return f"error: {e}; nothing was sent"
    try:
        # No redirects: a redirect could carry the secret to a host it is not bound to.
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            r = await client.request(method.upper(), real_url, headers=real_headers, content=real_body or None)
    except httpx.HTTPError as e:
        return f"error: request failed ({e.__class__.__name__})"
    text = r.text
    if len(text) > MAX_BODY:
        text = text[:MAX_BODY] + "\n[... truncated ...]"
    location = f"\nLocation: {r.headers['location']}" if "location" in r.headers else ""
    return mask(f"HTTP {r.status_code}{location}\n{text}", bot_id)
