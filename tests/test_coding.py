"""Personal coding bots: creation, settings validation and shielding.

Does not import app at collection time, so test_flow can set TB_DATA_DIR first.
"""
import json
import os
import sys
import tempfile

os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp(prefix="teambots-coding-"))
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
os.environ.setdefault("TB_SANDBOX", "local")

import pytest  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _register(c, username):
    r = c.post("/api/register", json={"username": username, "password": "password123", "display_name": username})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _client(enabled):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import config
    old = config.CODING_BOTS
    config.CODING_BOTS = enabled
    try:
        with TestClient(app) as c:
            yield c
    finally:
        config.CODING_BOTS = old


@pytest.fixture
def client():
    yield from _client(True)


@pytest.fixture
def client_off():
    yield from _client(False)


def test_coding_routes_hidden_when_disabled(client_off):
    h = _register(client_off, "coding_off")
    assert client_off.get("/api/coding-bots", headers=h).status_code == 404
    assert client_off.post("/api/coding-bots", headers=h, json={"name": "Coder"}).status_code == 404


def test_create_coding_bot(client):
    from app import home
    from app.db import Bot, session
    h = _register(client, "coding_creator")
    r = client.post("/api/coding-bots", headers=h, json={"name": "Coder"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["runner"] == "claude_code" and b["model"] == "opus" and b["workdir"] == ""
    assert b["handle"] == f"code-{b['id']}"
    assert b["conversation"]["kind"] == "dm" and b["conversation"]["team_id"] == 0
    with session() as s:
        bot = s.get(Bot, b["id"])
        assert home.sandbox_key(bot) == f"home-u{bot.owner_id}"
        assert (bot.is_team, bot.published, bot.computer_use, bot.endpoint_id) == (False, False, True, 0)
        assert (bot.team_id, bot.is_home) == (0, False)
    assert [x["id"] for x in client.get("/api/coding-bots", headers=h).json()] == [b["id"]]


def test_patch_coding_bot(client):
    h = _register(client, "coding_patcher")
    bid = client.post("/api/coding-bots", headers=h, json={"name": "Coder"}).json()["id"]
    r = client.patch(f"/api/coding-bots/{bid}", headers=h,
                     json={"name": "Renamed", "model": "sonnet", "workdir": "proj/app", "instructions": "be brief"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["name"], b["model"], b["workdir"], b["instructions"]) == ("Renamed", "sonnet", "proj/app", "be brief")
    assert b["conversation"]["id"]
    r = client.patch(f"/api/coding-bots/{bid}", headers=h, json={"workdir": ""})
    assert r.json()["workdir"] == "" and r.json()["name"] == "Renamed"


def test_coding_bot_rejects_bad_settings(client):
    h = _register(client, "coding_badsettings")
    assert client.post("/api/coding-bots", headers=h, json={"name": "x", "model": "opus; rm -rf /"}).status_code == 400
    assert client.post("/api/coding-bots", headers=h, json={"name": "x", "workdir": "../etc"}).status_code == 400
    assert client.post("/api/coding-bots", headers=h, json={"name": " "}).status_code == 400
    bid = client.post("/api/coding-bots", headers=h, json={"name": "ok"}).json()["id"]
    assert client.patch(f"/api/coding-bots/{bid}", headers=h, json={"model": "a b"}).status_code == 400
    assert client.patch(f"/api/coding-bots/{bid}", headers=h, json={"workdir": "/abs"}).status_code == 400


def test_coding_bot_cannot_be_shared_or_used_by_others(client):
    h = _register(client, "coding_sharer")
    bid = client.post("/api/coding-bots", headers=h, json={"name": "Coder"}).json()["id"]
    assert client.put(f"/api/bots/{bid}/share", headers=h, json={"published": True, "access": "team"}).status_code == 400
    assert client.patch(f"/api/bots/{bid}", headers=h, json={"endpoint_id": 1}).status_code == 400
    other = _register(client, "coding_other")
    assert client.patch(f"/api/coding-bots/{bid}", headers=other, json={"name": "mine"}).status_code == 404
    assert client.get("/api/coding-bots", headers=other).json() == []


def test_generic_bot_patch_keeps_coding_bot_invariants(client):
    h = _register(client, "coding_generic_patch")
    bid = client.post("/api/coding-bots", headers=h, json={"name": "Coder"}).json()["id"]
    assert client.patch(f"/api/bots/{bid}", headers=h, json={"model": "opus; rm -rf /"}).status_code == 400
    assert client.patch(f"/api/bots/{bid}", headers=h, json={"computer_use": False}).status_code == 400
    r = client.patch(f"/api/bots/{bid}", headers=h, json={"model": "sonnet"})
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "sonnet"


def test_parse_init():
    from app import coding
    assert coding.parse_event('{"type":"system","subtype":"init","session_id":"s1"}') == [{"kind": "session", "session_id": "s1"}]


def test_parse_assistant_text_and_tool_use():
    from app import coding
    line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Ik kijk even"},
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}]}})
    assert coding.parse_event(line) == [
        {"kind": "text", "text": "Ik kijk even"},
        {"kind": "tool_call", "id": "toolu_1", "name": "Bash", "arguments": '{"command": "ls"}'}]


def test_parse_tool_result_list_and_truncate():
    from app import coding
    line = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "x" * 7000}]}]}})
    [item] = coding.parse_event(line)
    assert item["id"] == "toolu_1" and item["content"].endswith("[... truncated ...]") and len(item["content"]) < 6100


def test_parse_result():
    from app import coding
    line = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Klaar",
                       "total_cost_usd": 0.42, "usage": {"input_tokens": 10, "output_tokens": 3}})
    assert coding.parse_event(line) == [{"kind": "result", "text": "Klaar", "is_error": False,
                                         "cost_usd": 0.42, "usage": {"input_tokens": 10, "output_tokens": 3}}]


def test_parse_garbage():
    from app import coding
    assert coding.parse_event("not json") == [] and coding.parse_event('{"type":"other"}') == []


@pytest.mark.parametrize("line, expected", [
    ('{"type":"system","subtype":"x","message":"hi"}', []),
    ('{"type":"result","message":[1]}', [{"kind": "result", "text": "", "is_error": False, "cost_usd": None, "usage": {}}]),
    ('{"type":"assistant","message":"s"}', []),
    ('{"type":"user","message":5}', []),
    ('{"type":"assistant","message":{"content":[{"type":"text","text":null},{"type":"text","text":"ok"}]}}',
     [{"kind": "text", "text": ""}, {"kind": "text", "text": "ok"}]),
    ('{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t","content":[{"type":"text","text":null},{"type":"text","text":"a"}]}]}}',
     [{"kind": "tool_result", "id": "t", "content": "a"}]),
    ('{"type":"result","total_cost_usd":"abc","result":null,"usage":"x"}',
     [{"kind": "result", "text": "", "is_error": False, "cost_usd": None, "usage": {}}]),
    ('{"type":"system","subtype":"init","session_id":5}', [{"kind": "session", "session_id": ""}]),
])
def test_parse_malformed_shapes_do_not_raise(line, expected):
    from app import coding
    assert coding.parse_event(line) == expected


# ---------------------------------------------------------------- run a turn (fake claude)

import time  # noqa: E402


def wait_for(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for a condition")


def _uniq(prefix):
    return f"{prefix}_{os.getpid()}_{time.time_ns() % 10**9}"


@pytest.fixture
def fake_claude(tmp_path):
    from app import config
    wrapper = tmp_path / "claude"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT}/tests/fake_claude_code.py" "$@"\n')
    wrapper.chmod(0o755)
    old = (config.CODING_CLAUDE_BIN, config.CODING_POLL_S)
    config.CODING_CLAUDE_BIN, config.CODING_POLL_S = str(wrapper), 0.05
    try:
        yield
    finally:
        config.CODING_CLAUDE_BIN, config.CODING_POLL_S = old


def _login(owner_id):
    from app import config
    d = os.path.join(config.DATA_DIR, "workspaces", f"home-u{owner_id}", ".config", "claude")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, ".credentials.json"), "w") as f:
        f.write("{}")


def _make_bot(client, username, login=True, **body):
    from app.db import Bot, session
    h = _register(client, username)
    b = client.post("/api/coding-bots", headers=h, json={"name": "Coder", **body}).json()
    if login:
        with session() as s:
            _login(s.get(Bot, b["id"]).owner_id)
    return b["id"], b["conversation"]["id"], h


@pytest.fixture
def logged_in_bot(client, fake_claude):
    yield _make_bot(client, _uniq("cr"))


@pytest.fixture
def bot_without_login(client, fake_claude):
    yield _make_bot(client, _uniq("cn"), login=False)


def _say(client, h, conv, text):
    r = client.post(f"/api/conversations/{conv}/messages", headers=h, json={"content": text})
    assert r.status_code == 200, r.text


def _messages(conv):
    from app.db import Message, session, select
    with session() as s:
        return s.exec(select(Message).where(Message.conversation_id == conv).order_by(Message.id)).all()


def _assistant_texts(conv):
    return [m.content for m in _messages(conv) if m.role == "assistant" and m.content]


def _last_run(bid):
    from app.db import CodingRun, session, select
    with session() as s:
        return s.exec(select(CodingRun).where(CodingRun.bot_id == bid).order_by(CodingRun.id.desc())).first()


def _finished_run(bid):
    run = _last_run(bid)
    return run if run and run.status != "running" else None


def _usage_rows(bid):
    from app.db import LlmUsage, session, select
    with session() as s:
        return s.exec(select(LlmUsage).where(LlmUsage.bot_id == bid).order_by(LlmUsage.id)).all()


def test_run_posts_steps_and_reply(client, logged_in_bot):
    bid, conv, h = logged_in_bot
    _say(client, h, conv, "maak een test")
    wait_for(lambda: _finished_run(bid))
    texts = _assistant_texts(conv)
    assert texts[-1] == "echo:maak een test|resume:|system:"
    assert texts.count(texts[-1]) == 1  # the result text is not posted twice
    calls = [json.loads(m.tool_calls)[0]["function"]["name"] for m in _messages(conv) if m.tool_calls]
    assert calls == ["Bash"]
    assert any(m.role == "tool" and m.tool_call_id == "toolu_1" and m.content == "ok" for m in _messages(conv))
    run = _last_run(bid)
    assert run.status == "done" and run.session_id == "sess-1" and run.ended_at
    assert _usage_rows(bid)[-1].notes.endswith("claude_code")  # log_call prefixes its own note


def test_second_message_resumes_session(client, logged_in_bot):
    bid, conv, h = logged_in_bot
    _say(client, h, conv, "een")
    wait_for(lambda: _finished_run(bid))
    first = _last_run(bid).id
    _say(client, h, conv, "twee")
    wait_for(lambda: (r := _last_run(bid)) and r.id != first and r.status != "running")
    assert _assistant_texts(conv)[-1] == "echo:twee|resume:sess-1|system:"


def test_prompt_with_shell_characters_arrives_literally(client, logged_in_bot):
    bid, conv, h = logged_in_bot
    nasty = 'a "b" $(touch /tmp/pwned-coding-test) `id` \n c'
    _say(client, h, conv, nasty)
    wait_for(lambda: _finished_run(bid))
    assert _assistant_texts(conv)[-1].startswith("echo:" + nasty.strip())
    assert not os.path.exists("/tmp/pwned-coding-test")


def test_instructions_reach_the_process(client, fake_claude):
    bid, conv, h = _make_bot(client, _uniq("ci"), instructions="be brief")
    _say(client, h, conv, "hoi")
    wait_for(lambda: _finished_run(bid))
    assert _assistant_texts(conv)[-1] == "echo:hoi|resume:|system:be brief"


def test_partial_line_is_not_parsed():
    from app import coding
    assert coding._complete(b'{"a":1}\n{"b"') == (b'{"a":1}\n', 8)
    assert coding._complete(b'{"b"') == (b"", 0)


def test_not_logged_in_posts_hint(client, bot_without_login):
    bid, conv, h = bot_without_login
    _say(client, h, conv, "hoi")
    wait_for(lambda: any("niet ingelogd" in t for t in _assistant_texts(conv)))
    assert _last_run(bid) is None


def test_process_dying_fails_the_run_with_stderr(client, logged_in_bot):
    bid, conv, h = logged_in_bot
    _say(client, h, conv, "die now")
    run = wait_for(lambda: _finished_run(bid))
    assert run.status == "failed"
    last = _assistant_texts(conv)[-1]
    assert "De run stopte zonder resultaat." in last and "boom" in last


def test_message_during_run_is_held_then_runs_next(client, logged_in_bot):
    from app import agent
    bid, conv, h = logged_in_bot
    _say(client, h, conv, "sleep please")
    wait_for(lambda: (r := _last_run(bid)) and r.pid)
    _say(client, h, conv, "tweede")
    assert agent._held.get((conv, bid))
    first = _last_run(bid)
    os.kill(first.pid, 15)  # the fake takes 30 s to answer a "sleep" prompt: end it, the held message then runs
    wait_for(lambda: (r := _last_run(bid)) and r.id != first.id and r.status != "running", timeout=20)
    assert _assistant_texts(conv)[-1] == "echo:tweede|resume:sess-1|system:"
