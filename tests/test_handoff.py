"""Parallel bot-to-bot handoff threads (own conversation, own lock, full context)."""
import asyncio
import os
import sys
import tempfile
import time

os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp(prefix="teambots-handoff-"))
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
os.environ.setdefault("TB_SANDBOX", "local")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from sqlmodel import create_engine  # noqa: E402

from app.db import (Bot, Conversation, Endpoint, Message, Team, TeamMember, User,  # noqa: E402
                    init_db, select, session)


@pytest.fixture()
def world():
    import app.db as db
    import app.home as home
    saved_db, saved_home = db.engine, home.engine
    db.engine = home.engine = create_engine(
        f"sqlite:///{tempfile.mkdtemp(prefix='teambots-handoff-db-')}/t.db",
        connect_args={"check_same_thread": False})
    init_db()
    with session() as s:
        user = User(username="alice", display_name="Alice", password_hash="x")
        s.add(user)
        s.commit()
        s.refresh(user)
        team = Team(name="P", invite_code="inv-handoff")
        s.add(team)
        s.commit()
        s.refresh(team)
        s.add(TeamMember(team_id=team.id, user_id=user.id, role="admin"))
        ep = Endpoint(team_id=team.id, owner_id=user.id, name="mock",
                      base_url="http://127.0.0.1:9/v1", default_model="mock-1")
        s.add(ep)
        s.commit()
        s.refresh(ep)
        helper = Bot(team_id=team.id, owner_id=user.id, name="Helper", handle="helper",
                     endpoint_id=ep.id, model="helper-model", is_team=True, published=True)
        writer = Bot(team_id=team.id, owner_id=user.id, name="Writer", handle="writer",
                     endpoint_id=ep.id, model="writer-model", is_team=True, published=True)
        s.add(helper)
        s.add(writer)
        s.commit()
        s.refresh(helper)
        s.refresh(writer)
        dm_h = Conversation(team_id=team.id, kind="dm", name="Helper", user_id=user.id, bot_id=helper.id)
        dm_w = Conversation(team_id=team.id, kind="dm", name="Writer", user_id=user.id, bot_id=writer.id)
        s.add(dm_h)
        s.add(dm_w)
        s.commit()
        s.refresh(dm_h)
        s.refresh(dm_w)
        ids = dict(user=user.id, team=team.id, helper=helper.id, writer=writer.id,
                   dm_h=dm_h.id, dm_w=dm_w.id, ep=ep.id)
    yield ids
    db.engine, home.engine = saved_db, saved_home


def _patch_chat(monkeypatch, delays, seen):
    from app import llm

    async def fake_chat(ep, model, messages, tools=None, max_tokens=None, _routed=False):
        sysmsg = ""
        if messages and isinstance(messages[0].get("content"), str):
            sysmsg = messages[0]["content"]
        is_handoff = "Bot-to-bot thread" in sysmsg
        from app import usage as usage_mod
        origin = usage_mod.current().origin if usage_mod.current() else ""
        seen.append({"model": model, "messages": messages, "tools": tools or [],
                     "origin": origin, "handoff": is_handoff})
        await asyncio.sleep(0 if is_handoff else delays.get("default", 0))
        last = ""
        for m in reversed(messages or []):
            c = m.get("content")
            if m.get("role") == "user":
                last = c if isinstance(c, str) else str(c)
                break
        return {"content": f"ECHO[{model}] {last[-80:]}", "tool_calls": []}

    monkeypatch.setattr(llm, "chat", fake_chat)


def test_handoff_runs_in_parallel_on_its_own_thread(world, monkeypatch):
    asyncio.run(_test_handoff_runs_in_parallel_on_its_own_thread(world, monkeypatch))

async def _test_handoff_runs_in_parallel_on_its_own_thread(world, monkeypatch):
    from app import agent
    from app.agent import Ctx, handle_user_message, run_subagent

    delays = {"default": 0.4}
    seen = []
    _patch_chat(monkeypatch, delays, seen)
    monkeypatch.setattr(agent, "HANDOFF_WAIT_S", 5)

    # A slow turn in Writer's private chat
    handle_user_message(world["dm_w"], world["user"], "please write a long essay")
    await asyncio.sleep(0.05)  # let the DM run start and enter its sleep
    assert (world["dm_w"], world["writer"]) in agent._runs

    sub = Ctx(bot_id=world["writer"], requester_id=world["user"], conv_id=world["dm_h"],
              is_channel=False, depth=1, team_id=world["team"])
    t0 = time.monotonic()
    answer = await run_subagent(sub, "draft a title", asked_by="Helper", asker_id=world["helper"])
    elapsed = time.monotonic() - t0

    assert "ECHO[writer-model]" in answer and "draft a title" in answer
    # Must not have waited for the slow DM turn (0.4s).
    assert elapsed < 0.35, elapsed
    # The private chat is still running; the handoff did not queue behind it.
    assert (world["dm_w"], world["writer"]) in agent._runs

    with session() as s:
        threads = s.exec(select(Conversation).where(Conversation.kind == "handoff")).all()
        assert len(threads) == 1
        h = threads[0]
        assert h.bot_id == world["writer"] and h.peer_bot_id == world["helper"]
        assert h.user_id == world["user"]
        dm_msgs = [m.content for m in s.exec(select(Message).where(
            Message.conversation_id == world["dm_w"], Message.role == "user"))]
        assert "draft a title" not in " ".join(dm_msgs)
        h_msgs = list(s.exec(select(Message).where(Message.conversation_id == h.id)))
        assert any("draft a title" in (m.content or "") for m in h_msgs)
        assert any(m.role == "assistant" and "ECHO[writer-model]" in (m.content or "") for m in h_msgs)

    # System prompt of the handoff includes the bot-to-bot note and a brief of recent work.
    handoff_calls = [c for c in seen if c["model"] == "writer-model" and any(
        isinstance((m.get("content") if isinstance(m, dict) else None), str)
        and "Bot-to-bot thread" in m["content"] for m in c["messages"])]
    assert handoff_calls, "expected a handoff system prompt"
    sysmsg = handoff_calls[0]["messages"][0]["content"]
    assert "Bot-to-bot thread" in sysmsg
    assert "Recent work" in sysmsg or "private chat" in sysmsg
    tool_names = [t.get("function", {}).get("name") for t in handoff_calls[0]["tools"]]
    assert "ask_bot" not in tool_names  # depth/cycle guard
    assert handoff_calls[0]["origin"] == "ask_bot"

    await agent.idle()


def test_handoff_is_readable_only_by_the_person(world):
    from app.access import can_read_conversation
    from app.agent import handoff_conversation
    from app.db import User

    with session() as s:
        user = s.get(User, world["user"])
        outsider = User(username="bob", display_name="Bob", password_hash="x")
        s.add(outsider)
        s.commit()
        s.refresh(outsider)
        target, asker = s.get(Bot, world["writer"]), s.get(Bot, world["helper"])
        conv = handoff_conversation(world["team"], target, asker, user)
        assert can_read_conversation(s, user.id, conv) is True
        assert can_read_conversation(s, outsider.id, conv) is False
