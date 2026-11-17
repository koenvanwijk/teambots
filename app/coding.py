"""Personal coding bots: Claude Code running in the user's own sandbox (`home-u{owner_id}`).

This module holds the bot helpers. app.agent imports this module at its top, so anything here that
needs app.agent must import it lazily inside the function.
"""
from __future__ import annotations

import json
import re

from . import sandbox
from .db import Bot, Conversation, Session, User, select

RUNNER = "claude_code"
DEFAULT_MODEL = "opus"
MODEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
TOOL_RESULT_MAX = 6000


def clean_workdir(raw: str) -> str:
    """Empty stays empty; otherwise a path relative to the workspace. Raises ValueError."""
    raw = (raw or "").strip()
    return sandbox._check_path(raw) if raw else ""


def _tool_result_text(content) -> str:
    """Tool output as plain text: list content joins its text parts, long output is cut short."""
    if isinstance(content, list):
        content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    text = content if isinstance(content, str) else ""
    if len(text) > TOOL_RESULT_MAX:
        text = text[:TOOL_RESULT_MAX] + "\n[... truncated ...]"
    return text


def parse_event(line: str) -> list[dict]:
    """One stream-json line from `claude -p --output-format stream-json` as display items. Unknown lines give []."""
    try:
        ev = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(ev, dict):
        return []
    kind = ev.get("type")
    if kind == "system" and ev.get("subtype") == "init":
        return [{"kind": "session", "session_id": ev.get("session_id", "")}]
    blocks = (ev.get("message") or {}).get("content") or []
    if not isinstance(blocks, list):
        blocks = []
    out = []
    if kind == "assistant":
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                out.append({"kind": "text", "text": b.get("text", "")})
            elif b.get("type") == "tool_use":
                out.append({"kind": "tool_call", "id": b.get("id", ""), "name": b.get("name", ""),
                            "arguments": json.dumps(b.get("input", {}))})
    elif kind == "user":
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                out.append({"kind": "tool_result", "id": b.get("tool_use_id", ""),
                            "content": _tool_result_text(b.get("content"))})
    elif kind == "result":
        cost = ev.get("total_cost_usd")
        out.append({"kind": "result", "text": ev.get("result", ""), "is_error": bool(ev.get("is_error")),
                    "cost_usd": float(cost) if cost is not None else None, "usage": ev.get("usage") or {}})
    return out


def create_bot(s: Session, user: User, name: str, model: str = DEFAULT_MODEL, workdir: str = "",
               instructions: str = "") -> tuple[Bot, Conversation]:
    """Create a personal coding bot with its private chat. The caller commits."""
    bot = Bot(team_id=0, owner_id=user.id, name=name.strip(), handle="", instructions=instructions,
              endpoint_id=0, model=model, is_team=False, published=False, is_home=False,
              computer_use=True, runner=RUNNER, workdir=clean_workdir(workdir))
    s.add(bot)
    s.flush()
    bot.handle = f"code-{bot.id}"
    s.add(bot)
    conv = Conversation(team_id=0, kind="dm", name=bot.name, share_activity=False,
                        user_id=user.id, bot_id=bot.id)
    s.add(conv)
    s.flush()
    return bot, conv


def bot_conv(s: Session, bot: Bot) -> Conversation:
    """The owner's private chat with this coding bot (created when missing)."""
    conv = s.exec(select(Conversation).where(
        Conversation.bot_id == bot.id, Conversation.kind == "dm", Conversation.user_id == bot.owner_id)).first()
    if not conv:
        conv = Conversation(team_id=0, kind="dm", name=bot.name, share_activity=False,
                            user_id=bot.owner_id, bot_id=bot.id)
        s.add(conv)
        s.flush()
    return conv


def bot_out(bot: Bot, conv: Conversation) -> dict:
    return {"id": bot.id, "name": bot.name, "handle": bot.handle, "model": bot.model, "workdir": bot.workdir,
            "instructions": bot.instructions, "runner": bot.runner,
            "conversation": {"id": conv.id, "kind": conv.kind, "team_id": conv.team_id, "name": conv.name}}
