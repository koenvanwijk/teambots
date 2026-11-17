"""Personal coding bots: Claude Code running in the user's own sandbox (`home-u{owner_id}`).

This module holds the bot helpers. app.agent imports this module at its top, so anything here that
needs app.agent must import it lazily inside the function.
"""
from __future__ import annotations

import re

from . import sandbox
from .db import Bot, Conversation, Session, User, select

RUNNER = "claude_code"
DEFAULT_MODEL = "opus"
MODEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


def clean_workdir(raw: str) -> str:
    """Empty stays empty; otherwise a path relative to the workspace. Raises ValueError."""
    raw = (raw or "").strip()
    return sandbox._check_path(raw) if raw else ""


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
