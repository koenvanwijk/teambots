"""Client for any OpenAI-compatible /chat/completions endpoint (vLLM, Ollama, LiteLLM, ...)."""
import asyncio
import json
import logging
import re

import httpx

from . import config
from .db import Endpoint

log = logging.getLogger("teambots.llm")

LOCAL_5XX_RETRY_S = 1.0
TOOL_RESULT_MAX_CHARS = 4000
HISTORY_TEXT_BUDGET = 32000  # ~8k tokens of text; keeps long chats from ballooning

# Model-name hints that usually accept image content parts.
_VISION_HINTS = (
    "gpt-4o", "gpt-4.1", "gpt-4.5", "gpt-5", "claude", "sonnet", "opus", "haiku",
    "fable", "gemini", "grok", "o3", "o4", "vision", "omni",
)
_NO_VISION_HINTS = ("deepseek", "llama", "mistral", "qwen", "exl3", "flash-1017")


class LLMError(Exception):
    pass


def redact_error(text: str, limit: int = 800) -> str:
    """Strip bearer tokens and obvious secrets from a snippet meant for errors or logs."""
    text = text or ""
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[redacted]", text)
    text = re.sub(r"\b(ghu_|gho_|ghs_|ghr_|github_pat_|tid=)\S+", r"\1…", text)
    text = re.sub(r'(?i)("(?:token|access_token|api_key|client_secret)"\s*:\s*")[^"]+', r"\1[redacted]", text)
    return text[:limit]


def _http_error_detail(text: str) -> str:
    """Prefer a JSON error message, even when the gateway sent it as text/plain."""
    body = (text or "").strip()
    if body.startswith("{") or body.startswith("["):
        try:
            data = json.loads(body)
        except ValueError:
            return body
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if isinstance(err, str) and err.strip():
                return err
            if data.get("message"):
                return str(data["message"])
    return body


def format_http_error(r: httpx.Response, ep_name: str, limit: int = 800) -> str:
    """HTTP status plus the response body (or reason phrase if the body is empty)."""
    body = redact_error(_http_error_detail(r.text or ""), limit)
    reason = (r.reason_phrase or "").strip()
    detail = body or reason or "no body"
    return f"endpoint '{ep_name}' returned HTTP {r.status_code}: {detail}"


def log_http_error(ep_name: str, r: httpx.Response, *, model: str = "", has_images: bool = False,
                   extra: str = "") -> None:
    """Log the status and redacted body; never log Authorization or image payloads."""
    ctype = r.headers.get("content-type", "")
    log.warning(
        "endpoint %s returned HTTP %s %s (model=%s images=%s content-type=%s%s): %s",
        ep_name, r.status_code, (r.reason_phrase or "").strip(),
        model or "-", has_images, ctype, f" {extra}" if extra else "",
        redact_error((r.text or "").strip() or "(empty body)", 800),
    )


def messages_have_images(messages: list[dict] | None) -> bool:
    for m in messages or []:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for p in c:
            if not isinstance(p, dict):
                continue
            if p.get("type") in ("image_url", "image", "input_image") or "image_url" in p or p.get("source"):
                return True
    return False


def model_accepts_images(provider: str, model: str) -> bool:
    """Best-effort: send images only to models that typically have vision."""
    name = (model or "").lower()
    provider = (provider or "openai").lower()
    if any(h in name for h in _NO_VISION_HINTS) and "vision" not in name and "vl" not in name:
        return False
    if provider == "claude_cli":
        return True
    if provider == "github_copilot":
        return not name or any(h in name for h in _VISION_HINTS)
    return any(h in name for h in _VISION_HINTS)


def strip_image_parts(messages: list[dict], note: str = "[image omitted: this model has no vision]") -> list[dict]:
    """Replace image content parts with a short text note; do not copy image payloads."""
    out = []
    for m in messages or []:
        c = m.get("content")
        if not isinstance(c, list):
            out.append(m)
            continue
        parts, had = [], False
        for p in c:
            if isinstance(p, dict) and (p.get("type") in ("image_url", "image", "input_image")
                                        or "image_url" in p or p.get("source")):
                had = True
                continue
            parts.append(p)
        if had:
            text_already = any(isinstance(p, dict) and p.get("type") == "text" and note in (p.get("text") or "")
                               for p in parts)
            if not text_already:
                parts.append({"type": "text", "text": note})
            if len(parts) == 1 and isinstance(parts[0], dict) and parts[0].get("type") == "text":
                nm = {**m, "content": parts[0].get("text") or note}
            else:
                nm = {**m, "content": parts or note}
            out.append(nm)
        else:
            out.append(m)
    return out


def prepare_messages(messages: list[dict], provider: str, model: str) -> list[dict]:
    """Drop image parts when the model cannot use them, so they do not bloat the prompt."""
    if messages_have_images(messages) and not model_accepts_images(provider, model):
        log.info("omitting images for %s model %s (no vision)", provider, model or "(default)")
        return strip_image_parts(messages)
    return messages


def _msg_chars(m: dict) -> int:
    c = m.get("content")
    if isinstance(c, list):
        n = 0
        for p in c:
            if isinstance(p, dict):
                n += len(p.get("text") or "")
            elif isinstance(p, str):
                n += len(p)
        return n + len(m.get("tool_calls") and str(m.get("tool_calls")) or "")
    return len(str(c or "")) + len(m.get("tool_calls") and str(m.get("tool_calls")) or "")


def _has_tool_calls(m: dict) -> bool:
    return bool(m.get("role") == "assistant" and m.get("tool_calls"))


def _drop_leading_tool_groups(kept: list[dict]) -> list[dict]:
    """Drop a leading assistant tool-call group (and orphan tool results).

    GitHub Copilot returns an opaque HTTP 400 "Bad Request" when the messages
    array starts with an assistant message that has tool_calls, even if a user
    message follows later. Orphan tool results at the cut are already invalid.
    """
    while kept:
        if kept[0].get("role") == "tool":
            kept.pop(0)
            continue
        if _has_tool_calls(kept[0]):
            kept.pop(0)
            while kept and kept[0].get("role") == "tool":
                kept.pop(0)
            continue
        break
    return kept


def trim_messages(messages: list[dict], budget: int = HISTORY_TEXT_BUDGET,
                  tool_max: int = TOOL_RESULT_MAX_CHARS) -> list[dict]:
    """Cap tool results and drop the oldest messages when the history is too long.

    The latest user message is always kept. A tool-call group is not split: leading
    orphan tool messages after a cut are dropped, and a leading assistant tool-call
    group is dropped so the request does not start with tool_calls (Copilot 400).
    """
    trimmed = []
    for m in messages or []:
        if m.get("role") == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > tool_max:
            trimmed.append({**m, "content": m["content"][:tool_max] + "\n[truncated]"})
        else:
            trimmed.append(m)
    if sum(_msg_chars(m) for m in trimmed) <= budget:
        kept = list(trimmed)
    else:
        kept, total = [], 0
        for m in reversed(trimmed):
            n = _msg_chars(m)
            if kept and total + n > budget:
                break
            kept.append(m)
            total += n
        kept.reverse()
    kept = _drop_leading_tool_groups(kept)
    if not any(m.get("role") == "user" for m in kept):
        for m in reversed(trimmed):
            if m.get("role") == "user":
                kept.insert(0, m)
                break
        kept = _drop_leading_tool_groups(kept)
    return kept or trimmed[-1:]


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
        messages = trim_messages(prepare_messages(messages, "claude_cli", model or ep.default_model or ""))
        return await claude_cli.chat(ep, model, messages, tools, max_tokens=max_tokens)
    from . import usage as usage_mod
    provider = getattr(ep, "provider", "openai") or "openai"
    model_id = model or ep.default_model or ""
    messages = trim_messages(prepare_messages(messages, provider, model_id))
    budget = max_tokens or config.LLM_MAX_TOKENS
    body: dict = {"model": model_id, "messages": messages, "max_tokens": budget}
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
    has_images = messages_have_images(messages)

    async def _post(client, payload):
        r = await client.post(url, json=payload, headers=headers)
        if r.status_code >= 500 and usage_mod.is_local_endpoint(ep):
            log_http_error(ep.name, r, model=model_id, has_images=has_images, extra="will retry once")
            await asyncio.sleep(LOCAL_5XX_RETRY_S)
            r = await client.post(url, json=payload, headers=headers)
            return r, True
        return r, False

    try:
        try:
            async with httpx.AsyncClient(timeout=config.LLM_TIMEOUT) as client:
                r, retried = await _post(client, body)
                if r.status_code >= 400:
                    log_http_error(ep.name, r, model=model_id, has_images=has_images)
                    raise LLMError(format_http_error(r, ep.name))
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
                    r, extra = await _post(client, body)
                    retried = retried or extra
                    if r.status_code >= 400:
                        log_http_error(ep.name, r, model=model_id, has_images=has_images)
                        raise LLMError(format_http_error(r, ep.name))
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
