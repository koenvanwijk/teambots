"""Client for any OpenAI-compatible /chat/completions endpoint (vLLM, Ollama, LiteLLM, ...)."""
import httpx

from . import config
from .db import Endpoint


class LLMError(Exception):
    pass


async def chat(ep: Endpoint, model: str, messages: list[dict], tools: list[dict] | None = None) -> dict:
    """Return the assistant message dict ({'content': ..., 'tool_calls': [...]})."""
    body: dict = {"model": model or ep.default_model, "messages": messages}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    headers = {"Content-Type": "application/json"}
    key = config.decrypt(ep.api_key_enc)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = ep.base_url.rstrip("/") + "/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=config.LLM_TIMEOUT) as client:
            r = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise LLMError(f"cannot reach endpoint '{ep.name}': {e.__class__.__name__}") from e
    if r.status_code >= 400:
        raise LLMError(f"endpoint '{ep.name}' returned HTTP {r.status_code}: {r.text[:300]}")
    try:
        msg = r.json()["choices"][0]["message"]
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"endpoint '{ep.name}' returned an unexpected response") from e
    return {"content": msg.get("content") or "", "tool_calls": msg.get("tool_calls") or []}
