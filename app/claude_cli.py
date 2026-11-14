"""Claude Code CLI as a model provider.

Instead of calling an HTTP endpoint, this provider shells out to the local
``claude`` binary in headless print mode (``claude -p --output-format json``).
It reuses the CLI's own stored Claude Code credentials, so the endpoint needs no
API key, and it can reach models the OpenAI-compatible and Copilot routes do not
expose (for example ``claude-fable-5-2``).

The CLI is kept isolated from the host project: no MCP servers, one turn, and
the working directory is a neutral folder so it does not pick up a repo's
CLAUDE.md. TeamBots tools are offered through a prompt-based JSON protocol
(a fallback, because the CLI is not given MCP or host tools). If the model
answers in plain text, that text is returned as the chat reply.

Limitations:
  * Every reply runs under the one account the CLI is logged in as — a shared
    identity with shared rate and spend limits — which must be present and
    authenticated on the host that runs TeamBots.
  * The account's organization policy (if any) is part of the CLI's context, so
    replies may carry text that policy injects.
  * Cost is taken from the CLI JSON (``total_cost_usd`` / usage) when present;
    otherwise the call is logged as a subscription with unknown price, never a
    guessed API rate.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid

from . import config
from .db import Endpoint
from .llm import LLMError

# The CLI resolves short aliases to the current version at call time; it also
# accepts canonical ids. We offer the aliases plus Fable's canonical id so a
# human can pick a family without tracking exact version strings.
_MODELS = [
    {"id": "fable", "name": "Claude Fable (fable)"},
    {"id": "claude-fable-5-2", "name": "claude-fable-5-2"},
    {"id": "haiku", "name": "Claude Haiku (haiku)"},
    {"id": "sonnet", "name": "Claude Sonnet (sonnet)"},
    {"id": "opus", "name": "Claude Opus (opus)"},
]

_ROLE_LABEL = {"user": "User", "assistant": "Assistant", "tool": "Tool result", "system": "System"}

_TOOL_PROTOCOL = """You can call tools by replying with a single JSON object and no other text:
{"tool_calls": [{"name": "TOOL_NAME", "arguments": { ... }}]}
Do not wrap it in markdown. If you can answer without a tool, reply with normal prose, not JSON.
Available tools:
"""


def _cwd() -> str:
    os.makedirs(config.CLAUDE_CLI_CWD, exist_ok=True)
    return config.CLAUDE_CLI_CWD


def _content_text(content) -> str:
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return content or ""


def _tool_schema_text(tools: list[dict] | None) -> str:
    if not tools:
        return ""
    lines = []
    for t in tools:
        fn = t.get("function") if isinstance(t, dict) else None
        if not isinstance(fn, dict):
            fn = t if isinstance(t, dict) else {}
        name = fn.get("name") or (t.get("name") if isinstance(t, dict) else "")
        if not name:
            continue
        desc = fn.get("description") or ""
        params = fn.get("parameters") or {}
        lines.append(f"- {name}: {desc}\n  parameters: {json.dumps(params, ensure_ascii=False)}")
    if not lines:
        return ""
    return _TOOL_PROTOCOL + "\n".join(lines)


def build_prompt(messages: list[dict], tools: list[dict] | None = None) -> tuple[str, str]:
    """Split OpenAI-style messages into (system prompt, flattened transcript).

    System messages become the CLI ``--system-prompt``; the rest become a plain
    transcript ending in ``Assistant:`` so the model continues the conversation.
    When TeamBots tools are present, their schema is appended to the system prompt.
    """
    system_parts: list[str] = []
    turns: list[str] = []
    for m in messages:
        text = _content_text(m.get("content")).strip()
        role = m.get("role")
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        if role == "assistant" and m.get("tool_calls") and not text:
            names = []
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                names.append(fn.get("name") or "tool")
            text = "Requested tool call: " + ", ".join(names)
        if not text:
            continue
        label = _ROLE_LABEL.get(role, str(role).capitalize())
        if role == "tool" and m.get("tool_call_id"):
            turns.append(f"{label} ({m.get('tool_call_id')}): {text}")
        else:
            turns.append(f"{label}: {text}")
    schema = _tool_schema_text(tools)
    if schema:
        system_parts.append(schema)
    prompt = ("\n\n".join(turns) + "\n\nAssistant:") if turns else ""
    return "\n\n".join(system_parts), prompt


def _int(val) -> int:
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def _float(val):
    try:
        if val is None or val == "":
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def parse_cli_usage(data: dict | None) -> dict:
    """Tokens and optional USD cost from a Claude CLI JSON result. Never guesses."""
    data = data if isinstance(data, dict) else {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    # Newer CLI builds also emit a per-model breakdown.
    if not usage and isinstance(data.get("modelUsage"), dict):
        for item in data["modelUsage"].values():
            if isinstance(item, dict):
                usage = item
                break
    prompt = _int(
        usage.get("input_tokens") or usage.get("inputTokens") or usage.get("prompt_tokens")
    )
    completion = _int(
        usage.get("output_tokens") or usage.get("outputTokens") or usage.get("completion_tokens")
    )
    cached = _int(
        usage.get("cache_read_input_tokens") or usage.get("cacheReadInputTokens")
        or usage.get("cached_tokens")
    )
    cache_write = _int(
        usage.get("cache_creation_input_tokens") or usage.get("cacheCreationInputTokens")
        or usage.get("cache_write_tokens")
    )
    cost = _float(data.get("total_cost_usd") or data.get("totalCostUsd") or usage.get("costUSD") or usage.get("cost_usd"))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": 0,
        "cost_usd": cost,
    }


def _tool_names(tools: list[dict]) -> set:
    allowed = set()
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if not isinstance(fn, dict):
            fn = t if isinstance(t, dict) else {}
        if fn.get("name"):
            allowed.add(fn["name"])
    return allowed


def _json_calls(text: str) -> list:
    """The tool protocol we ask for: {"tool_calls": [{"name", "arguments"}]} (optionally fenced)."""
    raw = text.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", raw)
    if fence:
        raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        # Tolerate a JSON object embedded in otherwise short text.
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return []
        try:
            data = json.loads(raw[start:end + 1])
        except ValueError:
            return []
    if not isinstance(data, dict):
        return []
    calls = data.get("tool_calls") or data.get("toolCalls")
    if data.get("name") and not calls:
        calls = [data]
    return calls if isinstance(calls, list) else []


_INVOKE_RE = re.compile(r'<invoke\s+name="([^"]+)"\s*>(.*?)</invoke>', re.S)
_PARAM_RE = re.compile(r'<parameter\s+name="([^"]+)"\s*>(.*?)</parameter>', re.S)


def _invoke_calls(text: str) -> list:
    """Claude's own native tool-call text, which it emits instead of the JSON protocol often
    enough that we must accept it too:
        <invoke name="TOOL"><parameter name="k">v</parameter>...</invoke>
    Otherwise the raw markup leaks into the chat and the call never runs."""
    out = []
    for name, body in _INVOKE_RE.findall(text or ""):
        args = {}
        for key, val in _PARAM_RE.findall(body):
            val = val.strip()
            try:                     # a value may itself be JSON (number, bool, list, object)
                args[key] = json.loads(val)
            except ValueError:
                args[key] = val
        out.append({"name": name.strip(), "arguments": args})
    return out


def parse_tool_reply(text: str, tools: list[dict] | None) -> list[dict]:
    """Turn a model reply into OpenAI-style tool_calls, accepting either the JSON tool protocol
    or Claude's native <invoke> text format."""
    if not tools or not (text or "").strip():
        return []
    allowed = _tool_names(tools)
    calls = _json_calls(text) or _invoke_calls(text)
    out = []
    for c in calls:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") if isinstance(c.get("function"), dict) else c
        name = fn.get("name") or ""
        if not name or (allowed and name not in allowed):
            continue
        args = fn.get("arguments") if "arguments" in fn else fn.get("args")
        if args is None:
            args = {k: v for k, v in fn.items() if k not in ("name", "id")}
        if not isinstance(args, str):
            args = json.dumps(args if args is not None else {}, ensure_ascii=False)
        out.append({
            "id": c.get("id") or f"call_{uuid.uuid4().hex[:10]}",
            "type": "function",
            "function": {"name": name, "arguments": args},
        })
    return out


async def list_models(ep: Endpoint) -> list[dict]:  # noqa: ARG001 - signature parity with llm.list_models
    return [dict(m) for m in _MODELS]


def _cli_argv(chosen: str, system_prompt: str) -> list[str]:
    argv = [config.CLAUDE_CLI_BIN, "-p",
            "--output-format", "json",
            "--max-turns", "1",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--tools", "",  # disable the CLI's built-in tools: no host Bash/Edit, and no
                            # tool turn that --max-turns 1 would cut off (error_max_turns).
                            # TeamBots tools are offered via the JSON protocol in the prompt.
            "--permission-mode", "dontAsk"]
    if chosen:
        argv += ["--model", chosen]
    if system_prompt:
        argv += ["--system-prompt", system_prompt]
    return argv


async def chat(ep: Endpoint, model: str, messages: list[dict], tools: list[dict] | None = None,
               max_tokens: int | None = None) -> dict:  # noqa: ARG001 - max_tokens unused by the CLI
    """Run one headless completion through the Claude CLI. Returns {content, tool_calls} and logs usage."""
    from . import usage
    system_prompt, prompt = build_prompt(messages, tools)
    chosen = (model or ep.default_model or "").strip()
    timer = usage.Timer()
    tokens: dict = {}
    if not prompt.strip():
        usage.log_call(endpoint=ep, model=chosen, tokens=tokens, latency_ms=timer.ms(),
                       success=True, notes="empty prompt; CLI not invoked")
        return {"content": "", "tool_calls": []}
    argv = _cli_argv(chosen, system_prompt)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=_cwd(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError as e:
        usage.log_call(endpoint=ep, model=chosen, tokens=tokens, latency_ms=timer.ms(),
                       success=False, error="claude CLI not found")
        raise LLMError("claude CLI not found; set TB_CLAUDE_CLI to its path") from e
    except OSError as e:
        usage.log_call(endpoint=ep, model=chosen, tokens=tokens, latency_ms=timer.ms(),
                       success=False, error=e.__class__.__name__)
        raise LLMError(f"cannot start the claude CLI: {e.__class__.__name__}") from e
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(prompt.encode()), timeout=config.LLM_TIMEOUT)
    except asyncio.TimeoutError as e:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        usage.log_call(endpoint=ep, model=chosen, tokens=tokens, latency_ms=timer.ms(),
                       success=False, error="claude CLI timed out")
        raise LLMError(f"endpoint '{ep.name}': the claude CLI timed out") from e
    # Parse the JSON result first: the CLI writes a structured result (with a
    # helpful message) to stdout even on a non-zero exit — e.g. a rate or spend
    # limit returns HTTP 429 with exit code 1 and the reason in `result`.
    out_text = out.decode(errors="replace")
    data = None
    if out_text.strip():
        try:
            parsed = json.loads(out_text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            data = parsed
    if data is None:
        # No usable JSON on stdout: fall back to stderr / exit code.
        if proc.returncode != 0:
            detail = (err or b"").decode(errors="replace").strip() or f"exit code {proc.returncode}"
            usage.log_call(endpoint=ep, model=chosen, tokens=tokens, latency_ms=timer.ms(),
                           success=False, error=detail[:300])
            raise LLMError(f"endpoint '{ep.name}': claude CLI failed: {detail[:300]}")
        usage.log_call(endpoint=ep, model=chosen, tokens=tokens, latency_ms=timer.ms(),
                       success=False, error="claude CLI returned non-JSON output")
        raise LLMError(f"endpoint '{ep.name}': claude CLI returned non-JSON output")
    tokens = parse_cli_usage(data)
    result = data.get("result")
    # A non-zero exit or an is_error flag both mean failure; prefer the CLI's own
    # message (the spend-limit text, a model error, ...) over a bare exit code.
    if data.get("is_error") or proc.returncode != 0:
        reason = result if isinstance(result, str) and result else (
            data.get("subtype") or f"claude CLI error (exit code {proc.returncode})")
        usage.log_call(endpoint=ep, model=chosen, tokens=tokens, latency_ms=timer.ms(),
                       success=False, error=str(reason)[:300],
                       reported_cost_usd=tokens.get("cost_usd"))
        raise LLMError(f"endpoint '{ep.name}': {str(reason)[:300]}")
    text = result.strip() if isinstance(result, str) else ""
    calls = parse_tool_reply(text, tools)
    usage.log_call(endpoint=ep, model=chosen, tokens=tokens, latency_ms=timer.ms(),
                   success=True, reported_cost_usd=tokens.get("cost_usd"))
    if calls:
        return {"content": "", "tool_calls": calls}
    return {"content": text, "tool_calls": []}
