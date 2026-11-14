"""Unit tests for the rule-based LLM router. Does not import app.main at collection time."""
import asyncio
import os
import sys
import tempfile
import types

import pytest

os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp(prefix="teambots-router-"))
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
os.environ.setdefault("TB_SANDBOX", "local")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _ep(id, name, provider="openai", model="m", team_id=1, base="http://127.0.0.1:9/v1"):
    return types.SimpleNamespace(id=id, name=name, provider=provider, default_model=model,
                                 team_id=team_id, base_url=base, rules_json="")


def _rules(local=1, cheap=2, normal=2, heavy=2):
    from app.llm_router import validate_rules
    raw = {
        "version": 1,
        "routes": [
            {"name": "local", "endpoint_id": local, "model": "spark"},
            {"name": "cheap", "endpoint_id": cheap, "model": "claude-haiku-4.5"},
            {"name": "normal", "endpoint_id": normal, "model": "claude-sonnet-5"},
            {"name": "heavy", "endpoint_id": heavy, "model": "claude-opus-5.5"},
        ],
        "policy": {
            "privacy": ["local"],
            "simple": ["local", "cheap"],
            "normal": ["normal", "cheap", "local"],
            "heavy": ["heavy", "normal", "cheap"],
            "budget_80": ["local", "cheap"],
            "budget_100_block": ["local"],
        },
        "signals": {"long_context_tokens": 1000, "short_chars": 40, "hard_keywords": ["think hard"]},
    }
    eps = [_ep(1, "dgx-spark"), _ep(2, "ICT Copilot", "github_copilot", "claude-sonnet-5")]
    return validate_rules(raw, endpoints=eps)


def test_validate_rejects_bad_json_and_unknown_route():
    from app.llm_router import validate_rules
    with pytest.raises(ValueError, match="valid JSON"):
        validate_rules("{nope", endpoints=[_ep(1, "spark")])
    with pytest.raises(ValueError, match="unknown route"):
        validate_rules({"routes": [{"name": "local", "endpoint_id": 1, "model": ""}],
                        "policy": {"privacy": ["nope"], "simple": ["local"], "normal": ["local"],
                                   "heavy": ["local"], "budget_80": ["local"], "budget_100_block": ["local"]}},
                       endpoints=[_ep(1, "spark")])
    with pytest.raises(ValueError, match="not in this project"):
        validate_rules({"routes": [{"name": "local", "endpoint_id": 99, "model": ""}]},
                       endpoints=[_ep(1, "spark")])
    with pytest.raises(ValueError, match="another Router"):
        validate_rules({"routes": [{"name": "local", "endpoint_id": 3, "model": ""}]},
                       endpoints=[_ep(3, "Router", "router")])


def test_short_message_is_simple_and_local_first():
    from app.llm_router import decide, begin_turn
    begin_turn()
    rules = _rules()
    choice = decide(rules, messages=[{"role": "user", "content": "Hi"}])
    assert choice["policy"] == "simple"
    assert choice["route"]["name"] == "local"
    assert [r["name"] for r in choice["chain"]] == ["local", "cheap"]


def test_tool_heavy_and_hard_keyword_and_long_context():
    from app.llm_router import decide, begin_turn
    rules = _rules()
    begin_turn()
    heavy = decide(rules, messages=[{"role": "user", "content": "please run this command on the project computer"}])
    assert heavy["policy"] == "heavy" and heavy["route"]["model"] == "claude-opus-5.5"
    begin_turn()
    mid = decide(rules, messages=[
        {"role": "user", "content": "ok"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "run_command"}}]},
        {"role": "tool", "content": "done"},
    ])
    assert mid["policy"] == "heavy"
    begin_turn()
    hard = decide(rules, messages=[{"role": "user", "content": "Please think hard about this plan."}])
    assert hard["policy"] == "heavy"
    begin_turn()
    long = decide(rules, messages=[{"role": "user", "content": "x" * 5000}])
    assert long["policy"] == "heavy" and "tokens" in long["reason"]


def test_local_only_bot_and_secret_stay_on_spark():
    from app.llm_router import decide, begin_turn
    rules = _rules()
    begin_turn()
    bot = types.SimpleNamespace(local_only=True, is_home=False, computer_use=False)
    choice = decide(rules, messages=[{"role": "user", "content": "think hard and use tools"}],
                    tools=[{"function": {"name": "run_command"}}], bot=bot)
    assert choice["policy"] == "privacy" and choice["route"]["name"] == "local"
    begin_turn()
    bot2 = types.SimpleNamespace(local_only=False, is_home=False, computer_use=False)
    secret = decide(rules, messages=[{"role": "user", "content": "Use {{secret:API_KEY}} now"}], bot=bot2)
    assert secret["policy"] == "privacy"


def test_budget_steers_cheaper_and_block_is_local_only(monkeypatch):
    from app import usage
    from app.llm_router import decide, begin_turn
    rules = _rules()

    def status(_team):
        return {"level": "warn", "block": False}

    monkeypatch.setattr(usage, "budget_status", status)
    begin_turn()
    choice = decide(rules, messages=[{"role": "user", "content": "think hard about this"}], team_id=1)
    assert choice["policy"] == "budget_80" and choice["route"]["name"] == "local"
    assert [r["name"] for r in choice["chain"]] == ["local", "cheap"]

    def over(_team):
        return {"level": "over", "block": True}

    monkeypatch.setattr(usage, "budget_status", over)
    begin_turn()
    blocked = decide(rules, messages=[{"role": "user", "content": "think hard about this"}], team_id=1)
    assert blocked["policy"] == "budget_100_block"
    assert [r["name"] for r in blocked["chain"]] == ["local"]


def test_forced_route_and_sticky(monkeypatch):
    from app import usage
    from app.llm_router import decide, begin_turn, _sticky
    monkeypatch.setattr(usage, "budget_status", lambda _t: {"level": "ok", "block": False})
    rules = _rules()
    begin_turn()
    forced = decide(rules, messages=[{"role": "user", "content": "Hi"}], forced="normal")
    assert forced["route"]["name"] == "normal"
    _sticky.set({"route": forced["route"], "reason": "earlier"})
    follow = decide(rules, messages=[{"role": "user", "content": "and now use this result"}])
    assert follow["route"]["name"] == "normal"
    assert follow["reason"] == "same route as earlier in this turn"
    # privacy overrides sticky
    bot = types.SimpleNamespace(local_only=True, is_home=False, computer_use=False)
    priv = decide(rules, messages=[{"role": "user", "content": "hi"}], bot=bot)
    assert priv["route"]["name"] == "local"


def test_router_chat_falls_back_on_error_and_empty(monkeypatch):
    from app import llm_router, llm, usage
    from app.llm import LLMError
    monkeypatch.setattr(usage, "budget_status", lambda _t: {"level": "ok", "block": False})

    rules = _rules()
    calls = []

    async def fake_chat(ep, model, messages, tools=None, max_tokens=None, _routed=False):
        calls.append((ep.id, model, _routed))
        if ep.id == 1:
            raise LLMError("spark is down")
        if model == "claude-haiku-4.5":
            return {"content": "ok-haiku", "tool_calls": []}
        return {"content": "", "tool_calls": []}

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm_router, "load_rules", lambda ep: rules)
    monkeypatch.setattr(llm_router, "_load_bot", lambda _id: None)
    monkeypatch.setattr(llm_router, "_load_endpoint",
                        lambda eid: _ep(eid, "dgx-spark" if eid == 1 else "ICT Copilot",
                                       "openai" if eid == 1 else "github_copilot",
                                       "spark" if eid == 1 else "claude-sonnet-5"))
    llm_router.begin_turn()
    router_ep = _ep(9, "Router", "router", "auto")
    # short message: local then cheap
    out = asyncio.run(llm_router.chat(router_ep, "auto", [{"role": "user", "content": "Hi"}]))
    assert out["content"] == "ok-haiku"
    assert out["route"] == "cheap"
    assert "fallback" in out["route_reason"]
    assert calls[0][0] == 1 and calls[1] == (2, "claude-haiku-4.5", True)
