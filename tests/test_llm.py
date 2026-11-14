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
