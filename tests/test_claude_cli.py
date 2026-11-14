"""Claude CLI provider tests against a fake `claude` binary (no real credentials).

The fake binary echoes back the model and system prompt it was given and the
prompt it read on stdin, so we can assert the argv and stdin wiring, plus the
error, non-JSON, usage and tool-call paths. Does not import app.main at collection time.
"""
import asyncio
import json
import os
import stat
import sys
import tempfile
import types

os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp(prefix="teambots-clicli-"))
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
os.environ.setdefault("TB_SANDBOX", "local")

import pytest  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# A fake `claude`: parses --model/--system-prompt, reads stdin, prints a result
# JSON in the shape the real CLI uses. Model "boom" -> is_error; "garbage" -> non-JSON.
FAKE = r'''#!{py}
import json, sys
argv = sys.argv[1:]
def opt(name):
    return argv[argv.index(name) + 1] if name in argv else ""
model = opt("--model")
system = opt("--system-prompt")
prompt = sys.stdin.read()
usage = {{"input_tokens": 11, "output_tokens": 5, "cache_read_input_tokens": 2,
          "cache_creation_input_tokens": 0}}
if model == "garbage":
    sys.stdout.write("not json at all")
    sys.exit(0)
if model == "crash":
    sys.stderr.write("kaboom on stderr")
    sys.exit(3)
if model == "boom":
    print(json.dumps({{"is_error": True, "subtype": "success",
                       "result": "You've hit your spend limit",
                       "usage": usage}}))
    sys.exit(0)
if model == "limited":
    # Real spend/rate limit: JSON on stdout with is_error AND a non-zero exit code.
    print(json.dumps({{"is_error": True, "subtype": "success",
                       "api_error_status": 429,
                       "result": "You've hit your individual spend limit. Fable limit resets Nov 17.",
                       "usage": usage}}))
    sys.exit(1)
if model == "tools":
    print(json.dumps({{"is_error": False, "subtype": "success",
                       "total_cost_usd": 0.0123,
                       "usage": usage,
                       "result": '{{"tool_calls": [{{"name": "ping", "arguments": {{"ok": true}}}}]}}'}}))
    sys.exit(0)
if model == "nocost":
    print(json.dumps({{"is_error": False, "subtype": "success",
                       "usage": usage,
                       "result": "no price here"}}))
    sys.exit(0)
print(json.dumps({{"is_error": False, "subtype": "success",
                   "total_cost_usd": 0.004,
                   "usage": usage,
                   "result": "  echo model={{}} system={{}} prompt={{}}  ".format(model, system, prompt)}}))
'''


def _ep(default_model="haiku", name="ICT Claude CLI"):
    return types.SimpleNamespace(name=name, default_model=default_model, provider="claude_cli",
                                 id=None, team_id=None, base_url="")


@pytest.fixture
def mod():
    from app import claude_cli, config
    from app.db import init_db
    init_db()
    d = tempfile.mkdtemp(prefix="teambots-fakecli-")
    path = os.path.join(d, "claude")
    with open(path, "w") as f:
        f.write(FAKE.format(py=sys.executable))
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    saved = config.CLAUDE_CLI_BIN
    config.CLAUDE_CLI_BIN = path
    yield claude_cli
    config.CLAUDE_CLI_BIN = saved


def test_build_prompt_splits_system_and_adds_assistant_cue():
    from app import claude_cli
    system, prompt = claude_cli.build_prompt([
        {"role": "system", "content": "You are Bob."},
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
        {"role": "user", "content": "Bye"},
    ])
    assert system == "You are Bob."
    assert prompt == "User: Hi\n\nAssistant: Hello\n\nUser: Bye\n\nAssistant:"


def test_build_prompt_handles_content_parts():
    from app import claude_cli
    _, prompt = claude_cli.build_prompt([{"role": "user", "content": [{"text": "a"}, {"text": "b"}]}])
    assert prompt == "User: ab\n\nAssistant:"


def test_build_prompt_appends_tool_schema():
    from app import claude_cli
    system, _ = claude_cli.build_prompt(
        [{"role": "user", "content": "hi"}],
        [{"type": "function", "function": {"name": "ping", "description": "Call this.", "parameters": {"type": "object"}}}],
    )
    assert "ping" in system and "tool_calls" in system


def test_parse_cli_usage_and_cost():
    from app import claude_cli
    u = claude_cli.parse_cli_usage({
        "total_cost_usd": 0.02,
        "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 3,
                  "cache_creation_input_tokens": 1},
    })
    assert u["prompt_tokens"] == 10 and u["completion_tokens"] == 4
    assert u["cached_tokens"] == 3 and u["cache_write_tokens"] == 1
    assert u["cost_usd"] == 0.02
    camel = claude_cli.parse_cli_usage({
        "modelUsage": {"claude-haiku": {"inputTokens": 8, "outputTokens": 2, "costUSD": 0.001}}
    })
    assert camel["prompt_tokens"] == 8 and camel["cost_usd"] == 0.001


def test_parse_tool_reply():
    from app import claude_cli
    tools = [{"type": "function", "function": {"name": "ping"}}]
    calls = claude_cli.parse_tool_reply('{"tool_calls": [{"name": "ping", "arguments": {"ok": true}}]}', tools)
    assert len(calls) == 1 and calls[0]["function"]["name"] == "ping"
    assert json.loads(calls[0]["function"]["arguments"])["ok"] is True
    fenced = claude_cli.parse_tool_reply('```json\n{"tool_calls": [{"name": "ping", "arguments": {}}]}\n```', tools)
    assert fenced[0]["function"]["name"] == "ping"
    assert claude_cli.parse_tool_reply("just a hello", tools) == []
    assert claude_cli.parse_tool_reply('{"tool_calls": [{"name": "nope"}]}', tools) == []


def test_chat_success_passes_model_system_and_stdin(mod):
    out = asyncio.run(mod.chat(_ep(), "sonnet",
                               [{"role": "system", "content": "sys here"},
                                {"role": "user", "content": "ping"}]))
    assert out["tool_calls"] == []
    assert out["content"].startswith("echo model=sonnet system=sys here prompt=")
    assert "User: ping\n\nAssistant:" in out["content"]


def test_chat_falls_back_to_default_model(mod):
    out = asyncio.run(mod.chat(_ep(default_model="opus"), "",
                               [{"role": "user", "content": "x"}]))
    assert "model=opus" in out["content"]


def test_chat_empty_prompt_skips_cli(mod):
    out = asyncio.run(mod.chat(_ep(), "haiku", [{"role": "system", "content": "only system"}]))
    assert out == {"content": "", "tool_calls": []}


def test_chat_surfaces_is_error_result(mod):
    from app.llm import LLMError
    with pytest.raises(LLMError) as ei:
        asyncio.run(mod.chat(_ep(), "boom", [{"role": "user", "content": "hi"}]))
    assert "spend limit" in str(ei.value)


def test_chat_nonzero_exit_raises_with_stderr(mod):
    from app.llm import LLMError
    with pytest.raises(LLMError) as ei:
        asyncio.run(mod.chat(_ep(), "crash", [{"role": "user", "content": "hi"}]))
    assert "kaboom" in str(ei.value)


def test_chat_nonzero_exit_with_json_surfaces_result(mod):
    # A spend/rate limit exits non-zero but still prints the reason as JSON: show it,
    # not a bare "exit code 1".
    from app.llm import LLMError
    with pytest.raises(LLMError) as ei:
        asyncio.run(mod.chat(_ep(), "limited", [{"role": "user", "content": "hi"}]))
    msg = str(ei.value)
    assert "spend limit" in msg and "exit code" not in msg


def test_chat_non_json_raises(mod):
    from app.llm import LLMError
    with pytest.raises(LLMError) as ei:
        asyncio.run(mod.chat(_ep(), "garbage", [{"role": "user", "content": "hi"}]))
    assert "non-JSON" in str(ei.value)


def test_chat_missing_binary_raises():
    from app import claude_cli, config
    from app.db import init_db
    init_db()
    saved = config.CLAUDE_CLI_BIN
    config.CLAUDE_CLI_BIN = "/nonexistent/claude-binary-xyz"
    try:
        from app.llm import LLMError
        with pytest.raises(LLMError) as ei:
            asyncio.run(claude_cli.chat(_ep(), "haiku", [{"role": "user", "content": "hi"}]))
        assert "not found" in str(ei.value)
    finally:
        config.CLAUDE_CLI_BIN = saved


def test_list_models_returns_catalogue():
    from app import claude_cli
    ids = [m["id"] for m in asyncio.run(claude_cli.list_models(_ep()))]
    assert "fable" in ids and "claude-fable-5-2" in ids and "opus" in ids


def test_chat_parses_tool_call_json(mod):
    tools = [{"type": "function", "function": {"name": "ping", "description": "Call this.",
                                              "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}}}}]
    out = asyncio.run(mod.chat(_ep(), "tools", [{"role": "user", "content": "Call ping"}], tools))
    assert out["content"] == ""
    assert out["tool_calls"][0]["function"]["name"] == "ping"
    assert json.loads(out["tool_calls"][0]["function"]["arguments"])["ok"] is True


def test_compute_cost_uses_cli_amount_or_subscription_unknown():
    from app import usage
    from app.db import init_db
    init_db()
    priced = usage.compute_cost(provider="claude_cli", model="sonnet", endpoint=None,
                                reported_cost_usd=0.0123)
    assert priced["billing_mode"] == "subscription" and priced["cost_usd"] == 0.0123 and not priced["cost_unknown"]
    unknown = usage.compute_cost(provider="claude_cli", model="sonnet", endpoint=None)
    assert unknown["billing_mode"] == "subscription" and unknown["cost_unknown"] and unknown["cost_usd"] is None
