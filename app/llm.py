"""Client for any OpenAI-compatible /chat/completions endpoint (vLLM, Ollama, LiteLLM, ...)."""
import re

import httpx

from . import config
from .db import Endpoint


class LLMError(Exception):
    pass


_THINK = re.compile(r"<think>.*?</think>", re.S)


def _text_from_content(content) -> str:
    """Visible text from a chat message content field (string or content parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(p.get("text") or (p.get("content") if isinstance(p.get("content"), str) else "") or "")
        text = "".join(parts)
    else:
        text = str(content)
    return _THINK.sub("", text).strip()


def parse_message(msg: dict, finish_reason: str | None = None) -> dict:
    """Pick the user-visible answer out of a chat-completions message.

    `reasoning_content` / `reasoning` is never used as the answer. Empty
    `content` with a length finish (typical when a thinking model used up
    max_tokens on reasoning) is marked truncated so the caller can retry.
    """
    content = _text_from_content(msg.get("content"))
    tool_calls = msg.get("tool_calls") or []
    had_reasoning = bool(msg.get("reasoning_content") or msg.get("reasoning"))
    reason = finish_reason or ""
    truncated = (not content and not tool_calls
                 and (reason == "length" or (had_reasoning and reason in ("", "length", "stop"))))
    return {"content": content, "tool_calls": tool_calls, "finish_reason": reason,
            "truncated": truncated, "had_reasoning": had_reasoning}


def _choice(data: dict, ep_name: str) -> tuple[dict, str]:
    try:
        ch = data["choices"][0]
        return ch["message"], ch.get("finish_reason") or ""
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"endpoint '{ep_name}' returned an unexpected response") from e


def _parse_model_list(data) -> list[dict]:
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


def _store_models(ep: Endpoint, models: list[dict]) -> None:
    if not models or not getattr(ep, "id", None):
        return
    from . import usage
    from .db import session
    with session() as s:
        row = s.get(Endpoint, ep.id)
        if not row:
            return
        usage.cache_models(row, models)
        s.add(row)
        s.commit()
        ep.models_json = row.models_json
        ep.models_fetched_at = row.models_fetched_at


async def list_models(ep: Endpoint, *, refresh: bool = False) -> list[dict]:
    """Best-effort model catalog. Falls back to the last cached list if /models fails."""
    from . import usage
    cached = usage.cached_models(ep)
    try:
        if getattr(ep, "provider", "openai") == "github_copilot":
            from . import copilot
            out = await copilot.list_models(ep)
        elif getattr(ep, "provider", "openai") == "claude_cli":
            from . import claude_cli
            out = await claude_cli.list_models(ep)
        elif getattr(ep, "provider", "openai") == "router":
            from . import llm_router
            out = await llm_router.list_models(ep)
        else:
            headers = {"Accept": "application/json"}
            key = config.decrypt(ep.api_key_enc)
            if key:
                headers["Authorization"] = f"Bearer {key}"
            url = ep.base_url.rstrip("/") + "/models"
            async with httpx.AsyncClient(timeout=config.LLM_TIMEOUT) as client:
                r = await client.get(url, headers=headers)
                if r.status_code == 404:
                    out = []
                elif r.status_code >= 400:
                    raise LLMError(f"endpoint '{ep.name}' /models returned HTTP {r.status_code}: {r.text[:300]}")
                else:
                    out = _parse_model_list(r.json())
        if out:
            _store_models(ep, out)
            return out
        return cached
    except (LLMError, httpx.HTTPError):
        if cached and not refresh:
            return cached
        if isinstance(cached, list) and not refresh:
            return cached
        raise


async def chat(ep: Endpoint, model: str, messages: list[dict], tools: list[dict] | None = None,
               max_tokens: int | None = None, _routed: bool = False) -> dict:
    """Return the assistant message dict ({'content': ..., 'tool_calls': [...]})."""
    from . import usage
    if (getattr(ep, "provider", "openai") == "router") and not _routed:
        from . import llm_router
        return await llm_router.chat(ep, model, messages, tools, max_tokens=max_tokens)
    if getattr(ep, "provider", "openai") == "github_copilot":
        from . import copilot
        return await copilot.chat(ep, model, messages, tools, max_tokens=max_tokens)
    if getattr(ep, "provider", "openai") == "claude_cli":
        from . import claude_cli
        return await claude_cli.chat(ep, model, messages, tools, max_tokens=max_tokens)
    budget = max_tokens or config.LLM_MAX_TOKENS
    body: dict = {"model": model or ep.default_model, "messages": messages, "max_tokens": budget}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    headers = {"Content-Type": "application/json"}
    key = config.decrypt(ep.api_key_enc)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = ep.base_url.rstrip("/") + "/chat/completions"
    timer = usage.Timer()
    used = {}
    retried = False
    try:
        try:
            async with httpx.AsyncClient(timeout=config.LLM_TIMEOUT) as client:
                r = await client.post(url, json=body, headers=headers)
                if r.status_code >= 400:
                    raise LLMError(f"endpoint '{ep.name}' returned HTTP {r.status_code}: {r.text[:300]}")
                data = r.json()
                used = usage.parse_usage(data)
                msg, finish = _choice(data, ep.name)
                parsed = parse_message(msg, finish)
                # Thinking used up the budget: one retry with a larger cap, still never surface reasoning.
                if parsed["truncated"]:
                    usage.log_call(endpoint=ep, model=body["model"], tokens=used,
                                   latency_ms=timer.ms(), success=True, retry=False)
                    retried = True
                    timer = usage.Timer()
                    body["max_tokens"] = max(budget * 2, 16384)
                    r = await client.post(url, json=body, headers=headers)
                    if r.status_code >= 400:
                        raise LLMError(f"endpoint '{ep.name}' returned HTTP {r.status_code}: {r.text[:300]}")
                    data = r.json()
                    used = usage.parse_usage(data)
                    msg, finish = _choice(data, ep.name)
                    parsed = parse_message(msg, finish)
        except httpx.HTTPError as e:
            raise LLMError(f"cannot reach endpoint '{ep.name}': {e.__class__.__name__}") from e
    except LLMError as err:
        usage.log_call(endpoint=ep, model=body["model"], tokens=used, latency_ms=timer.ms(),
                       success=False, error=str(err), retry=retried)
        raise
    usage.log_call(endpoint=ep, model=body["model"], tokens=used, latency_ms=timer.ms(),
                   success=True, retry=retried)
    return {"content": parsed["content"], "tool_calls": parsed["tool_calls"], "usage": used}
