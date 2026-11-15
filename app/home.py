"""The per-user overview bot (Home / Overview).

One bot and one private chat per person, not tied to a project, with its own
sandbox (`tb-home-u{user_id}`). This module is the message-in / message-out
surface a later voice adapter should call: `run_home_turn`. The web UI and a
future voice path both go through that function (or `handle_user_message` on
the home conversation, which the UI uses so replies can stream over SSE).
"""
from __future__ import annotations

import contextvars
import json
from typing import Optional

from .access import bot_role, can_read_conversation, team_role
from .db import (AppSetting, Approval, Bot, ChannelMember, Conversation, Endpoint, Routine, Session,
                 Team, TeamMember, User, engine, select, session)


HOME_HANDLE = "home"
HOME_NAME = "Overview"
# team_id 0 = not a project. Real project ids start at 1.
HOME_TEAM_ID = 0
# chat | voice: voice answers are short and have no markdown (for assistants / Tesla).
reply_style: contextvars.ContextVar[str] = contextvars.ContextVar("home_reply_style", default="chat")
VOICE_STYLE = (
    "The user is speaking through a voice assistant. Answer in short spoken sentences, in the user's language. No markdown, no bullet lists, no code fences, no headings. One or two sentences unless they clearly ask for more."
)

DEFAULT_INSTRUCTIONS = (
    "You are the user's overview assistant: one place to see and talk to every "
    "project, bot and team chat they have access to. You have your own private "
    "computer, separate from every project workspace.\n"
    "Start with list_workspace or get_overview when they want a picture of what "
    "is going on. Use read_messages to look at a specific chat. Use ask_bot to "
    "ask a project bot a question and relay its answer (if the same @handle "
    "exists in more than one project, pass project as well). Use post_to_channel "
    "only when they ask you to post in a team chat. Use list_routines to see "
    "their scheduled routines across projects, and update_routine, run_routine "
    "or delete_routine when they ask you to change, run or remove one. Be "
    "concise. Do not reveal chats, routines or approvals that are not theirs."
)


def sandbox_key(bot: Bot) -> str:
    """Docker/local sandbox key for this bot. Container name is `tb-{key}`."""
    if bot.is_home:
        return f"home-u{bot.owner_id}"
    return f"p{bot.team_id}"


def _backup_db() -> None:
    from pathlib import Path
    import shutil
    from .db import _sqlite_path
    path = _sqlite_path()
    if path and path.exists():
        bak = path.with_name(path.name + ".bak-home-bot")
        if not bak.exists():
            shutil.copy2(path, bak)


def migrate_home_bots() -> dict:
    """One-time: give every existing user with a usable endpoint an overview bot."""
    out = {"created": 0, "already": True}
    with Session(engine) as s:
        flag = s.get(AppSetting, "home_bots_v1")
        if flag and flag.value == "done":
            return out
        _backup_db()
        out["already"] = False
        created = 0
        for user in s.exec(select(User)).all():
            if _usable_endpoints(s, user) and not _home_bot(s, user.id):
                _create_home(s, user)
                created += 1
        s.add(AppSetting(key="home_bots_v1", value="done"))
        s.commit()
        out["created"] = created
    return out


def _home_bot(s: Session, user_id: int) -> Optional[Bot]:
    return s.exec(select(Bot).where(Bot.is_home == True, Bot.owner_id == user_id)).first()  # noqa: E712


def _home_conv(s: Session, bot: Bot) -> Optional[Conversation]:
    return s.exec(select(Conversation).where(Conversation.kind == "home", Conversation.bot_id == bot.id,
                                            Conversation.user_id == bot.owner_id)).first()


def _usable_endpoints(s: Session, user: User) -> list[Endpoint]:
    team_ids = [m.team_id for m in s.exec(select(TeamMember).where(TeamMember.user_id == user.id)).all()]
    if not team_ids:
        return []
    rows = s.exec(select(Endpoint).where(Endpoint.team_id.in_(team_ids)).order_by(
        Endpoint.team_id, Endpoint.id)).all()
    return [e for e in rows if e.shared or e.owner_id == user.id]


def default_endpoint(s: Session, user: User) -> Optional[Endpoint]:
    available = _usable_endpoints(s, user)
    if not available:
        return None
    if user.home_endpoint_id:
        for e in available:
            if e.id == user.home_endpoint_id:
                return e
    return available[0]


def ensure_home(s: Session, user: User) -> tuple[Bot, Conversation]:
    """Get or create this user's overview bot and private chat. Caller commits if needed."""
    bot = _home_bot(s, user.id)
    if not bot:
        bot = _create_home(s, user)
        s.commit()
        s.refresh(bot)
    conv = _home_conv(s, bot)
    if not conv:
        conv = Conversation(team_id=HOME_TEAM_ID, kind="home", name=bot.name,
                            share_activity=False, user_id=user.id, bot_id=bot.id)
        s.add(conv)
        s.commit()
        s.refresh(conv)
    # Keep the bot on a usable endpoint if the stored one disappeared.
    available_ids = {e.id for e in _usable_endpoints(s, user)}
    if bot.endpoint_id not in available_ids:
        ep = default_endpoint(s, user)
        if ep:
            bot.endpoint_id = ep.id
            s.add(bot)
            s.commit()
    return bot, conv


def _create_home(s: Session, user: User) -> Bot:
    ep = default_endpoint(s, user)
    if not ep:
        raise ValueError("add a model endpoint in one of your projects first, then open Overview")
    if not user.home_endpoint_id:
        user.home_endpoint_id = ep.id
        s.add(user)
    bot = Bot(team_id=HOME_TEAM_ID, owner_id=user.id, name=HOME_NAME, handle=HOME_HANDLE,
              instructions=DEFAULT_INSTRUCTIONS, endpoint_id=ep.id, model=user.home_model or "",
              is_team=False, published=False, is_home=True, approval_mode="ask", computer_use=True)
    s.add(bot)
    s.flush()
    conv = Conversation(team_id=HOME_TEAM_ID, kind="home", name=bot.name,
                        share_activity=False, user_id=user.id, bot_id=bot.id)
    s.add(conv)
    return bot


def apply_settings(s: Session, user: User, endpoint_id: Optional[int] = None, model: Optional[str] = None,
                   computer_use: Optional[bool] = None, android_enabled: Optional[bool] = None,
                   android_software: Optional[bool] = None, android_memory_mb: Optional[int] = None,
                   android_cpus: Optional[int] = None) -> Bot:
    from . import sandbox
    bot, _ = ensure_home(s, user)
    available = {e.id: e for e in _usable_endpoints(s, user)}
    if endpoint_id is not None:
        if endpoint_id not in available:
            raise ValueError("endpoint not available")
        user.home_endpoint_id = endpoint_id
        bot.endpoint_id = endpoint_id
    if model is not None:
        user.home_model = model
        bot.model = model
    if computer_use is not None:
        bot.computer_use = computer_use
    android_changed = any(v is not None for v in (
        android_enabled, android_software, android_memory_mb, android_cpus))
    if android_changed:
        enabled = user.home_android_enabled if android_enabled is None else bool(android_enabled)
        software = user.home_android_software if android_software is None else bool(android_software)
        mem = user.home_android_memory_mb if android_memory_mb is None else android_memory_mb
        cpus = user.home_android_cpus if android_cpus is None else android_cpus
        mem, cpus = sandbox.validate_android_limits(mem, cpus)
        sandbox.validate_android_enable(enabled=enabled, software=software)
        user.home_android_enabled = enabled
        user.home_android_software = software
        user.home_android_memory_mb = mem
        user.home_android_cpus = cpus
    s.add(user)
    s.add(bot)
    s.commit()
    s.refresh(bot)
    return bot


def _android_status() -> dict:
    from . import sandbox
    return sandbox.host_android_status()


def home_out(s: Session, user: User, bot: Bot, conv: Conversation) -> dict:
    available = _usable_endpoints(s, user)
    teams = {t.id: t for t in s.exec(select(Team).where(Team.id.in_([e.team_id for e in available] or [0]))).all()}
    return {
        "bot": {**bot.model_dump(), "role": "owner"},
        "conversation": conv.model_dump(),
        "endpoint_id": bot.endpoint_id,
        "model": bot.model,
        "computer_use": bot.computer_use,
        "android_enabled": bool(getattr(user, "home_android_enabled", False)),
        "android_software": bool(getattr(user, "home_android_software", False)),
        "android_memory_mb": int(getattr(user, "home_android_memory_mb", None) or 6144),
        "android_cpus": int(getattr(user, "home_android_cpus", None) or 4),
        "android_status": _android_status(),
        "endpoints": [
            {"id": e.id, "name": e.name, "default_model": e.default_model, "base_url": e.base_url,
             "project": teams[e.team_id].name if e.team_id in teams else "",
             "project_id": e.team_id}
            for e in available
        ],
    }


def accessible_bots(s: Session, user: User) -> list[tuple[Bot, Team]]:
    """Project bots this person may use, with their project. The overview bot itself is left out."""
    out = []
    for m in s.exec(select(TeamMember).where(TeamMember.user_id == user.id)).all():
        team = s.get(Team, m.team_id)
        if not team:
            continue
        for b in s.exec(select(Bot).where(Bot.team_id == team.id)).all():
            if not b.is_home and bot_role(s, user.id, b):
                out.append((b, team))
    return out


def resolve_bot(s: Session, user: User, handle: str, project: str | None = None) -> Bot | str:
    """Find a project bot by @handle, optionally disambiguated by project name or id."""
    handle = (handle or "").lstrip("@").lower()
    matches = [(b, t) for b, t in accessible_bots(s, user) if b.handle.lower() == handle]
    if project:
        key = str(project).strip().lower()
        matches = [(b, t) for b, t in matches if t.name.lower() == key or str(t.id) == key]
    if not matches:
        return "error: no such bot, or you have no access to it"
    if len(matches) > 1:
        names = ", ".join(f"@{b.handle} in {t.name}" for b, t in matches)
        return f"error: that handle exists in more than one project; pass project as well ({names})"
    return matches[0][0]


def list_workspace(s: Session, user: User) -> str:
    teams = s.exec(select(Team).where(Team.id.in_(
        [m.team_id for m in s.exec(select(TeamMember).where(TeamMember.user_id == user.id)).all()] or [0]
    )).order_by(Team.id)).all()
    if not teams:
        return "You are not in any project yet."
    lines = []
    for t in teams:
        role = team_role(s, user.id, t.id) or "member"
        title = f"Project {t.name}" + (f" (client: {t.client})" if t.client else "") + f" [id {t.id}, {role}]"
        lines.append(title)
        bots = [(b, bot_role(s, user.id, b)) for b in s.exec(select(Bot).where(Bot.team_id == t.id)).all()]
        bots = [(b, r) for b, r in bots if r]
        if bots:
            for b, r in bots:
                flag = []
                if b.is_team and not b.published:
                    flag.append("draft")
                elif b.is_team:
                    flag.append("team")
                lines.append(f"  bot @{b.handle} ({b.name}" + (f", {', '.join(flag)}" if flag else "") + f", role {r})")
        else:
            lines.append("  (no bots you can use)")
        channels = s.exec(select(Conversation).where(Conversation.team_id == t.id,
                                                     Conversation.kind == "channel")).all()
        visible = [ch for ch in channels if can_read_conversation(s, user.id, ch)]
        if visible:
            for ch in visible:
                lines.append(f"  team chat #{ch.name} [id {ch.id}]")
        else:
            lines.append("  (no team chats you are in)")
    return "\n".join(lines)


def get_overview(s: Session, user: User, limit: int = 12) -> str:
    parts = [list_workspace(s, user), ""]
    pending = s.exec(select(Approval).where(Approval.requester_id == user.id,
                                            Approval.status == "pending").order_by(Approval.id.desc())).all()
    if pending:
        parts.append("Pending approvals (yours):")
        for a in pending[:20]:
            bot, conv = s.get(Bot, a.bot_id), s.get(Conversation, a.conversation_id)
            if not conv or not can_read_conversation(s, user.id, conv):
                continue
            where = f"#{conv.name}" if conv.kind == "channel" else conv.name
            parts.append(f"  - {bot.name if bot else a.bot_id} in {where}: {a.tool_name}")
    else:
        parts.append("Pending approvals: none.")
    from . import agent
    running = []
    for (cid, bid), run in list(agent._runs.items()):
        if run.requester_id != user.id:
            continue
        conv, bot = s.get(Conversation, cid), s.get(Bot, bid)
        if not conv or not can_read_conversation(s, user.id, conv):
            continue
        where = f"#{conv.name}" if conv.kind == "channel" else conv.name
        running.append(f"  - {bot.name if bot else bid} is working in {where}")
    parts.append("Currently working:" + ("\n" + "\n".join(running) if running else " none."))
    # Latest message in conversations this person can read (skip feeds and the home chat itself).
    recent = []
    convs = s.exec(select(Conversation).order_by(Conversation.id.desc())).all()
    for conv in convs:
        if conv.kind in ("feed", "home"):
            continue
        if not can_read_conversation(s, user.id, conv):
            continue
        from .db import Message
        msg = s.exec(select(Message).where(Message.conversation_id == conv.id,
                                           Message.role.in_(["user", "assistant"]))
                     .order_by(Message.id.desc())).first()
        if not msg:
            continue
        who = "you" if msg.author_user_id == user.id else (
            (s.get(Bot, msg.author_bot_id).name if msg.author_bot_id and s.get(Bot, msg.author_bot_id) else "bot")
            if msg.role == "assistant" else (s.get(User, msg.author_user_id).display_name if msg.author_user_id else "someone"))
        label = f"#{conv.name}" if conv.kind == "channel" else conv.name
        text = (msg.content or "").replace("\n", " ").strip()[:160]
        recent.append(f"  - {label}: {who}: {text}")
        if len(recent) >= limit:
            break
    parts.append("Recent activity:" + ("\n" + "\n".join(recent) if recent else " none yet."))
    return "\n".join(parts)


def read_messages(s: Session, user: User, conversation_id: int | None = None,
                  project: str | None = None, channel: str | None = None,
                  handle: str | None = None, limit: int = 20) -> str:
    conv = None
    if conversation_id:
        conv = s.get(Conversation, int(conversation_id))
    elif channel:
        teams = _teams_named(s, user, project)
        found = []
        for t in teams:
            for ch in s.exec(select(Conversation).where(Conversation.team_id == t.id,
                                                        Conversation.kind == "channel")).all():
                if ch.name.lower() == channel.lstrip("#").lower() and can_read_conversation(s, user.id, ch):
                    found.append(ch)
        if len(found) != 1:
            return "error: that team chat was not found, or the name is ambiguous; pass project as well"
        conv = found[0]
    elif handle:
        target = resolve_bot(s, user, handle, project)
        if isinstance(target, str):
            return target
        conv = s.exec(select(Conversation).where(Conversation.kind == "dm", Conversation.bot_id == target.id,
                                                 Conversation.user_id == user.id)).first()
        if not conv:
            return "error: you have no private chat with that bot yet"
    else:
        return "error: pass conversation_id, or channel (and project if needed), or handle"
    if not conv or not can_read_conversation(s, user.id, conv):
        return "error: conversation not found"
    from .db import Message
    rows = list(reversed(s.exec(select(Message).where(Message.conversation_id == conv.id,
                                                      Message.role.in_(["user", "assistant"]))
                                .order_by(Message.id.desc()).limit(max(1, min(int(limit or 20), 50))).all())))
    if not rows:
        return f"{_conv_label(conv)} is empty."
    lines = [f"Messages in {_conv_label(conv)}:"]
    for m in rows:
        if m.role == "assistant":
            bot = s.get(Bot, m.author_bot_id) if m.author_bot_id else None
            who = bot.name if bot else "bot"
        else:
            u = s.get(User, m.author_user_id) if m.author_user_id else None
            who = "you" if m.author_user_id == user.id else (u.display_name if u else "someone")
        lines.append(f"  {who}: {(m.content or '').strip()[:1000]}")
    return "\n".join(lines)


def _conv_label(conv: Conversation) -> str:
    return f"#{conv.name}" if conv.kind == "channel" else conv.name


def _teams_named(s: Session, user: User, project: str | None) -> list[Team]:
    ids = [m.team_id for m in s.exec(select(TeamMember).where(TeamMember.user_id == user.id)).all()]
    teams = s.exec(select(Team).where(Team.id.in_(ids or [0]))).all()
    if not project:
        return teams
    key = str(project).strip().lower()
    return [t for t in teams if t.name.lower() == key or str(t.id) == key]


def post_to_channel(user_id: int, content: str, project: str | None, channel: str) -> str:
    if not (content or "").strip():
        return "error: the message is empty"
    with session() as s:
        user = s.get(User, user_id)
        teams = _teams_named(s, user, project)
        found = []
        for t in teams:
            for ch in s.exec(select(Conversation).where(Conversation.team_id == t.id,
                                                        Conversation.kind == "channel")).all():
                if ch.name.lower() == channel.lstrip("#").lower() and can_read_conversation(s, user_id, ch):
                    found.append((t, ch))
        if not found:
            return "error: that team chat was not found, or you are not a member"
        if len(found) > 1:
            names = ", ".join(f"#{ch.name} in {t.name}" for t, ch in found)
            return f"error: that team chat name exists in more than one project; pass project as well ({names})"
        team, ch = found[0]
        conv_id, name, team_name = ch.id, ch.name, team.name
    from . import agent
    agent.handle_user_message(conv_id, user_id, content.strip())
    return f"posted to #{name} in {team_name}. Team chat bots may reply there."



def _routine_for(s, user: User, args: dict) -> Routine | str:
    rid = args.get("routine_id") if args.get("routine_id") is not None else args.get("id")
    if rid is None:
        return "error: routine_id is required"
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return "error: routine_id must be a number"
    r = s.get(Routine, rid)
    if not r or r.owner_id != user.id:
        return "error: routine not found"
    return r


def list_routines_for(user_id: int) -> str:
    from . import routines as routines_mod
    with session() as s:
        user = s.get(User, user_id)
        rows = s.exec(select(Routine).where(Routine.owner_id == user.id)).all()
        if not rows:
            return "You have no routines."
        views = [routines_mod.view(s, r) for r in rows]
    views.sort(key=lambda v: (not v["enabled"], v["next_run"] or "z", v["name"].lower(), v["id"]))
    lines = []
    for v in views:
        state = "enabled" if v["enabled"] else "paused"
        nxt = v["next_run_text"] or "not scheduled"
        last = v["last_run_text"] or "never"
        lines.append(
            f"#{v['id']} {v['name']} ({state}) — @{v['bot_handle']} in {v['project']}, "
            f"{v['conversation_name']}. {v['cron_text']} ({v['cron']}). "
            f"Next: {nxt}. Last: {last}."
        )
    return "\n".join(lines)


def update_routine_for(user_id: int, args: dict) -> str:
    from . import routines as routines_mod
    with session() as s:
        user = s.get(User, user_id)
        r = _routine_for(s, user, args)
        if isinstance(r, str):
            return r
        changes = []
        if args.get("name"):
            r.name = str(args["name"]).strip()
            changes.append(f"name={r.name}")
        if args.get("cron"):
            try:
                routines_mod.cron_trigger(args["cron"])
            except ValueError as e:
                return f"error: invalid cron expression: {e}"
            r.cron = args["cron"]
            changes.append(f"cron={r.cron} ({routines_mod.describe_cron(r.cron)})")
        if args.get("prompt"):
            r.prompt = str(args["prompt"])
            changes.append("prompt updated")
        if "enabled" in args and args["enabled"] is not None:
            r.enabled = bool(args["enabled"])
            changes.append("resumed" if r.enabled else "paused")
        if not changes:
            return "error: pass name, cron, prompt and/or enabled"
        s.add(r)
        s.commit()
        summary = routines_mod.view(s, r)
    routines_mod.sync(r)
    return (f"Updated routine #{summary['id']} {summary['name']}: " + ", ".join(changes) +
            f". Next run: {summary['next_run_text'] or 'not scheduled'}.")


def delete_routine_for(user_id: int, args: dict) -> str:
    from . import routines as routines_mod
    with session() as s:
        user = s.get(User, user_id)
        r = _routine_for(s, user, args)
        if isinstance(r, str):
            return r
        name, rid = r.name, r.id
        s.delete(r)
        s.commit()
    routines_mod.remove(rid)
    return f"Deleted routine #{rid} {name}."


def run_routine_for(user_id: int, args: dict) -> str:
    from . import agent, routines as routines_mod
    with session() as s:
        user = s.get(User, user_id)
        r = _routine_for(s, user, args)
        if isinstance(r, str):
            return r
        rid, name, conv = r.id, r.name, r.conversation_id
    agent.spawn(routines_mod.fire(rid, force=True))
    return f"Started routine #{rid} {name}. It is running in conversation {conv}."


def execute_home_tool(user_id: int, name: str, args: dict) -> str:
    with session() as s:
        user = s.get(User, user_id)
        if name == "list_workspace":
            return list_workspace(s, user)
        if name == "get_overview":
            return get_overview(s, user)
        if name == "read_messages":
            return read_messages(s, user, conversation_id=args.get("conversation_id"),
                                 project=args.get("project"), channel=args.get("channel"),
                                 handle=args.get("handle"), limit=int(args.get("limit") or 20))
    if name == "post_to_channel":
        return post_to_channel(user_id, args.get("content") or "", args.get("project"),
                               args.get("channel") or "")
    if name == "list_routines":
        return list_routines_for(user_id)
    if name == "update_routine":
        return update_routine_for(user_id, args)
    if name == "delete_routine":
        return delete_routine_for(user_id, args)
    if name == "run_routine":
        return run_routine_for(user_id, args)
    return f"error: unknown tool {name}"


async def run_home_turn(user_id: int, content: str, *, source: str = "chat",
                        mode: str | None = None) -> dict:
    """Message in, final assistant text out. No UI.

    The web chat uses `handle_user_message` on the home conversation so the turn
    can stream over SSE. A voice adapter should call this function with
    source='voice' (or mode='voice') and then speak the returned `reply`.
    Voice mode only changes the reply style: short, spoken, no markdown.
    Approvals are never auto-granted; a pending card is reported in the reply.
    Messages are stored in the same home conversation the UI shows.
    """
    from . import agent
    from .db import Message
    style = "voice" if (mode or source) == "voice" else (mode or "chat")
    token = reply_style.set(style)
    try:
        with session() as s:
            user = s.get(User, user_id)
            if not user:
                raise ValueError("user not found")
            if not _usable_endpoints(s, user):
                raise ValueError("add a model endpoint in one of your projects first, then open Overview")
            bot, conv = ensure_home(s, user)
            conv_id, bot_id = conv.id, bot.id
        msg = agent.handle_user_message(conv_id, user_id, content)
        from . import usage
        origin = "voice" if style == "voice" else "overview"
        with usage.bind(origin=origin, user_id=user_id, team_id=None, bot_id=bot_id,
                        conversation_id=conv_id, endpoint_id=bot.endpoint_id):
            await agent.run_agent(conv_id, bot_id, user_id)
        with session() as s:
            last = s.exec(select(Message).where(Message.conversation_id == conv_id,
                                                Message.role == "assistant",
                                                Message.author_bot_id == bot_id)
                          .order_by(Message.id.desc())).first()
            reply = (last.content if last else "") or ""
            pending = s.exec(select(Approval).where(Approval.conversation_id == conv_id,
                                                    Approval.requester_id == user_id,
                                                    Approval.status == "pending")).all()
        approval_pending = bool(pending)
        if approval_pending and style == "voice":
            tools = ", ".join(sorted({a.tool_name for a in pending}))
            extra = (f" I need your approval in TeamBots for {tools}. "
                     "Open the Overview chat to allow or deny it. I did not go ahead.")
            if extra.strip() not in reply:
                reply = (reply.rstrip() + extra) if reply else extra.strip()
        elif approval_pending and not reply:
            tools = ", ".join(sorted({a.tool_name for a in pending}))
            reply = (f"This action needs your approval ({tools}). "
                     "Open the Overview chat to allow or deny it.")
        return {"conversation_id": conv_id, "message_id": msg.id, "reply": reply,
                "source": source, "mode": style, "approval_pending": approval_pending}
    finally:
        reply_style.reset(token)
