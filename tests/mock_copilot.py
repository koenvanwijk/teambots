"""Scripted GitHub Copilot token exchange + completions for tests. No real tokens."""
import os
import time

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

# Fake values used only in tests. They are not GitHub-issued secrets.
GITHUB_TOKEN = "ghu_testtoken_notreal_0123456789ab"
COPILOT_TOKEN = "tid=test-copilot-session;proxy-ep=copilot.test"
BASE = os.environ.get("TB_MOCK_COPILOT", "http://127.0.0.1:18793")

app = FastAPI()
stats = {"exchange": 0, "chat": 0, "models": 0}


@app.get("/__stats")
async def get_stats():
    return stats


@app.post("/__reset")
async def reset():
    stats.update(exchange=0, chat=0, models=0)
    return stats


@app.get("/copilot_internal/v2/token")
async def token(authorization: str = Header("")):
    stats["exchange"] += 1
    if authorization != f"Bearer {GITHUB_TOKEN}":
        return JSONResponse({"message": "Bad credentials", "documentation_url": "https://docs.github.com"},
                            status_code=401)
    return {
        "token": COPILOT_TOKEN,
        "expires_at": int(time.time()) + 3600,
        "refresh_in": 1500,
        "endpoints": {"api": BASE},
    }


def _require_session(authorization: str):
    if authorization != f"Bearer {COPILOT_TOKEN}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


@app.get("/models")
@app.get("/v1/models")
async def models(authorization: str = Header("")):
    bad = _require_session(authorization)
    if bad:
        return bad
    stats["models"] += 1
    return {"data": [
        {"id": "gpt-4.1"},
        {"id": "claude-sonnet-5"},
        {"id": "gpt-5"},
    ]}


def _has_image(messages) -> bool:
    for m in messages or []:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for p in c:
            if isinstance(p, dict) and (p.get("type") in ("image_url", "image") or "image_url" in p):
                return True
    return False


@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def chat(request: Request, authorization: str = Header("")):
    bad = _require_session(authorization)
    if bad:
        return bad
    stats["chat"] += 1
    body = await request.json()
    if _has_image(body.get("messages")) and request.headers.get("copilot-vision-request", "").lower() != "true":
        # Match the real Copilot gateway: a plain 400 body when the vision header is missing.
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("Bad Request\n", status_code=400)
    text = ""
    for m in body.get("messages") or []:
        if m.get("role") == "user":
            c = m.get("content")
            text = c if isinstance(c, str) else " ".join(
                (p.get("text") or "") for p in c if isinstance(p, dict)) if isinstance(c, list) else str(c or "")
    if _has_image(body.get("messages")):
        return {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "I can see the image."}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26}}
    if body.get("tools") and "ping" in text.lower():
        return {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "ping", "arguments": '{"ok": true}'}}]}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14}}
    return {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": "hello from mock copilot"}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14}}
