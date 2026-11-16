"""Rule-based LLM router (Option A in docs/router.md).

A Router endpoint does not call a model itself. For each request it picks an
underlying endpoint+model from an editable rule set: privacy and local-only
bots stay on a local route (Spark); short/simple work goes to Spark or a cheap
model; tool-heavy, long-context or hard work can use a stronger model; a
fallback chain covers errors, timeouts and empty replies; budget at 80% steers
cheaper, and at 100% with blocking only the local route is used.

Do not auto-switch existing bots; a person assigns this endpoint themselves.
"""
from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from typing import Any, Optional

from . import usage
from .db import Bot, Endpoint, select, session
from .llm import LLMError

log = logging.getLogger("teambots.router")

# Sticky destination for the rest of a turn (tool follow-ups stay on the same model).
_sticky: ContextVar[Optional[dict]] = ContextVar("router_sticky", default=None)

POLICY_KEYS = ("privacy", "simple", "normal", "heavy", "budget_80", "budget_100_block")

HARD_KEYWORDS_DEFAULT = (
    "think hard", "think carefully", "deep analysis", "design a system",
    "architect", "reason step by step", "write a thorough", "long design",
)

DEFAULT_SIGNALS = {
    "long_context_tokens": 32000,
    "short_chars": 500,
    "hard_keywords": list(HARD_KEYWORDS_DEFAULT),
}


class RouterError(LLMError):
    """Invalid rules or no usable route."""


def begin_turn() -> None:
    """Clear the sticky route at the start of a turn or a one-off call."""
    _sticky.set(None)


def _text_of(messages: list[dict]) -> str:
    parts = []
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, list):
            parts.append("".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c))
        elif c:
            parts.append(str(c))
    return "\n".join(parts)


def estimate_tokens(messages: list[dict]) -> int:
    """Rough char/4 estimate; good enough for the long-context threshold."""
    return max(1, len(_text_of(messages)) // 4)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


def validate_rules(rules: Any, *, team_id: int | None = None, endpoints: list[Endpoint] | None = None) -> dict:
    """Return a normalised rules object or raise ValueError with a readable message."""
    if isinstance(rules, str):
        try:
            rules = json.loads(rules) if rules.strip() else {}
        except ValueError as e:
            raise ValueError("rules must be valid JSON") from e
    if not isinstance(rules, dict):
        raise ValueError("rules must be a JSON object")
    routes_in = rules.get("routes")
    if not isinstance(routes_in, list) or not routes_in:
        raise ValueError("rules.routes must be a non-empty list")
    names = []
    routes = []
    by_id = None
    if endpoints is not None:
        by_id = {e.id: e for e in endpoints}
    elif team_id is not None:
        with session() as s:
            by_id = {e.id: e for e in s.exec(select(Endpoint).where(Endpoint.team_id == team_id)).all()}
    for i, raw in enumerate(routes_in):
        if not isinstance(raw, dict):
            raise ValueError(f"routes[{i}] must be an object")
        name = str(raw.get("name") or "").strip()
        if not name or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
            raise ValueError(f"routes[{i}].name must be 1-32 letters, digits, _ or -")
        if _norm(name) in {_norm(n) for n in names}:
            raise ValueError(f"duplicate route name {name!r}")
        try:
            eid = int(raw.get("endpoint_id"))
        except (TypeError, ValueError) as e:
            raise ValueError(f"route {name!r} needs an endpoint_id") from e
        if by_id is not None and eid not in by_id:
            raise ValueError(f"route {name!r} points at endpoint {eid}, which is not in this project")
        ep = by_id.get(eid) if by_id is not None else None
        if ep is not None and (getattr(ep, "provider", None) or "openai") == "router":
            raise ValueError(f"route {name!r} cannot point at another Router endpoint")
        names.append(name)
        routes.append({
            "name": name,
            "endpoint_id": eid,
            "model": str(raw.get("model") or "").strip(),
        })
    policy_in = rules.get("policy") if isinstance(rules.get("policy"), dict) else {}
    known = {_norm(r["name"]): r["name"] for r in routes}
    policy = {}
    for key in POLICY_KEYS:
        seq = policy_in.get(key)
        if seq is None:
            # Defaults that still work if the matching route exists.
            fallback = {
                "privacy": ["local"],
                "simple": ["local", "cheap"],
                "normal": ["normal", "cheap", "local"],
                "heavy": ["heavy", "normal", "cheap", "local"],
                "budget_80": ["local", "cheap"],
                "budget_100_block": ["local"],
            }[key]
            seq = [n for n in fallback if _norm(n) in known]
        if not isinstance(seq, list) or not seq:
            raise ValueError(f"policy.{key} must be a non-empty list of route names")
        resolved = []
        for n in seq:
            real = known.get(_norm(str(n)))
            if not real:
                raise ValueError(f"policy.{key} refers to unknown route {n!r}")
            resolved.append(real)
        policy[key] = resolved
    signals_in = rules.get("signals") if isinstance(rules.get("signals"), dict) else {}
    signals = dict(DEFAULT_SIGNALS)
    if "long_context_tokens" in signals_in:
        try:
            n = int(signals_in["long_context_tokens"])
        except (TypeError, ValueError) as e:
            raise ValueError("signals.long_context_tokens must be an integer") from e
        if n < 1000:
            raise ValueError("signals.long_context_tokens must be at least 1000")
        signals["long_context_tokens"] = n
    if "short_chars" in signals_in:
        try:
            n = int(signals_in["short_chars"])
        except (TypeError, ValueError) as e:
            raise ValueError("signals.short_chars must be an integer") from e
        if n < 20:
            raise ValueError("signals.short_chars must be at least 20")
        signals["short_chars"] = n
    if "hard_keywords" in signals_in:
        kw = signals_in["hard_keywords"]
        if not isinstance(kw, list) or not all(isinstance(x, str) and x.strip() for x in kw):
            raise ValueError("signals.hard_keywords must be a list of strings")
        signals["hard_keywords"] = [x.strip() for x in kw]
    return {"version": 1, "routes": routes, "policy": policy, "signals": signals}


def load_rules(ep: Endpoint) -> dict:
    raw = getattr(ep, "rules_json", None) or ""
    with session() as s:
        endpoints = s.exec(select(Endpoint).where(Endpoint.team_id == ep.team_id)).all()
    return validate_rules(raw, team_id=ep.team_id, endpoints=endpoints)


def find_team_endpoints(team_id: int) -> dict[str, Endpoint]:
    """Pick the usual suspects in a project: local Spark, ICT Copilot, optional Claude CLI."""
    with session() as s:
        rows = s.exec(select(Endpoint).where(Endpoint.team_id == team_id)).all()
    found: dict[str, Endpoint] = {}
    for e in rows:
        provider = getattr(e, "provider", None) or "openai"
        if provider == "router":
            continue
        name = (e.name or "").lower()
        if provider == "claude_cli" and "claude_cli" not in found:
            found["claude_cli"] = e
        elif provider == "github_copilot" and "copilot" not in found:
            found["copilot"] = e
        elif (usage.is_local_endpoint(e) or any(w in name for w in ("spark", "dgx"))) and "local" not in found:
            found["local"] = e
    return found


def default_rules_for_team(team_id: int) -> dict:
    """A default Option A rule set from the project's Spark, Copilot and optional Claude CLI endpoints."""
    found = find_team_endpoints(team_id)
    if "local" not in found:
        raise ValueError("this project has no local Spark (or other local) endpoint to use as the privacy route")
    local = found["local"]
    routes = [{"name": "local", "endpoint_id": local.id, "model": local.default_model or ""}]
    policy = {
        "privacy": ["local"],
        "simple": ["local"],
        "normal": ["local"],
        "heavy": ["local"],
        "budget_80": ["local"],
        "budget_100_block": ["local"],
    }
    copilot = found.get("copilot")
    if copilot:
        routes.append({"name": "cheap", "endpoint_id": copilot.id, "model": "claude-haiku-4.5"})
        routes.append({"name": "cheap_nano", "endpoint_id": copilot.id, "model": "gpt-5.4-nano"})
        routes.append({"name": "normal", "endpoint_id": copilot.id, "model": "claude-sonnet-5"})
        routes.append({"name": "heavy", "endpoint_id": copilot.id, "model": "claude-opus-5.5"})
        policy["simple"] = ["local", "cheap", "cheap_nano"]
        policy["normal"] = ["normal", "cheap", "local"]
        policy["heavy"] = ["heavy", "normal", "cheap", "local"]
        policy["budget_80"] = ["local", "cheap", "cheap_nano"]
    cli = found.get("claude_cli")
    if cli:
        routes.append({"name": "claude_cli", "endpoint_id": cli.id, "model": cli.default_model or "sonnet"})
        # Optional extra hop, never for privacy/budget-block.
        for key in ("simple", "normal", "heavy", "budget_80"):
            if "claude_cli" not in policy[key]:
                policy[key] = list(policy[key]) + ["claude_cli"]
    return validate_rules({"version": 1, "routes": routes, "policy": policy, "signals": dict(DEFAULT_SIGNALS)},
                          team_id=team_id)


def _route_by_name(rules: dict, name: str) -> Optional[dict]:
    want = _norm(name)
    for r in rules["routes"]:
        if _norm(r["name"]) == want:
            return r
    return None


def _looks_private(messages: list[dict], bot: Bot | None, ctx: usage.UsageCtx | None) -> tuple[bool, str]:
    if bot is not None and getattr(bot, "local_only", False):
        return True, "bot is marked local-only"
    if bot is not None and getattr(bot, "is_home", False):
        return True, "Overview / home bot stays local"
    origin = (ctx.origin if ctx else "") or ""
    if origin in ("overview", "voice") and bot is not None and getattr(bot, "is_home", False):
        return True, "Overview origin stays local"
    # The system prompt explains the {{secret:NAME}} syntax to every bot, so only the conversation counts.
    text = _text_of([m for m in messages or [] if m.get("role") != "system"])
    if "{{secret:" in text:
        return True, "prompt contains a TeamBots secret reference"
    for m in messages or []:
        name = str(m.get("name") or m.get("tool") or "")
        if m.get("role") == "tool" and re.search(r"get_secret|list_secrets", name, re.I):
            return True, "tool result comes from a secrets tool"
    return False, ""


def _last_user(messages: list[dict]) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
            return str(c or "")
    return ""


def _tool_heavy(tools: list[dict] | None, bot: Bot | None, messages: list[dict] | None = None) -> bool:
    """A large *catalog* of tools is normal in TeamBots; that alone is not a heavy turn.

    We escalate when the bot has computer use, the conversation is already mid
    tool-use, or the person is clearly asking for computer/shell work.
    """
    if bot is not None and getattr(bot, "computer_use", False):
        return True
    for m in messages or []:
        if m.get("role") == "tool" or m.get("tool_calls"):
            return True
    last = _last_user(messages or []).lower()
    cues = ("run this command", "run the command", "open the browser", "use the computer",
            "write a file", "on the screen", "click at", "type into")
    return any(c in last for c in cues)


def classify_tier(rules: dict, messages: list[dict], tools: list[dict] | None,
                  bot: Bot | None, ctx: usage.UsageCtx | None) -> tuple[str, str]:
    """Return (policy key, reason) excluding privacy/budget, which are applied in decide()."""
    hint = (getattr(ctx, "route_hint", None) if ctx else None) or ""
    if hint in POLICY_KEYS and hint not in ("privacy", "budget_80", "budget_100_block"):
        return hint, f"caller asked for the {hint} tier"
    origin = (ctx.origin if ctx else "") or ""
    if origin in ("test", "feed"):
        return "simple", f"origin {origin} uses the cheap tier"
    sig = rules["signals"]
    last = _last_user(messages)
    tokens = estimate_tokens(messages)
    if tokens >= int(sig["long_context_tokens"]):
        return "heavy", f"context is about {tokens} tokens (threshold {sig['long_context_tokens']})"
    blob = (last + "\n" + _text_of(messages[-3:])).lower()
    for kw in sig["hard_keywords"]:
        if kw.lower() in blob:
            return "heavy", f"hard-task keyword {kw!r}"
    if _tool_heavy(tools, bot, messages):
        return "heavy", "tool-heavy or computer-use turn"
    # TeamBots always passes a tool catalog; a short user message is still a simple turn.
    if len(last) <= int(sig["short_chars"]):
        return "simple", f"short message ({len(last)} chars)"
    return "normal", "default mid-tier task"


def decide(rules: dict, *, messages: list[dict], tools: list[dict] | None = None,
           bot: Bot | None = None, team_id: int | None = None,
           forced: str = "") -> dict:
    """Pick a route plus an ordered fallback chain. Honours the turn's sticky route when safe."""
    ctx = usage.current()
    private, private_why = _looks_private(messages, bot, ctx)
    budget = usage.budget_status(team_id) if team_id else {"level": "ok", "block": False}
    level = budget.get("level") or "ok"
    blocked = bool(budget.get("block")) and level == "over"

    if private:
        policy, reason = "privacy", private_why
    elif blocked:
        policy, reason = "budget_100_block", "monthly budget is at 100% and blocking is on"
    elif level in ("warn", "over"):
        policy, reason = "budget_80", f"monthly budget is at {level} (steer cheaper)"
    else:
        policy, reason = classify_tier(rules, messages, tools, bot, ctx)

    if forced:
        dest = _route_by_name(rules, forced)
        if dest and not (private and _norm(dest["name"]) != _norm(rules["policy"]["privacy"][0])):
            if not (blocked and _norm(dest["name"]) not in {_norm(n) for n in rules["policy"]["budget_100_block"]}):
                return {
                    "route": dest,
                    "reason": f"bot model override {forced!r}; {reason}",
                    "policy": policy,
                    "chain": [dest] + [_route_by_name(rules, n) for n in rules["policy"][policy]
                                       if _norm(n) != _norm(dest["name"])],
                }

    names = list(rules["policy"][policy])
    chain = [r for n in names if (r := _route_by_name(rules, n))]
    if not chain:
        raise RouterError(f"no usable route for policy {policy}")

    sticky = _sticky.get()
    if sticky and not private and not blocked:
        # Stay on the same model for tool follow-ups, unless privacy/budget now forbid it.
        allowed = {_norm(r["name"]) for r in chain}
        if _norm(sticky["route"]["name"]) in allowed or policy not in ("privacy", "budget_100_block"):
            # If the sticky route still exists, put it first.
            exist = _route_by_name(rules, sticky["route"]["name"])
            if exist:
                rest = [r for r in chain if _norm(r["name"]) != _norm(exist["name"])]
                return {
                    "route": exist,
                    "reason": "same route as earlier in this turn",
                    "policy": policy,
                    "chain": [exist] + rest,
                }

    return {"route": chain[0], "reason": reason, "policy": policy, "chain": chain}


def _load_bot(bot_id: int | None) -> Optional[Bot]:
    if not bot_id:
        return None
    with session() as s:
        return s.get(Bot, bot_id)


def _load_endpoint(endpoint_id: int) -> Optional[Endpoint]:
    with session() as s:
        return s.get(Endpoint, endpoint_id)


def route_label(dest: dict, ep: Endpoint | None, reason: str) -> str:
    model = dest.get("model") or (ep.default_model if ep else "") or ""
    name = dest.get("name") or "route"
    if model:
        return f"{name} · {model}"
    return name


async def list_models(ep: Endpoint) -> list[dict]:
    try:
        rules = load_rules(ep)
    except ValueError:
        return [{"id": "auto", "name": "auto (rules decide)"}]
    out = [{"id": "auto", "name": "auto (rules decide)"}]
    for r in rules["routes"]:
        label = r["name"] if not r["model"] else f"{r['name']} ({r['model']})"
        out.append({"id": r["name"], "name": label})
    return out


async def chat(ep: Endpoint, model: str, messages: list[dict], tools: list[dict] | None = None,
               max_tokens: int | None = None) -> dict:
    """Choose a route and call the underlying provider, walking the fallback chain on failure."""
    from . import llm
    try:
        rules = load_rules(ep)
    except ValueError as e:
        raise RouterError(f"endpoint '{ep.name}': invalid router rules: {e}") from e
    ctx = usage.current()
    # A new user turn always re-evaluates (billable_request is True on the first step).
    # Sticky only applies to tool follow-ups in the same turn. This also clears a leaked
    # ContextVar when the ASGI server reuses a task between requests.
    if ctx is None or ctx.billable_request:
        begin_turn()
    bot = _load_bot(ctx.bot_id if ctx else None)
    team_id = (ctx.team_id if ctx else None) or ep.team_id
    forced = (model or "").strip()
    if forced.lower() in ("", "auto", "default"):
        forced = ""
    choice = decide(rules, messages=messages, tools=tools, bot=bot, team_id=team_id, forced=forced)
    errors: list[str] = []
    last: Optional[Exception] = None
    for i, dest in enumerate(choice["chain"]):
        if not dest:
            continue
        target = _load_endpoint(dest["endpoint_id"])
        if target is None or (getattr(target, "provider", None) or "openai") == "router":
            errors.append(f"{dest['name']}: endpoint missing")
            continue
        reason = choice["reason"] if i == 0 else "fallback after " + (errors[-1] if errors else "earlier failure")
        label = route_label(dest, target, reason)
        try:
            with usage.bind(route=dest["name"], route_reason=reason, endpoint_id=target.id):
                reply = await llm.chat(target, dest["model"], messages, tools, max_tokens=max_tokens,
                                       _routed=True)
            content = (reply.get("content") or "").strip()
            calls = reply.get("tool_calls") or []
            if not content and not calls:
                errors.append(f"{dest['name']}: empty response")
                last = LLMError("empty response")
                continue
            _sticky.set({"route": dest, "reason": reason})
            reply = dict(reply)
            reply["route"] = dest["name"]
            reply["route_reason"] = reason
            reply["route_label"] = label
            reply["routed_model"] = dest["model"] or target.default_model
            reply["routed_endpoint_id"] = target.id
            return reply
        except LLMError as e:
            errors.append(f"{dest['name']}: {e}")
            last = e
            log.info("router fallback from %s: %s", dest["name"], e)
            continue
    detail = "; ".join(errors) or "no routes to try"
    raise RouterError(f"endpoint '{ep.name}': every route failed ({detail})") from last
