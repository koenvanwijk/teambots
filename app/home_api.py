"""External API for the overview bot: personal tokens and voice/OpenAI-compatible endpoints.

UI session tokens are not accepted here. After signing in, a person creates a
long-lived token (scope home-bot) that is shown once and stored hashed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import home
from .api import current_user
from .db import ApiToken, User, now, select, session

log = logging.getLogger("teambots.home_api")

tokens_router = APIRouter(prefix="/api")
home_router = APIRouter(prefix="/api/v1/home")
openai_router = APIRouter()

TOKEN_PREFIX = "tbh_"
RATE_LIMIT = 30
RATE_WINDOW = 60.0
_hits: dict[int, deque[float]] = defaultdict(deque)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _rate_limit(user_id: int) -> None:
    t = time.time()
    q = _hits[user_id]
    while q and q[0] < t - RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "rate limit: 30 requests per minute")
    q.append(t)


def _token_out(row: ApiToken, raw: str | None = None) -> dict:
    data = {"id": row.id, "name": row.name, "scope": row.scope, "prefix": row.prefix,
            "created_at": row.created_at, "last_used_at": row.last_used_at,
            "revoked": bool(row.revoked_at)}
    if raw:
        data["token"] = raw
    return data


class TokenIn(BaseModel):
    name: str = "Voice"
    scope: str = "home-bot"


@tokens_router.get("/tokens")
def list_tokens(user: User = Depends(current_user)):
    """List this person's personal API tokens (never the raw secret)."""
    with session() as s:
        rows = s.exec(select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.id.desc())).all()
        return [_token_out(r) for r in rows]


@tokens_router.post("/tokens")
def create_token(body: TokenIn, user: User = Depends(current_user)):
    """Create a long-lived token. The raw value is returned only this once."""
    _need(body.scope == "home-bot", 400, "scope: home-bot")
    name = (body.name or "Voice").strip()[:80] or "Voice"
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = ApiToken(user_id=user.id, name=name, scope="home-bot",
                   token_hash=_hash_token(raw), prefix=raw[:11])
    with session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
        out = _token_out(row, raw)
    log.info("api token created user=%s id=%s prefix=%s", user.id, out["id"], out["prefix"])
    return out


@tokens_router.delete("/tokens/{token_id}")
def revoke_token(token_id: int, user: User = Depends(current_user)):
    with session() as s:
        row = s.get(ApiToken, token_id)
        if not row or row.user_id != user.id:
            raise HTTPException(404, "token not found")
        if not row.revoked_at:
            row.revoked_at = now()
            s.add(row)
            s.commit()
        log.info("api token revoked user=%s id=%s", user.id, token_id)
        return {"ok": True}


def _need(cond, code=403, msg="not allowed"):
    if not cond:
        raise HTTPException(code, msg)


def home_api_user(authorization: str = Header(default=""),
                  request: Request = None) -> User:  # noqa: ARG001
    """Bearer personal API token only (not a UI session token)."""
    raw = (authorization or "").removeprefix("Bearer ").strip()
    if not raw or not raw.startswith(TOKEN_PREFIX):
        raise HTTPException(401, "missing or invalid API token")
    digest = _hash_token(raw)
    with session() as s:
        row = s.exec(select(ApiToken).where(ApiToken.token_hash == digest)).first()
        if not row or row.revoked_at or row.scope != "home-bot":
            raise HTTPException(401, "missing or invalid API token")
        user = s.get(User, row.user_id)
        if not user:
            raise HTTPException(401, "missing or invalid API token")
        row.last_used_at = now()
        s.add(row)
        s.commit()
        _rate_limit(user.id)
        log.info("home api user=%s path=%s", user.id, request.url.path if request else "")
        return user


class HomeMessageIn(BaseModel):
    text: str
    conversation_id: int | None = None
    stream: bool = False
    mode: str = "voice"  # voice (short, no markdown) | chat


def _sse(payload: dict, stream: bool, status: int = 200):
    if not stream:
        return JSONResponse(payload, status_code=status)
    # The model path is not token-streamed; one event carries the finished reply.

    def events():
        yield f"data: {json.dumps(payload, default=str)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(events(), media_type="text/event-stream")


@home_router.post("/message")
async def home_message(body: HomeMessageIn, request: Request, user: User = Depends(home_api_user)):
    """Send a message to the overview bot and get its reply. Visible in the Overview chat."""
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    mode = body.mode if body.mode in ("voice", "chat") else "voice"
    with session() as s:
        try:
            bot, conv = home.ensure_home(s, user)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if body.conversation_id and body.conversation_id != conv.id:
            raise HTTPException(400, "conversation_id is not your Overview chat")
        conv_id = conv.id
    try:
        out = await home.run_home_turn(user.id, text, source="voice" if mode == "voice" else "api",
                                       mode=mode)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    log.info("home message user=%s conv=%s approval=%s", user.id, out["conversation_id"],
             out.get("approval_pending"))
    return _sse({"reply": out["reply"], "conversation_id": out["conversation_id"],
                 "approval_pending": out.get("approval_pending", False)}, body.stream)


@home_router.get("/status")
def home_status(request: Request, user: User = Depends(home_api_user)):
    """A short overview: projects, pending approvals, what is running."""
    with session() as s:
        try:
            bot, conv = home.ensure_home(s, user)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        overview = home.get_overview(s, user)
        from .db import Approval
        pending = s.exec(select(Approval).where(Approval.requester_id == user.id,
                                                Approval.status == "pending")).all()
        return {"conversation_id": conv.id, "overview": overview,
                "pending_approvals": len(pending)}


@home_router.get("/openapi")
def home_openapi(request: Request, user: User = Depends(home_api_user)):
    """Tool/API schema so other agents can call the overview bot."""
    return {
        "name": "teambots-home",
        "description": "The user's TeamBots overview bot: list projects and bots, ask a project bot, get a status summary. Authenticate with a personal API token (scope home-bot) as Bearer.",
        "auth": {"type": "http", "scheme": "bearer", "in": "header"},
        "tools": [
            {"name": "send_home_message",
             "description": "Send a message to the user's overview bot and get its reply.",
             "path": "POST /api/v1/home/message",
             "parameters": {"text": {"type": "string", "required": True},
                            "mode": {"type": "string", "enum": ["voice", "chat"], "default": "voice"},
                            "conversation_id": {"type": "integer"},
                            "stream": {"type": "boolean", "default": False}}},
            {"name": "home_status",
             "description": "Get a quick overview of projects, bots, recent activity and pending approvals.",
             "path": "GET /api/v1/home/status"},
        ],
        "openai": {"path": "POST /v1/chat/completions", "model": "teambots-home",
                   "voice_model": "teambots-home-voice"},
    }


class ChatMessage(BaseModel):
    role: str
    content: str | list | None = ""


class ChatIn(BaseModel):
    model: str = "teambots-home"
    messages: list[ChatMessage]
    stream: bool = False
    # Non-standard but handy for voice stacks that can pass it:
    conversation_id: int | None = None


def _last_user_text(messages: list[ChatMessage]) -> str:
    for m in reversed(messages):
        if m.role == "user":
            c = m.content
            if isinstance(c, list):
                parts = [x.get("text", "") if isinstance(x, dict) else str(x) for x in c]
                return "\n".join(p for p in parts if p).strip()
            return (c or "").strip()
    return ""


@openai_router.get("/v1/models")
def list_models(request: Request, user: User = Depends(home_api_user)):
    return {"object": "list", "data": [
        {"id": "teambots-home", "object": "model", "owned_by": "teambots"},
        {"id": "teambots-home-voice", "object": "model", "owned_by": "teambots"},
    ]}


@openai_router.post("/v1/chat/completions")
async def chat_completions(body: ChatIn, request: Request, user: User = Depends(home_api_user)):
    """OpenAI-compatible wrapper so Grok Bot voice or a Tesla agent can plug in directly."""
    text = _last_user_text(body.messages)
    if not text:
        raise HTTPException(400, "messages must include a user turn")
    model = body.model or "teambots-home"
    mode = "voice" if "voice" in model or model == "teambots-home" else "chat"
    # Default the OpenAI wrapper to voice-style replies: that is what voice stacks call.
    if model in ("teambots-home", "teambots-home-voice"):
        mode = "voice"
    try:
        out = await home.run_home_turn(user.id, text, source="voice", mode=mode)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    created = int(time.time())
    payload = {
        "id": f"chatcmpl-tb{out['message_id']}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": out["reply"]},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "conversation_id": out["conversation_id"],
        "approval_pending": out.get("approval_pending", False),
    }
    log.info("home chat.completions user=%s conv=%s", user.id, out["conversation_id"])
    if not body.stream:
        return payload

    def events():
        chunk = {
            "id": payload["id"], "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": out["reply"]},
                         "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(events(), media_type="text/event-stream")
