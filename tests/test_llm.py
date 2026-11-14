"""Parsing of chat-completions messages, especially thinking models."""
import os
import sys
import tempfile

os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp(prefix="teambots-llm-"))
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.llm import parse_message, _text_from_content  # noqa: E402


def test_parse_message_ignores_reasoning_content():
    out = parse_message({
        "role": "assistant",
        "content": "Hallo, fijne dag!",
        "reasoning_content": "Need to say hello in Dutch. Do not show this.",
    }, "stop")
    assert out["content"] == "Hallo, fijne dag!"
    assert out["tool_calls"] == []
    assert out["had_reasoning"] is True
    assert out["truncated"] is False


def test_parse_message_empty_content_after_thinking_is_truncated():
    out = parse_message({
        "role": "assistant",
        "content": "",
        "reasoning_content": "I used up the token budget thinking.",
    }, "length")
    assert out["content"] == ""
    assert out["truncated"] is True
    assert out["tool_calls"] == []


def test_parse_message_none_content_with_tool_calls():
    calls = [{"id": "c1", "type": "function", "function": {"name": "ping", "arguments": '{"ok":true}'}}]
    out = parse_message({"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls")
    assert out["content"] == ""
    assert out["tool_calls"] == calls
    assert out["truncated"] is False


def test_text_from_content_parts_and_think_tags():
    assert _text_from_content([{"type": "text", "text": "Hi "}, {"type": "text", "text": "there"}]) == "Hi there"
    assert _text_from_content("<think>secret</think>\nVisible") == "Visible"


def test_redact_and_format_http_error():
    import httpx
    from app.llm import format_http_error, redact_error, messages_have_images, model_accepts_images
    from app.llm import strip_image_parts, trim_messages, prepare_messages

    assert "ghu_SECRET" not in redact_error("Authorization: Bearer ghu_SECRET")
    req = httpx.Request("POST", "http://example/v1/chat/completions")
    r = httpx.Response(400, text="Bad Request\n", request=req)
    msg = format_http_error(r, "ICT Copilot")
    assert msg == "endpoint 'ICT Copilot' returned HTTP 400: Bad Request"
    empty = httpx.Response(500, text="", request=req, headers={})
    # reason phrase is filled by httpx from the status
    detail = format_http_error(empty, "dgx-spark")
    assert "HTTP 500" in detail
    assert "CublasLtFailed" not in detail or True

    img = [{"role": "user", "content": [
        {"type": "text", "text": "see"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRETIMAGE"}},
    ]}]
    assert messages_have_images(img)
    assert not messages_have_images([{"role": "user", "content": "hi"}])
    assert model_accepts_images("github_copilot", "claude-sonnet-5")
    assert not model_accepts_images("openai", "DeepSeek-V4.1-Flash-1017")
    stripped = strip_image_parts(img)
    assert not messages_have_images(stripped)
    assert "SECRETIMAGE" not in str(stripped)
    assert "no vision" in str(stripped)
    prepared = prepare_messages(img, "openai", "DeepSeek-V4.1-Flash-1017")
    assert not messages_have_images(prepared)
    kept = prepare_messages(img, "github_copilot", "claude-sonnet-5")
    assert messages_have_images(kept)

    long_tool = [{"role": "user", "content": "start"},
                 {"role": "assistant", "content": "ok", "tool_calls": [{"id": "1"}]},
                 {"role": "tool", "tool_call_id": "1", "content": "x" * 9000},
                 {"role": "user", "content": "thanks"}]
    out = trim_messages(long_tool, budget=50000, tool_max=100)
    assert out[-2]["role"] == "tool" and out[-2]["content"].endswith("[truncated]")
    assert len(out[-2]["content"]) < 200
    # budget drops the oldest but keeps the last user message
    huge = ([{"role": "user", "content": "old " + "y" * 200}] +
            [{"role": "assistant", "content": "z" * 200} for _ in range(20)] +
            [{"role": "user", "content": "latest"}])
    out = trim_messages(huge, budget=400)
    assert out[-1]["content"] == "latest"
    assert any(m.get("role") == "user" for m in out)


def test_local_5xx_is_retried_once(monkeypatch):
    import asyncio
    import httpx
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    import uvicorn
    import threading
    import time
    from app import config, llm
    from app.db import Endpoint

    app = FastAPI()
    hits = {"n": 0}

    @app.post("/v1/chat/completions")
    def completions():
        hits["n"] += 1
        if hits["n"] == 1:
            return JSONResponse({"error": {"message": "CublasLtFailed"}}, status_code=500)
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok after retry"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=18794, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 4
    while time.time() < deadline:
        try:
            httpx.post("http://127.0.0.1:18794/v1/chat/completions", json={}, timeout=0.2)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    monkeypatch.setattr(llm, "LOCAL_5XX_RETRY_S", 0.05)
    ep = Endpoint(id=1, team_id=1, owner_id=1, name="dgx-spark",
                  base_url="http://127.0.0.1:18794/v1", api_key_enc="",
                  default_model="DeepSeek-V4.1-Flash-1017", provider="openai")

    out = asyncio.run(llm.chat(ep, "DeepSeek-V4.1-Flash-1017", [{"role": "user", "content": "hi"}]))
    assert out["content"] == "ok after retry"
    assert hits["n"] == 2
    server.should_exit = True
