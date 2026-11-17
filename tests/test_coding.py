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
