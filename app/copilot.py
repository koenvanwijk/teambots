"""GitHub Copilot as an OpenAI-compatible model provider (github.com and *.ghe.com).

The stored secret is a GitHub OAuth token (typically ghu_…). It is exchanged
for a short-lived Copilot token at https://api.<domain>/copilot_internal/v2/token
and never sent to the sandbox or written to logs. Completions go to the API
base URL from the token response (endpoints.api or proxy-ep), falling back to
https://copilot-api.<domain> (or api.individual.githubcopilot.com on github.com).
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from typing import Optional

import httpx

from . import config
from .db import Endpoint
from .llm import LLMError, parse_message, _choice

INTEGRATION_ID = "vscode-chat"
EDITOR_VERSION = "vscode/1.107.0"
EDITOR_PLUGIN_VERSION = "copilot-chat/0.35.0"
USER_AGENT = "GitHubCopilotChat/0.35.0"
GITHUB_API_VERSION = "2025-04-01"
# Refresh this many seconds before expires_at (OpenClaw uses 5 minutes).
REFRESH_SKEW_S = 300
EXCHANGE_TIMEOUT_S = 30

_GHE = re.compile(r"^[a-z0-9-]+\.ghe\.com$")
_cache: dict[tuple[str, str], dict] = {}
_lock = threading.Lock()


def ide_headers(*, api_version: bool = False) -> dict[str, str]:
    headers = {
        "Accept-Encoding": "identity",
        "Editor-Version": EDITOR_VERSION,
        "Editor-Plugin-Version": EDITOR_PLUGIN_VERSION,
        "User-Agent": USER_AGENT,
        "Copilot-Integration-Id": INTEGRATION_ID,
    }
    if api_version:
        headers["X-Github-Api-Version"] = GITHUB_API_VERSION
    return headers


def completions_headers(messages: list[dict] | None = None) -> dict[str, str]:
    last = (messages or [{}])[-1] if messages else {}
    initiator = "agent" if last.get("role") in ("assistant", "tool") else "user"
    return {
        **ide_headers(),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Openai-Organization": "github-copilot",
        "x-initiator": initiator,
    }


def normalize_domain(raw: str | None) -> str:
    """Bare hostname: github.com or a data-residency tenant (org.ghe.com)."""
    trimmed = (raw or "").strip().lower()
    trimmed = re.sub(r"^https?://", "", trimmed).split("/")[0].split(":")[0]
    if trimmed in ("", "github.com"):
        return "github.com"
    if _GHE.fullmatch(trimmed):
        return trimmed
    raise ValueError("github_domain must be github.com or a data-residency tenant like org.ghe.com")


def token_url(domain: str) -> str:
    return f"https://api.{normalize_domain(domain)}/copilot_internal/v2/token"


def fallback_base(domain: str) -> str:
    domain = normalize_domain(domain)
    if domain == "github.com":
        return "https://api.individual.githubcopilot.com"
    return f"https://copilot-api.{domain}"


def _fingerprint(github_token: str) -> str:
    return hashlib.sha256(github_token.encode()).hexdigest()[:16]


def _expires_ms(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    value = int(value)
    return value * 1000 if value < 10**11 else value


def redact(text: str, limit: int = 300) -> str:
    """Strip bearer tokens and GitHub/Copilot secrets from a snippet meant for errors."""
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[redacted]", text)
    text = re.sub(r"\b(ghu_|gho_|ghs_|ghr_|github_pat_|tid=)[^\s\"']+", r"\1…", text)
    text = re.sub(r"(?i)(\"(?:token|access_token|api_key)\"\s*:\s*\")[^\"]+", r"\1[redacted]", text)
    return text[:limit]


def _base_from_token(token: str) -> Optional[str]:
    m = re.search(r"(?:^|;)\s*proxy-ep=([^;\s]+)", token or "", re.I)
    if not m:
        return None
    host = m.group(1).strip()
    if not host:
        return None
    if not re.match(r"^https?://", host, re.I):
        host = "https://" + host
    try:
        from urllib.parse import urlparse
        parsed = urlparse(host)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        hostname = parsed.hostname
        if hostname.startswith("proxy."):
            hostname = "api." + hostname[6:]
        return f"https://{hostname}"
    except Exception:  # noqa: BLE001
        return None


def _base_from_response(data: dict, token: str, domain: str) -> str:
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if isinstance(endpoints, dict):
        for key in ("api", "api_url", "completions"):
            url = endpoints.get(key)
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url.rstrip("/")
    return _base_from_token(token) or fallback_base(domain)


def parse_token_response(data: dict) -> dict:
    if not isinstance(data, dict):
        raise LLMError("Copilot token response was not an object")
    token = data.get("token")
    if not isinstance(token, str) or not token.strip():
        raise LLMError("Copilot token response missing token")
    expires = _expires_ms(data.get("expires_at") if "expires_at" in data else data.get("expiresAt"))
    if not expires:
        raise LLMError("Copilot token response missing or invalid expires_at")
    domain = ""  # filled by caller
    return {"token": token.strip(), "expires_at_ms": expires, "raw": data}


def _cache_get(domain: str, fingerprint: str) -> Optional[dict]:
    with _lock:
        row = _cache.get((domain, fingerprint))
    if not row:
        return None
    if row["expires_at_ms"] - int(time.time() * 1000) <= REFRESH_SKEW_S * 1000:
        return None
    return row


def _cache_put(domain: str, fingerprint: str, row: dict) -> None:
    with _lock:
        _cache[(domain, fingerprint)] = row


def clear_cache() -> None:
    with _lock:
        _cache.clear()


async def exchange(github_token: str, domain: str, *, client: httpx.AsyncClient | None = None,
                   token_endpoint: str | None = None) -> dict:
    """Exchange a GitHub OAuth token for a short-lived Copilot session.

    Returns {token, expires_at_ms, base_url}. Uses the in-memory cache when the
    cached session is still valid for more than REFRESH_SKEW_S seconds.
    """
    if not (github_token or "").strip():
        raise LLMError("this Copilot endpoint has no GitHub token")
    domain = normalize_domain(domain)
    fp = _fingerprint(github_token)
    cached = _cache_get(domain, fp)
    if cached:
        return cached
    url = token_endpoint or token_url(domain)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {github_token}",
        **ide_headers(api_version=True),
    }
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=EXCHANGE_TIMEOUT_S)
    try:
        r = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        raise LLMError(f"cannot reach Copilot token endpoint: {e.__class__.__name__}") from e
    finally:
        if own:
            await client.aclose()
    if r.status_code >= 400:
        raise LLMError(f"Copilot token exchange failed: HTTP {r.status_code}: {redact(r.text)}")
    try:
        data = r.json()
    except Exception as e:  # noqa: BLE001
        raise LLMError("Copilot token endpoint returned a non-JSON response") from e
    parsed = parse_token_response(data)
    row = {
        "token": parsed["token"],
        "expires_at_ms": parsed["expires_at_ms"],
        "base_url": _base_from_response(data, parsed["token"], domain),
    }
    _cache_put(domain, fp, row)
    return row


def _github_token(ep: Endpoint) -> str:
    return config.decrypt(ep.api_key_enc)


def _domain(ep: Endpoint) -> str:
    return normalize_domain(getattr(ep, "github_domain", None) or "")


def _join(base: str, path: str) -> str:
    base = base.rstrip("/")
    if path.startswith("/"):
        path = path[1:]
    if base.endswith("/v1") and path.startswith("v1/"):
        path = path[3:]
    return f"{base}/{path}"


async def _session(ep: Endpoint, client: httpx.AsyncClient, *, token_endpoint: str | None = None) -> dict:
    return await exchange(_github_token(ep), _domain(ep), client=client, token_endpoint=token_endpoint)


async def list_models(ep: Endpoint, *, client: httpx.AsyncClient | None = None,
                      token_endpoint: str | None = None) -> list[dict]:
    """Best-effort model list from the Copilot API. Empty if /models is unavailable."""
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=config.LLM_TIMEOUT)
    try:
        sess = await _session(ep, client, token_endpoint=token_endpoint)
        headers = {**completions_headers(), "Authorization": f"Bearer {sess['token']}"}
        last_error = None
        for path in ("models", "v1/models"):
            try:
                r = await client.get(_join(sess["base_url"], path), headers=headers)
            except httpx.HTTPError as e:
                last_error = e
                continue
            if r.status_code == 404:
                continue
            if r.status_code >= 400:
                raise LLMError(f"Copilot /models failed: HTTP {r.status_code}: {redact(r.text)}")
            data = r.json()
            rows = data.get("data") if isinstance(data, dict) else data
            if isinstance(data, dict) and not rows:
                rows = data.get("models")
            if not isinstance(rows, list):
                return []
            out = []
            for item in rows:
                if isinstance(item, str):
                    out.append({"id": item})
                elif isinstance(item, dict) and item.get("id"):
                    out.append({"id": item["id"], "name": item.get("name") or item["id"]})
            return out
        if last_error:
            raise LLMError(f"cannot reach Copilot /models: {last_error.__class__.__name__}") from last_error
        return []
    finally:
        if own:
            await client.aclose()


async def chat(ep: Endpoint, model: str, messages: list[dict], tools: list[dict] | None = None,
               max_tokens: int | None = None, *, client: httpx.AsyncClient | None = None,
               token_endpoint: str | None = None) -> dict:
    """Chat completions through Copilot, returning {content, tool_calls}."""
    budget = max_tokens or config.LLM_MAX_TOKENS
    body: dict = {"model": model or ep.default_model, "messages": messages, "max_tokens": budget}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=config.LLM_TIMEOUT)
    try:
        sess = await _session(ep, client, token_endpoint=token_endpoint)
        headers = {**completions_headers(messages), "Authorization": f"Bearer {sess['token']}"}
        url = None
        r = None
        last_status = None
        for path in ("chat/completions", "v1/chat/completions"):
            url = _join(sess["base_url"], path)
            r = await client.post(url, json=body, headers=headers)
            last_status = r.status_code
            if r.status_code != 404:
                break
        if r is None:
            raise LLMError(f"cannot reach Copilot completions at '{ep.name}'")
        if r.status_code >= 400:
            raise LLMError(
                f"endpoint '{ep.name}' returned HTTP {r.status_code}: {redact(r.text)}")
        msg, finish = _choice(r.json(), ep.name)
        parsed = parse_message(msg, finish)
        if parsed["truncated"]:
            body["max_tokens"] = max(budget * 2, 16384)
            r = await client.post(url, json=body, headers=headers)
            if r.status_code >= 400:
                raise LLMError(
                    f"endpoint '{ep.name}' returned HTTP {r.status_code}: {redact(r.text)}")
            msg, finish = _choice(r.json(), ep.name)
            parsed = parse_message(msg, finish)
        return {"content": parsed["content"], "tool_calls": parsed["tool_calls"]}
    except httpx.HTTPError as e:
        raise LLMError(f"cannot reach endpoint '{ep.name}': {e.__class__.__name__}") from e
    finally:
        if own:
            await client.aclose()
