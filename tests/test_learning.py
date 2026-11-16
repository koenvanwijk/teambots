"""Background learning review, rolling summary and search_history (app/learning.py)."""
import asyncio
import json
import os
import sys
import tempfile

os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp(prefix="teambots-learning-"))
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
os.environ.setdefault("TB_SANDBOX", "local")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from app import agent, config, learning, llm, memory, prompt  # noqa: E402
from app.db import (Bot, ChannelMember, Conversation, Endpoint, Message, Skill, Team, TeamMember, User,  # noqa: E402
                    init_db, select, session)


@pytest.fixture()
def world(monkeypatch):
    import app.db as db
    import app.home as home
    from sqlmodel import create_engine
    tmp = tempfile.mkdtemp(prefix="teambots-learning-db-")
    engine = create_engine(f"sqlite:///{tmp}/test.db", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(home, "engine", engine)
    monkeypatch.setattr(config, "DATA_DIR", tmp)
    monkeypatch.setattr(config, "LEARNING", True)
    learning._turns.clear()
    learning._busy.clear()
    init_db()
    with session() as s:
        team, other_team = Team(name="Proj", invite_code="inv-a"), Team(name="Other", invite_code="inv-b")
        s.add(team)
        s.add(other_team)
        koen, ruud = User(username="koen", display_name="Koen", password_hash="x"), \
            User(username="ruud", display_name="Ruud", password_hash="x")
        s.add(koen)
        s.add(ruud)
        s.commit()
        for u in (koen, ruud):
            s.add(TeamMember(team_id=team.id, user_id=u.id, role="member"))
        ep = Endpoint(team_id=team.id, owner_id=koen.id, name="Copilot", base_url="https://api.example.com/v1",
                      default_model="claude-sonnet-5", provider="openai")
        s.add(ep)
        s.commit()
        bot = Bot(team_id=team.id, owner_id=koen.id, name="Helper", handle="helper", endpoint_id=ep.id,
                  is_team=True, published=True)
        s.add(bot)
        s.commit()
        dm = Conversation(team_id=team.id, kind="dm", name="Helper", bot_id=bot.id, user_id=koen.id)
        ruud_dm = Conversation(team_id=team.id, kind="dm", name="Helper", bot_id=bot.id, user_id=ruud.id)
        chan = Conversation(team_id=team.id, kind="channel", name="general")
        other = Conversation(team_id=other_team.id, kind="channel", name="elsewhere")
        for c in (dm, ruud_dm, chan, other):
            s.add(c)
        s.commit()
        s.add(ChannelMember(conversation_id=chan.id, user_id=koen.id))
        s.add(ChannelMember(conversation_id=chan.id, bot_id=bot.id))
        s.add(Message(conversation_id=dm.id, role="user", content="The invoice for ACME is due friday",
                      author_user_id=koen.id))
        s.add(Message(conversation_id=dm.id, role="assistant", content="Noted: ACME invoice friday.",
                      author_bot_id=bot.id))
        s.add(Message(conversation_id=ruud_dm.id, role="user", content="ACME invoice secret from Ruud",
                      author_user_id=ruud.id))
        s.add(Message(conversation_id=chan.id, role="user", content="ACME invoice in the channel",
                      author_user_id=koen.id))
        s.add(Message(conversation_id=other.id, role="user", content="ACME invoice other project",
                      author_user_id=koen.id))
        s.commit()
        out = dict(team=team.id, koen=koen.id, ruud=ruud.id, bot=bot.id, ep=ep.id, dm=dm.id, chan=chan.id)
    return out


class FakeLLM:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def __call__(self, ep, model, messages, tools=None, **kw):
        self.calls.append({"ep": ep.name, "model": model, "messages": messages, "tools": tools})
        r = self.replies.pop(0) if self.replies else {"content": "nothing", "tool_calls": []}
        if isinstance(r, Exception):
            raise r
        return r


def _call(tool, **args):
    return {"id": "call_" + tool, "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}


def test_review_saves_memory_and_skill_on_bot_endpoint(world, monkeypatch):
    fake = FakeLLM([{"content": "", "tool_calls": [
        _call("memory", target="user", operations=[{"action": "add", "content": "Koen handles ACME invoices"}]),
        _call("skill_manage", action="create", name="acme-invoice", description="Send the ACME invoice",
              instructions="1. export 2. mail"),
        _call("run_command", command="rm -rf /")]}])
    monkeypatch.setattr(llm, "chat", fake)
    changes = asyncio.run(learning.review(world["dm"], world["bot"], world["koen"]))
    assert changes == 2
    assert fake.calls[0]["ep"] == "Copilot" and {t["function"]["name"] for t in fake.calls[0]["tools"]} == {
        "memory", "skill_manage"}
    assert "The invoice for ACME is due friday" in fake.calls[0]["messages"][1]["content"]
    with session() as s:
        bot = s.get(Bot, world["bot"])
        assert [m.content for m in memory.entries(s, bot, "user", world["koen"])] == ["Koen handles ACME invoices"]
        assert s.exec(select(Skill).where(Skill.name == "acme-invoice")).first()


def test_review_prefers_spark_and_fails_silently(world, monkeypatch):
    with session() as s:
        s.add(Endpoint(team_id=world["team"], owner_id=world["koen"], name="dgx-spark",
                       base_url="http://192.168.86.29:8888/v1", default_model="deepseek-flash"))
        s.commit()
        bot = s.get(Bot, world["bot"])
    ep, model = learning.side_endpoint(bot)
    assert ep.name == "dgx-spark" and model == "deepseek-flash"

    fake = FakeLLM([llm.LLMError("spark is down")])
    monkeypatch.setattr(llm, "chat", fake)

    async def go():
        learning._spawn("review", world["dm"], world["bot"], learning.review(world["dm"], world["bot"], world["koen"]))
        await agent.idle()
    asyncio.run(go())  # no exception escapes
    assert fake.calls and fake.calls[0]["ep"] == "dgx-spark"


def test_after_turn_triggers_every_eight_turns_or_many_tools(world, monkeypatch):
    started = []
    monkeypatch.setattr(learning, "_spawn", lambda kind, c, b, coro: (coro.close(), started.append(kind)))
    for _ in range(7):
        learning.after_turn(world["dm"], world["bot"], world["koen"])
    assert started.count("review") == 0 and started.count("summary") == 7
    learning.after_turn(world["dm"], world["bot"], world["koen"])
    assert started.count("review") == 1
    with session() as s:  # a turn with six tool calls is reviewed right away
        s.add(Message(conversation_id=world["dm"], role="user", content="do it", author_user_id=world["koen"]))
        s.add(Message(conversation_id=world["dm"], role="assistant", author_bot_id=world["bot"],
                      tool_calls=json.dumps([{"id": str(i)} for i in range(6)])))
        s.commit()
    learning.after_turn(world["dm"], world["bot"], world["koen"])
    assert started.count("review") == 2
    monkeypatch.setattr(config, "LEARNING", False)
    learning.after_turn(world["dm"], world["bot"], world["koen"])
    assert len(started) == 11  # switched off: nothing started


def test_rolling_summary_covers_left_out_messages_and_reaches_the_prompt(world, monkeypatch):
    with session() as s:
        for i in range(60):
            s.add(Message(conversation_id=world["dm"], role="user" if i % 2 == 0 else "assistant",
                          content=f"message {i} " + "x" * 1500,
                          author_user_id=world["koen"] if i % 2 == 0 else None,
                          author_bot_id=None if i % 2 == 0 else world["bot"]))
        s.commit()
    fake = FakeLLM([{"content": "Koen asked about ACME; we agreed on friday.", "tool_calls": []}] * 10)
    monkeypatch.setattr(llm, "chat", fake)
    assert asyncio.run(learning.summarize(world["dm"], world["bot"])) is True
    first = fake.calls[0]
    assert first["tools"] is None and "The invoice for ACME is due friday" in first["messages"][1]["content"]
    rounds = 1
    while asyncio.run(learning.summarize(world["dm"], world["bot"])):  # a long backlog is folded in in chunks
        rounds += 1
        assert "Earlier summary:" in fake.calls[-1]["messages"][1]["content"]
    assert rounds >= 2 and len(fake.calls) == rounds
    with session() as s:
        conv = s.get(Conversation, world["dm"])
        rows = list(s.exec(select(Message).where(Message.conversation_id == conv.id).order_by(Message.id)).all())
        left = learning._left_out(rows, 0)
        assert conv.summary.startswith("Koen asked about ACME") and conv.summary_upto == left[-1].id
        assert left[-1].id < rows[-1].id  # the recent part stays in the history, not in the summary
        bot, user = s.get(Bot, world["bot"]), s.get(User, world["koen"])
    text = asyncio.run(agent.system_prompt(bot, conv, user, agent.Ctx(bot.id, user.id, conv.id, False,
                                                                       team_id=bot.team_id), ["memory"]))
    assert "## Earlier in this conversation" in text and "we agreed on friday" in text


def test_search_history_respects_access(world):
    dm_ctx = agent.Ctx(world["bot"], world["koen"], world["dm"], False, team_id=world["team"])
    out = learning.search_history(dm_ctx, "ACME invoice")
    assert "due friday" in out and "in the channel" in out
    assert "Ruud" not in out and "other project" not in out  # someone else's chat, another project
    chan_ctx = agent.Ctx(world["bot"], world["koen"], world["chan"], True, team_id=world["team"])
    out = learning.search_history(chan_ctx, "ACME")
    assert "in the channel" in out and "due friday" not in out  # a team chat only searches itself
    assert learning.search_history(dm_ctx, "zzzz-nothing").startswith("No messages found")
    assert learning.search_history(dm_ctx, " ").startswith("error")


def test_search_history_is_a_bot_tool(world):
    names = [t["function"]["name"] for t in agent.BUILTIN_TOOLS]
    assert "search_history" in names
    out = asyncio.run(agent._execute(agent.Ctx(world["bot"], world["koen"], world["dm"], False, team_id=world["team"]),
                                     "search_history", {"query": "due friday"}, {}))
    assert "private chat with @helper" in out
    assert "search_history" in prompt._tool_guidance({"search_history"}, None, None)
