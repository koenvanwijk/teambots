"""Secrets stay outside the sandbox and out of the model's context.

The model only knows a secret's name. It writes the placeholder {{secret:NAME}} into an
`http_request` or `git_push` call; this module, running in the app, swaps in the real value just
before the request leaves, and only when the destination host is one the secret is bound to.
Everything that flows back to the model is masked.
"""
import ipaddress
import re
import socket
from urllib.parse import urlsplit

import httpx

from . import config, sandbox
from .db import Bot, Secret, select, session

PLACEHOLDER = re.compile(r"\{\{secret:([A-Z_][A-Z0-9_]*)\}\}")
MAX_BODY = 6000


def rows_for(s, bot: Bot | None) -> list[Secret]:
    """The secrets a bot can use: its project's secrets, then its own (a bot secret of the same
    name wins). Home bots belong to no project and only have their own."""
    if not bot:
        return []
    own = s.exec(select(Secret).where(Secret.bot_id == bot.id)).all()
    shared = [] if bot.is_home or not bot.team_id else s.exec(
        select(Secret).where(Secret.team_id == bot.team_id, Secret.bot_id == 0)).all()
    names = {r.name for r in own}
    return [r for r in shared if r.name not in names] + list(own)


def load(bot_id: int) -> dict[str, tuple[str, list[str]]]:
    """name -> (value, bound hosts)"""
    with session() as s:
        bot = s.get(Bot, bot_id)
        rows = rows_for(s, bot) if bot else s.exec(select(Secret).where(Secret.bot_id == bot_id)).all()
    return {r.name: (config.decrypt(r.value_enc), [h.strip().lower() for h in r.hosts.split(",") if h.strip()])
            for r in rows}


def mask(text: str, bot_id: int) -> str:
    """Replace any secret value that shows up in tool output before the model sees it."""
    for name, (value, _) in load(bot_id).items():
        if len(value) >= 4 and value in text:
            text = text.replace(value, f"[secret:{name}]")
    return text


# What a pasted credential looks like: (kind, suggested secret name, suggested host, pattern).
# When a pattern has a group, the group is the value; otherwise the whole match is.
LEAKS = [(kind, name, host, re.compile(rx)) for kind, name, host, rx in [
    ("private key", "PRIVATE_KEY", "", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"),
    ("GitHub token", "GITHUB_TOKEN", "github.com,api.github.com", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    ("GitLab token", "GITLAB_TOKEN", "gitlab.com", r"\bglpat-[A-Za-z0-9_-]{20,}"),
    ("Slack token", "SLACK_TOKEN", "slack.com", r"\bxox[abeprs]-[A-Za-z0-9-]{10,}"),
    ("AWS access key", "AWS_ACCESS_KEY_ID", "", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("Google API key", "GOOGLE_API_KEY", "googleapis.com", r"\bAIza[0-9A-Za-z_-]{35}"),
    ("API key", "API_KEY", "", r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{20,}"),
    ("JSON web token", "ACCESS_TOKEN", "", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("bearer token", "ACCESS_TOKEN", "", r"(?i)\bbearer\s+([A-Za-z0-9._~+/-]{20,}=*)"),
    ("password", "PASSWORD", "", r"(?i)\b(?:password|passwd|pwd|wachtwoord)\b\s*(?:is|[:=])\s*[\"']?([^\s\"'{}]{6,})"),
    ("token", "API_TOKEN", "", r"(?i)\b(?:token|secret|api[_-]?key)\b\s*(?:is|[:=])\s*[\"']?"
                               r"((?=[^\s\"'{}]*\d)(?=[^\s\"'{}]*[A-Za-z])[^\s\"'{}]{12,})"),
]]


def find_leaks(text: str, bot_ids: list[int]) -> list[dict]:
    """Credentials a person typed into a message: values of stored secrets of these bots, and
    strings that look like a token, key or password. A secret in the chat is stored with the
    conversation and read by the model, which is what this module exists to prevent."""
    spans: list[tuple[int, int, dict]] = []
    for bot_id in bot_ids:
        for name, (value, _) in load(bot_id).items():
            at = text.find(value) if len(value) >= 6 else -1
            if at >= 0:
                spans.append((at, at + len(value), {"kind": "stored secret", "value": value, "name": name}))
    for kind, name, host, rx in LEAKS:
        for m in rx.finditer(text):
            g = 1 if m.groups() else 0
            spans.append((m.start(g), m.end(g), {"kind": kind, "value": m.group(g), "suggest": name, "hosts": host}))
    found, end = [], -1
    for start, stop, item in sorted(spans, key=lambda x: (x[0], -x[1])):
        if start >= end:  # first and longest wins where two patterns overlap
            found.append(item)
            end = stop
    return found


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


async def git_push(bot_id: int, sandbox_key: str, remote_url: str, branch: str, token: str,
                   path: str | None = ".", username: str | None = None) -> str:
    """Resolve one stored token and send it only to the dedicated Git worker.

    Unlike HTTP placeholders, `token` must be exactly one placeholder.  This
    keeps a credential from being mixed into a URL/refspec and lets the approval
    card show only its stored name.
    """
    try:
        remote_url, host = sandbox.git_push_remote(remote_url)
        branch = sandbox.git_push_branch(branch)
        path = sandbox.git_push_path(path)
        username = sandbox.git_push_username(username)
    except ValueError as e:
        return f"error: {e}; nothing was pushed"
    if not config.GIT_PUSH_ALLOW_PRIVATE and not _is_public(host):
        return "error: this Git host is not a public address; nothing was pushed"
    if not isinstance(token, str) or not (match := PLACEHOLDER.fullmatch(token)):
        return "error: token must be exactly one {{secret:NAME}} placeholder; nothing was pushed"
    name = match.group(1)
    secrets = load(bot_id)
    if name not in secrets:
        return f"error: unknown secret {name}; nothing was pushed"
    value, bound = secrets[name]
    # A git write credential is deliberately stricter than a generic HTTP
    # secret: a binding for example.com does not authorize every subdomain.
    if host not in bound:
        return f"error: secret {name} is not bound exactly to Git host {host}; nothing was pushed"
    if config.SANDBOX_BACKEND != "docker":
        return "error: git_push requires the Docker sandbox to keep the token out of the shared workspace"
    try:
        result = await sandbox.git_push(sandbox_key, remote_url, branch, value, path, username)
    except ValueError as e:
        return f"error: {e}; nothing was pushed"
    return mask(result, bot_id)
