"""Background learning, Hermes-style: the bot gets better without anyone asking.

* Learning review (Hermes: agent/background_review.py). After every ``REVIEW_EVERY_TURNS`` turns in
  a conversation, or after a turn with ``REVIEW_TOOL_CALLS`` or more tool calls, a side call reads the
  recent conversation and may update team memory, the person's profile and skills with the same
  ``memory`` and ``skill_manage`` tools the bot has. Its writes go through the knowledge base as
  "learning-review (bot)", based on the files as they were when the review started: when a person or
  another bot changed a file since, that write is skipped (it never overwrites their change).
* Rolling summary. When older messages no longer fit in the history the model gets, they are
  summarized into ``Conversation.summary``; the prompt shows it as "Earlier in this conversation".
* ``search_history``: find something in earlier chats the person can read.

The side calls run on the project's Spark (local) endpoint when there is one, otherwise on the bot's
own endpoint (a Router then picks as usual). They never block a chat and fail silently.
"""
from __future__ import annotations

import asyncio
import json
import logging

from . import config, llm
from .access import can_read_conversation
from .db import Bot, Conversation, Endpoint, Message, Team, User, select, session

log = logging.getLogger("teambots.learning")

REVIEW_EVERY_TURNS = 8
REVIEW_TOOL_CALLS = 6
REVIEW_TRANSCRIPT_CHARS = 12000
SUMMARY_MIN_CHARS = 1500
SUMMARY_MAX_CHARS = 2500
SUMMARY_INPUT_CHARS = 24000
HISTORY_MESSAGES = 80  # agent.build_history reads this many messages

_turns: dict[tuple[int, int], int] = {}
_busy: set[tuple[str, int, int]] = set()

REVIEW_PROMPT = (
    "You review a finished stretch of conversation between a person and {bot}, an AI teammate, to help it "
    "improve. Decide whether anything is worth keeping for later and save it with the tools:\n"
    "- `memory` target 'team': a fact everyone in the project should know in every conversation.\n"
    "- `memory` target 'user': a lasting fact or preference about this person (not in team chats).\n"
    "- `skill_manage`: steps for a task that worked and will come back (create), or a fix to a skill that was "
    "wrong or missed a step (patch).\n"
    "Save facts, not orders. Do not save task progress, one-off details, logs or anything already listed below. "
    "Keep memory within its budget: merge or replace stale entries in the same batch. Make at most a few calls. "
    "If nothing qualifies, reply with exactly: nothing.\n\n"
    "Current memory and skills:\n{state}")

SUMMARY_PROMPT = (
    "You summarize the older part of a conversation for an AI teammate whose chat history no longer holds "
    "it. Write a compact summary (at most {limit} characters) in the conversation's language: decisions, "
    "facts, open questions and promises, who asked what. No preamble. If there is an earlier summary, merge "
    "it in.")


def _enabled() -> bool:
    return config.LEARNING


def side_endpoint(bot: Bot) -> tuple[Endpoint | None, str]:
    """Spark when the project has one, otherwise the bot's own endpoint and model."""
    from .llm_router import find_team_endpoints
    try:
        local = find_team_endpoints(bot.team_id).get("local") if bot.team_id else None
    except Exception:  # noqa: BLE001
        local = None
    if local is not None:
        return local, local.default_model or ""
    with session() as s:
        ep = s.get(Endpoint, bot.endpoint_id) if bot.endpoint_id else None
    return ep, bot.model


def _spawn(kind: str, conv_id: int, bot_id: int, coro) -> None:
    key = (kind, conv_id, bot_id)
    if key in _busy:
        coro.close()
        return
    _busy.add(key)

    async def run():
        try:
            await coro
        except Exception:  # noqa: BLE001 - background learning must never surface
            log.debug("%s for conversation %s failed", kind, conv_id, exc_info=True)
        finally:
            _busy.discard(key)
    from .agent import spawn
    spawn(run())


def after_turn(conv_id: int, bot_id: int, requester_id: int) -> None:
    """Called when a turn ends with an answer. Starts background work when it is due; never raises."""
    if not _enabled():
        return
    try:
        with session() as s:
            conv = s.get(Conversation, conv_id)
            if conv is None or conv.kind == "feed":
                return
            rows = list(s.exec(select(Message).where(Message.conversation_id == conv_id)
                               .order_by(Message.id.desc()).limit(60)).all())
        tool_calls = 0
        for m in rows:  # newest first, until the message that started this turn
            if m.role == "user":
                break
            if m.author_bot_id == bot_id and m.tool_calls:
                try:
                    tool_calls += len(json.loads(m.tool_calls))
                except ValueError:
                    pass
        key = (conv_id, bot_id)
        _turns[key] = _turns.get(key, 0) + 1
        if _turns[key] >= REVIEW_EVERY_TURNS or tool_calls >= REVIEW_TOOL_CALLS:
            _turns[key] = 0
            _spawn("review", conv_id, bot_id, review(conv_id, bot_id, requester_id))
        _spawn("summary", conv_id, bot_id, summarize(conv_id, bot_id))
    except Exception:  # noqa: BLE001
        log.debug("after_turn failed", exc_info=True)


# ---------------------------------------------------------------- learning review

def _transcript(rows: list[Message], names: dict, bots: dict, limit: int) -> str:
    lines, size = [], 0
    for m in reversed(rows):  # newest first, so the budget keeps the most recent part
        if m.role == "tool" or not (m.content or "").strip():
            if m.tool_calls:
                try:
                    called = ", ".join(c["function"]["name"] for c in json.loads(m.tool_calls))
                except (ValueError, KeyError, TypeError):
                    called = "tools"
                line = f"[{bots.get(m.author_bot_id, 'bot')} used: {called}]"
            else:
                continue
        elif m.role == "user":
            line = f"{names.get(m.author_user_id, 'person')}: {m.content.strip()}"
        else:
            line = f"{bots.get(m.author_bot_id, 'bot')}: {m.content.strip()}"
        line = line[:2000]
        if size + len(line) > limit and lines:
            break
        lines.append(line)
        size += len(line)
    return "\n".join(reversed(lines))


async def review(conv_id: int, bot_id: int, requester_id: int) -> int:
    """One learning pass. Returns the number of memory/skill changes it made."""
    from . import agent, memory, usage
    from . import knowledge as kb
    with session() as s:
        conv, bot, user = s.get(Conversation, conv_id), s.get(Bot, bot_id), s.get(User, requester_id)
        if not (conv and bot and user):
            return 0
        rows = list(reversed(s.exec(select(Message).where(Message.conversation_id == conv_id)
                                    .order_by(Message.id.desc()).limit(120)).all()))
        names = {u.id: u.display_name for u in s.exec(select(User)).all()}
        bots = {b.id: b.name for b in s.exec(select(Bot).where(Bot.team_id == bot.team_id)).all()}
        channel = conv.kind == "channel"
    key = kb.key_for(bot)
    snap = kb.snapshot(key)  # writes are based on this; later changes by others win
    state = "\n\n".join(x for x in (
        memory.render_team(key)[0],
        "" if channel else memory.render_user(key, user),
        "Skills: " + (", ".join(f"{k['name']} ({k['description']})" for k in memory.skills(key)) or "none"),
    ) if x)
    if usage.should_block(bot.team_id):
        return 0
    text = _transcript(rows, names, bots, REVIEW_TRANSCRIPT_CHARS)
    if not text:
        return 0
    ep, model = side_endpoint(bot)
    if ep is None:
        return 0
    allowed = {"memory", "skill_manage"}
    specs = [t for t in agent.BUILTIN_TOOLS if t["function"]["name"] in allowed]
    messages = [{"role": "system", "content": REVIEW_PROMPT.format(bot=bot.name, state=state)},
                {"role": "user", "content": "Conversation" + (" (a team chat)" if channel else "") + ":\n\n" + text}]
    ctx = agent.Ctx(bot.id, user.id, conv_id, channel, team_id=bot.team_id, is_home=bot.is_home)
    changes = 0
    t_author, t_pinned = kb.acting_as.set(kb.LEARNING), kb.pinned.set(snap)
    try:
        changes = await _review_rounds(ep, model, messages, specs, allowed, ctx, user, bot, conv_id)
    finally:
        kb.acting_as.reset(t_author)
        kb.pinned.reset(t_pinned)
    if changes:
        log.info("learning review in conversation %s made %d change(s)", conv_id, changes)
    return changes


async def _review_rounds(ep, model, messages, specs, allowed, ctx, user, bot, conv_id) -> int:
    from . import agent, usage
    changes = 0
    for _ in range(2):
        with usage.bind(origin="learning", user_id=user.id, team_id=bot.team_id, bot_id=bot.id,
                        conversation_id=conv_id, endpoint_id=ep.id, billable_request=True):
            reply = await asyncio.wait_for(llm.chat(ep, model, messages, specs), config.LLM_TIMEOUT)
        calls = [c for c in reply.get("tool_calls") or [] if c.get("function", {}).get("name") in allowed]
        if not calls:
            break
        messages.append({"role": "assistant", "content": reply.get("content") or "", "tool_calls": calls})
        for c in calls:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
                result = await agent._execute(ctx, c["function"]["name"], args, {})
            except Exception as e:  # noqa: BLE001
                result = f"error: {e}"
            if not result.startswith(("error", "skipped", "conflict")) and "nothing changed" not in result:
                changes += 1
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
    return changes


# ---------------------------------------------------------------- rolling summary

def _cost(m: Message) -> int:
    n = len(m.content or "")
    if m.role == "tool":
        n = min(n, llm.TOOL_RESULT_MAX_CHARS)
    return n + len(m.tool_calls or "")


def _left_out(rows: list[Message], upto: int) -> list[Message]:
    """Messages (oldest first) that no longer fit in the history the model gets, after ``upto``."""
    kept, size = 0, 0
    for m in reversed(rows):
        if kept >= HISTORY_MESSAGES or (kept and size + _cost(m) > llm.HISTORY_TEXT_BUDGET):
            break
        kept += 1
        size += _cost(m)
    older = rows[:len(rows) - kept]
    return [m for m in older if m.id > upto and m.role in ("user", "assistant") and (m.content or "").strip()]


async def summarize(conv_id: int, bot_id: int) -> bool:
    """Fold newly left-out messages into the conversation summary. True when it changed."""
    from . import usage
    with session() as s:
        conv, bot = s.get(Conversation, conv_id), s.get(Bot, bot_id)
        if not (conv and bot):
            return False
        rows = list(s.exec(select(Message).where(Message.conversation_id == conv_id).order_by(Message.id)).all())
        names = {u.id: u.display_name for u in s.exec(select(User)).all()}
        bots = {b.id: b.name for b in s.exec(select(Bot).where(Bot.team_id == bot.team_id)).all()}
        old_summary, upto = conv.summary or "", conv.summary_upto or 0
    fresh = _left_out(rows, upto)
    if sum(len(m.content) for m in fresh) < SUMMARY_MIN_CHARS:
        return False
    if usage.should_block(bot.team_id):
        return False
    ep, model = side_endpoint(bot)
    if ep is None:
        return False
    chunk, size = [], 0  # oldest first; what does not fit is folded in on a later turn
    for m in fresh:
        if chunk and size + min(len(m.content), 2000) > SUMMARY_INPUT_CHARS:
            break
        chunk.append(m)
        size += min(len(m.content), 2000)
    text = _transcript(chunk, names, bots, SUMMARY_INPUT_CHARS + 2000)
    user = (f"Earlier summary:\n{old_summary}\n\n" if old_summary else "") + "Messages to add:\n" + text
    with usage.bind(origin="summary", team_id=bot.team_id, bot_id=bot.id, conversation_id=conv_id,
                    endpoint_id=ep.id, billable_request=True):
        reply = await asyncio.wait_for(llm.chat(ep, model, [
            {"role": "system", "content": SUMMARY_PROMPT.format(limit=SUMMARY_MAX_CHARS)},
            {"role": "user", "content": user}], None), config.LLM_TIMEOUT)
    new = (reply.get("content") or "").strip()
    if not new:
        return False
    with session() as s:
        conv = s.get(Conversation, conv_id)
        if conv is None:
            return False
        conv.summary = new[:SUMMARY_MAX_CHARS * 2]
        conv.summary_upto = chunk[-1].id
        s.add(conv)
        s.commit()
    return True


# ---------------------------------------------------------------- search_history

def _local(dt) -> str:
    from datetime import timezone
    from zoneinfo import ZoneInfo
    if dt is None:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d %H:%M %Z")


def search_history(ctx, query: str, limit: int = 8) -> str:
    """Find messages in chats the person can read: in a team chat only that chat; in the overview all
    their projects; otherwise this project. Tool output is not searched."""
    words = [w for w in (query or "").split() if len(w) >= 2][:6]
    if not words:
        return "error: give a few words to search for"
    limit = max(1, min(int(limit or 8), 20))
    with session() as s:
        q = select(Message, Conversation).where(Message.conversation_id == Conversation.id,
                                                Message.role.in_(("user", "assistant")))
        for w in words:
            q = q.where(Message.content.ilike(f"%{w}%"))
        if ctx.is_channel:
            q = q.where(Conversation.id == ctx.conv_id)
        elif not ctx.is_home:
            q = q.where(Conversation.team_id == ctx.team_id)
        hits = s.exec(q.order_by(Message.id.desc()).limit(300)).all()
        names = {u.id: u.display_name for u in s.exec(select(User)).all()}
        bots = {b.id: (b.name, b.handle) for b in s.exec(select(Bot)).all()}
        teams = {t.id: t.name for t in s.exec(select(Team)).all()}
        out, seen = [], {}
        for m, conv in hits:
            if conv.id not in seen:
                seen[conv.id] = can_read_conversation(s, ctx.requester_id, conv)
            if not seen[conv.id]:
                continue
            if conv.kind == "channel":
                where = f"#{conv.name}"
            elif conv.kind in ("dm", "home"):
                where = f"private chat with @{bots.get(conv.bot_id, ('', 'bot'))[1]}"
            elif conv.kind == "handoff":
                where = f"bot thread of @{bots.get(conv.bot_id, ('', 'bot'))[1]}"
            else:
                where = conv.kind
            if ctx.is_home and conv.team_id:
                where += f" in {teams.get(conv.team_id, 'a project')}"
            who = names.get(m.author_user_id, "someone") if m.role == "user" else bots.get(m.author_bot_id, ("bot",))[0]
            text = " ".join((m.content or "").split())
            i = text.lower().find(words[0].lower())
            start = max(0, i - 120)
            snippet = ("…" if start else "") + text[start:start + 320] + ("…" if len(text) > start + 320 else "")
            out.append(f"- [{where}, {_local(m.created_at)}, message {m.id}] {who}: {snippet}")
            if len(out) >= limit:
                break
    if not out:
        return f"No messages found for {query!r}."
    return f"{len(out)} message(s), newest first:\n" + "\n".join(out)
