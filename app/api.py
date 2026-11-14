"""REST API. Every route checks permissions through app.access."""
import asyncio
import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel
import httpx
import websockets

from . import agent, config, copilot, events, home, llm, mcp_client, oauth, routines, sandbox, screen, secrets_http
from .access import bot_role, can_manage, can_read_conversation, team_role
from .db import (Approval, AuthToken, Bot, BotAccess, ChannelMember, Conversation, Endpoint, Identity, McpServer,
                 Memory, Message, OAuthState, Routine, Secret, Skill, Team, TeamMember, User, now, select, session)

router = APIRouter(prefix="/api")


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()


def current_user(authorization: str = Header(default=""), token: str = Query(default=""),
                 tb_token: str = Cookie(default="")) -> User:
    tok = authorization.removeprefix("Bearer ").strip() or token or tb_token
    user = _user_for_token(tok)
    if not user:
        raise HTTPException(401, "not signed in")
    return user


def _user_for_token(tok: str | None) -> User | None:
    if not tok:
        return None
    with session() as s:
        row = s.get(AuthToken, tok)
        return s.get(User, row.user_id) if row else None


def _need(cond, code=403, msg="not allowed"):
    if not cond:
        raise HTTPException(code, msg)


def _bot(s, bot_id: int, user: User, manage: bool = False) -> Bot:
    bot = s.get(Bot, bot_id)
    _need(bot and bot_role(s, user.id, bot), 404, "bot not found")
    if manage:
        _need(can_manage(s, user.id, bot))
    return bot


def _bot_out(s, bot: Bot, user: User) -> dict:
    return {**bot.model_dump(), "role": bot_role(s, user.id, bot)}


# ---------------------------------------------------------------- auth and teams

class Credentials(BaseModel):
    username: str
    password: str
    display_name: str = ""


@router.post("/register")
def register(body: Credentials):
    _need(re.fullmatch(r"[a-z0-9_.-]{2,32}", body.username), 400, "username: 2-32 chars of a-z 0-9 _ . -")
    _need(len(body.password) >= 8, 400, "password needs at least 8 characters")
    with session() as s:
        _need(not s.exec(select(User).where(User.username == body.username)).first(), 409, "username is taken")
        salt = secrets.token_hex(8)
        u = User(username=body.username, display_name=body.display_name or body.username,
                 password_hash=f"{salt}${_hash(body.password, salt)}")
        s.add(u)
        s.commit()
    return login(body)


@router.post("/login")
def login(body: Credentials):
    with session() as s:
        u = s.exec(select(User).where(User.username == body.username)).first()
        salt, _, digest = (u.password_hash if u else "x$x").partition("$")
        _need(u and secrets.compare_digest(_hash(body.password, salt), digest), 401, "wrong username or password")
        tok = secrets.token_urlsafe(32)
        s.add(AuthToken(token=tok, user_id=u.id))
        s.commit()
    return {"token": tok, "user": {"id": u.id, "username": u.username, "display_name": u.display_name}}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "username": user.username, "display_name": user.display_name}


# ---------------------------------------------------------------- sign in with GitHub or Google
#
# A person signs in with their own GitHub or Google account; the identity is not tied to any client.
# A valid project invite code is the gate for creating a new account: no invite (and no allowlisted
# email) means no new account. Membership stays per project, so one person can work across clients.

_OAUTH_STATE_TTL = 600  # seconds a sign-in round trip may take


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _issue_token(s, user_id: int) -> str:
    tok = secrets.token_urlsafe(32)
    s.add(AuthToken(token=tok, user_id=user_id, created_at=now()))
    return tok


def _signup_allowed_email(email: str) -> bool:
    """Whether an email may create a new account without an invite (TB_OAUTH_ALLOWLIST)."""
    al = config.OAUTH_ALLOWLIST
    if not al or not email:
        return False
    email = email.lower()
    domain = email.rsplit("@", 1)[-1]
    return email in al or f"@{domain}" in al or domain in al


def _unique_username(s, email: str, name: str) -> str:
    base = re.sub(r"[^a-z0-9_.-]", "", (email.split("@")[0] or name).lower()) or "user"
    base = base[:28] if len(base) >= 2 else (base + "user")[:28]
    cand, n = base, 0
    while s.exec(select(User).where(User.username == cand)).first():
        n += 1
        cand = f"{base[:28 - len(str(n))]}{n}"
    return cand


def _maybe_join(s, user_id: int, invite_code: str) -> None:
    if not invite_code:
        return
    t = s.exec(select(Team).where(Team.invite_code == invite_code)).first()
    if t and not team_role(s, user_id, t.id):
        s.add(TeamMember(team_id=t.id, user_id=user_id))


def _find_or_create_user(s, email: str, name: str, invite_code: str) -> User:
    # One person, one account: reuse an account that already owns this verified email.
    if email:
        same = s.exec(select(User).where(User.email == email)).first()
        if same:
            return same
    has_invite = bool(invite_code and s.exec(select(Team).where(Team.invite_code == invite_code)).first())
    if not has_invite and not _signup_allowed_email(email):
        raise oauth.OAuthError("You need an invite link to create a TeamBots account. "
                               "Ask a project admin for one.")
    user = User(username=_unique_username(s, email, name),
                display_name=name or (email.split("@")[0] if email else "user"),
                password_hash="!", email=email)  # "!" never matches a password hash: sign in via the provider
    s.add(user)
    s.flush()
    return user


def _complete_signin(s, provider: str, ident: dict, link_user_id: Optional[int], invite_code: str) -> str:
    subject, email = ident["subject"], (ident.get("email") or "").lower()
    name = ident.get("name") or ""
    existing = s.exec(select(Identity).where(Identity.provider == provider,
                                             Identity.subject == subject)).first()
    if existing:
        if link_user_id and link_user_id != existing.user_id:
            raise oauth.OAuthError("This account is already linked to another TeamBots user.")
        user = s.get(User, existing.user_id)
        if not user:
            raise oauth.OAuthError("The linked account no longer exists.")
        existing.last_login_at = now()
        if email and not existing.email:
            existing.email = email
        s.add(existing)
    else:
        if link_user_id is not None:
            user = s.get(User, link_user_id)
            if not user:
                raise oauth.OAuthError("Your session has expired. Sign in again, then link the account.")
        else:
            user = _find_or_create_user(s, email, name, invite_code)
        s.add(Identity(provider=provider, subject=subject, user_id=user.id, email=email, last_login_at=now()))
        s.flush()
    if email and not user.email:
        user.email = email
        s.add(user)
    _maybe_join(s, user.id, invite_code)
    return _issue_token(s, user.id)


def _oauth_redirect(request: Request, token: str = "", error: str = "") -> RedirectResponse:
    # The token rides back in the URL fragment (never sent to a server, so it stays out of access logs);
    # the frontend moves it into localStorage. A cookie is set too, for the noVNC websocket.
    frag = f"#tb_oauth={token}" if token else (f"#tb_oauth_error={quote(error)}" if error else "")
    resp = RedirectResponse(f"/{frag}", status_code=302)
    if token:
        resp.set_cookie("tb_token", token, max_age=60 * 60 * 24 * 30, **_cookie_flags(request))
    return resp


@router.get("/auth/providers")
def auth_providers():
    """Which sign-in providers are configured, for the login screen."""
    return {"providers": oauth.providers_public(), "public_url_set": bool(config.PUBLIC_URL)}


@router.get("/auth/{provider}/start")
def auth_start(provider: str, request: Request, invite: str = Query(default=""), link: int = Query(default=0),
               authorization: str = Header(default=""), token: str = Query(default=""),
               tb_token: str = Cookie(default="")):
    """Begin a sign-in. ?invite=CODE lets a new person create an account and join that project.
    ?link=1 (from an account-settings action, while signed in) attaches the new identity to the
    caller's own account; without it an ambient session is ignored, so no identity is linked by surprise."""
    link_user = None
    if link:
        link_user = _user_for_token(authorization.removeprefix("Bearer ").strip() or token or tb_token)
        if link_user is None:
            return _oauth_redirect(request, error="Sign in first, then link another account.")
    try:
        state = secrets.token_urlsafe(24)
        url = oauth.authorize_url(provider, state)
    except oauth.OAuthError as e:
        return _oauth_redirect(request, error=str(e))
    with session() as s:
        s.add(OAuthState(state=state, provider=provider, invite_code=invite.strip(),
                         link_user_id=link_user.id if link_user else None))
        s.commit()
    return RedirectResponse(url, status_code=302)


@router.get("/auth/{provider}/callback")
def auth_callback(provider: str, request: Request, code: str = Query(default=""),
                  state: str = Query(default=""), error: str = Query(default="")):
    if error:
        return _oauth_redirect(request, error=f"{oauth.NAMES.get(provider, provider)} sign-in was cancelled.")
    with session() as s:  # consume the one-time state; copy what we need before it expires on commit
        st = s.get(OAuthState, state) if state else None
        if not st or st.provider != provider:
            return _oauth_redirect(request, error="This sign-in link has expired. Please start again.")
        created, link_user_id, invite_code = st.created_at, st.link_user_id, st.invite_code
        s.delete(st)
        s.commit()
    if (now() - _aware(created)).total_seconds() > _OAUTH_STATE_TTL:
        return _oauth_redirect(request, error="This sign-in link has expired. Please start again.")
    if not code:
        return _oauth_redirect(request, error="The sign-in provider returned no code.")
    try:
        ident = oauth.exchange_code(provider, code)
        with session() as s:
            tok = _complete_signin(s, provider, ident, link_user_id, invite_code)
            s.commit()
    except oauth.OAuthError as e:
        return _oauth_redirect(request, error=str(e))
    return _oauth_redirect(request, token=tok)


@router.get("/auth/identities")
def auth_identities(user: User = Depends(current_user)):
    """The sign-in methods linked to the caller's account, for an account-settings view."""
    with session() as s:
        rows = s.exec(select(Identity).where(Identity.user_id == user.id)).all()
        has_password = bool(user.password_hash) and user.password_hash != "!"
    return {"has_password": has_password,
            "identities": [{"id": i.id, "provider": i.provider, "name": oauth.NAMES.get(i.provider, i.provider),
                            "email": i.email} for i in rows]}


# ---------------------------------------------------------------- overview bot (per user, not tied to a project)

@router.get("/home")
def get_home(user: User = Depends(current_user)):
    """The caller's overview bot and private chat. Created on first visit once a model endpoint exists."""
    with session() as s:
        try:
            bot, conv = home.ensure_home(s, user)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return home.home_out(s, user, bot, conv)


class HomePatch(BaseModel):
    endpoint_id: Optional[int] = None
    model: Optional[str] = None
    computer_use: Optional[bool] = None


@router.patch("/home")
def patch_home(body: HomePatch, user: User = Depends(current_user)):
    """Choose the model endpoint (from any of your projects) and options for the overview bot."""
    with session() as s:
        try:
            bot = home.apply_settings(s, user, endpoint_id=body.endpoint_id, model=body.model,
                                      computer_use=body.computer_use)
            conv = home._home_conv(s, bot)
            if not conv:
                bot, conv = home.ensure_home(s, user)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return home.home_out(s, user, bot, conv)


@router.get("/home/files")
async def home_files(user: User = Depends(current_user)):
    with session() as s:
        try:
            bot, _ = home.ensure_home(s, user)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        key = home.sandbox_key(bot)
    return await sandbox.list_files(key)




class TeamIn(BaseModel):
    name: str  # the project
    client: str = ""  # optional customer the project belongs to


@router.post("/teams")
def create_team(body: TeamIn, user: User = Depends(current_user)):
    with session() as s:
        t = Team(name=body.name, client=body.client.strip(), invite_code=secrets.token_urlsafe(8))
        s.add(t)
        s.commit()
        s.add(TeamMember(team_id=t.id, user_id=user.id, role="admin"))
        s.commit()
    return {**t.model_dump(), "role": "admin"}


class JoinIn(BaseModel):
    invite_code: str


@router.post("/teams/join")
def join_team(body: JoinIn, user: User = Depends(current_user)):
    with session() as s:
        t = s.exec(select(Team).where(Team.invite_code == body.invite_code)).first()
        _need(t, 404, "invalid invite code")
        if not team_role(s, user.id, t.id):
            s.add(TeamMember(team_id=t.id, user_id=user.id))
            s.commit()
    return {"id": t.id, "name": t.name}


@router.get("/teams")
def my_teams(user: User = Depends(current_user)):
    with session() as s:
        out = []
        for m in s.exec(select(TeamMember).where(TeamMember.user_id == user.id)).all():
            t = s.get(Team, m.team_id)
            d = {"id": t.id, "name": t.name, "client": t.client, "role": m.role, "bot_sharing": t.bot_sharing}
            if m.role == "admin":
                d["invite_code"] = t.invite_code
            out.append(d)
    return out


@router.get("/overview")
def overview(user: User = Depends(current_user)):
    """The layer above projects: every project this person is in, grouped by client in the UI,
    with its bots and the latest shared activity."""
    out = []
    with session() as s:
        for m in s.exec(select(TeamMember).where(TeamMember.user_id == user.id)).all():
            t = s.get(Team, m.team_id)
            bots = [b for b in s.exec(select(Bot).where(Bot.team_id == t.id)).all() if bot_role(s, user.id, b)]
            feeds = {c.id: c.bot_id for c in s.exec(select(Conversation).where(
                Conversation.team_id == t.id, Conversation.kind == "feed")).all()
                if c.bot_id in {b.id for b in bots}}
            recent = s.exec(select(Message).where(Message.conversation_id.in_(list(feeds) or [0]))
                            .order_by(Message.id.desc()).limit(5)).all()
            names = {b.id: b.name for b in bots}
            out.append({"id": t.id, "name": t.name, "client": t.client, "role": m.role,
                        "people": len(s.exec(select(TeamMember).where(TeamMember.team_id == t.id)).all()),
                        "bots": [{"id": b.id, "name": b.name, "handle": b.handle} for b in bots],
                        "activity": [{"bot": names.get(x.author_bot_id, "bot"), "content": x.content,
                                      "at": x.created_at.isoformat()} for x in recent]})
    return sorted(out, key=lambda p: (p["client"].lower(), p["name"].lower()))


@router.get("/teams/{team_id}/members")
def team_members(team_id: int, user: User = Depends(current_user)):
    with session() as s:
        _need(team_role(s, user.id, team_id))
        rows = s.exec(select(TeamMember).where(TeamMember.team_id == team_id)).all()
        users = {m.user_id: s.get(User, m.user_id) for m in rows}
        return [{"user_id": m.user_id, "role": m.role, "display_name": users[m.user_id].display_name,
                 "username": users[m.user_id].username} for m in rows]


@router.get("/teams/{team_id}/files")
async def team_files(team_id: int, user: User = Depends(current_user)):
    """Files on the project computer, for the '@' menu. Empty when the computer cannot be reached."""
    with session() as s:
        _need(team_role(s, user.id, team_id))
    try:
        return await sandbox.list_files(f"p{team_id}")
    except Exception:  # noqa: BLE001 - the menu simply shows no files
        return []


class TeamPatch(BaseModel):
    bot_sharing: Optional[str] = None
    name: Optional[str] = None
    client: Optional[str] = None


@router.patch("/teams/{team_id}")
def patch_team(team_id: int, body: TeamPatch, user: User = Depends(current_user)):
    _need(body.bot_sharing in (None, "everyone", "admins"), 400, "bot_sharing: everyone | admins")
    _need(body.name is None or body.name.strip(), 400, "the project needs a name")
    with session() as s:
        _need(team_role(s, user.id, team_id) == "admin")
        t = s.get(Team, team_id)
        if body.bot_sharing is not None:
            t.bot_sharing = body.bot_sharing
        if body.name is not None:
            t.name = body.name.strip()
        if body.client is not None:
            t.client = body.client.strip()
        s.add(t)
        s.commit()
    return {"ok": True}


# ---------------------------------------------------------------- endpoints

class EndpointIn(BaseModel):
    name: str
    provider: str = "openai"  # openai | github_copilot
    base_url: str = ""
    github_domain: str = ""
    api_key: str = ""
    default_model: str
    shared: bool = False


def _ep_out(e: Endpoint) -> dict:  # the key never leaves the server
    return {"id": e.id, "name": e.name, "base_url": e.base_url, "default_model": e.default_model,
            "shared": e.shared, "owner_id": e.owner_id, "has_key": bool(e.api_key_enc),
            "provider": getattr(e, "provider", None) or "openai",
            "github_domain": getattr(e, "github_domain", None) or ""}


def _normalize_provider(value: str) -> str:
    provider = (value or "openai").strip()
    _need(provider in ("openai", "github_copilot"), 400, "provider: openai | github_copilot")
    return provider


def _github_domain_or_400(raw: str) -> str:
    try:
        return copilot.normalize_domain(raw)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/teams/{team_id}/endpoints")
def list_endpoints(team_id: int, user: User = Depends(current_user)):
    with session() as s:
        _need(team_role(s, user.id, team_id))
        rows = s.exec(select(Endpoint).where(Endpoint.team_id == team_id)).all()
    return [_ep_out(e) for e in rows if e.shared or e.owner_id == user.id]


@router.post("/teams/{team_id}/endpoints")
def create_endpoint(team_id: int, body: EndpointIn, user: User = Depends(current_user)):
    _need(body.name.strip() and body.default_model.strip(), 400, "name and default model cannot be empty")
    provider = _normalize_provider(body.provider)
    if provider == "github_copilot":
        domain = _github_domain_or_400(body.github_domain or "github.com")
        _need(body.api_key.strip(), 400, "a GitHub token is required")
        base = body.base_url.strip().rstrip("/")
        if base:
            _need(base.startswith(("http://", "https://")), 400, "base_url must start with http:// or https://")
        else:
            base = copilot.fallback_base(domain)
        fields = dict(provider=provider, github_domain=domain, base_url=base,
                      api_key_enc=config.encrypt(body.api_key.strip()))
    else:
        _need(body.base_url.startswith(("http://", "https://")), 400, "base_url must start with http:// or https://")
        fields = dict(provider="openai", github_domain="", base_url=body.base_url.rstrip("/"),
                      api_key_enc=config.encrypt(body.api_key))
    with session() as s:
        _need(team_role(s, user.id, team_id))
        e = Endpoint(team_id=team_id, owner_id=user.id, name=body.name.strip(),
                     default_model=body.default_model.strip(), shared=body.shared, **fields)
        s.add(e)
        s.commit()
    return _ep_out(e)


class EndpointPatch(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    github_domain: Optional[str] = None
    api_key: Optional[str] = None  # left out = keep the stored key
    default_model: Optional[str] = None
    shared: Optional[bool] = None


@router.patch("/endpoints/{endpoint_id}")
def patch_endpoint(endpoint_id: int, body: EndpointPatch, user: User = Depends(current_user)):
    data = body.model_dump(exclude_none=True)
    _need(all(str(data[k]).strip() for k in ("name", "default_model") if k in data), 400,
          "name and default model cannot be empty")
    with session() as s:
        e = s.get(Endpoint, endpoint_id)
        _need(e and e.owner_id == user.id, 404, "endpoint not found")
        provider = _normalize_provider(data.get("provider", getattr(e, "provider", None) or "openai"))
        if provider == "github_copilot":
            raw_domain = data["github_domain"] if "github_domain" in data else (e.github_domain or "github.com")
            domain = _github_domain_or_400(raw_domain)
            data["github_domain"] = domain
            if "api_key" in data:
                _need(str(data["api_key"]).strip(), 400, "GitHub token cannot be empty")
            elif not e.api_key_enc:
                raise HTTPException(400, "a GitHub token is required")
            if "base_url" in data:
                base = data["base_url"].strip().rstrip("/")
                if base:
                    _need(base.startswith(("http://", "https://")), 400,
                          "base_url must start with http:// or https://")
                    data["base_url"] = base
                else:
                    data["base_url"] = copilot.fallback_base(domain)
            elif (getattr(e, "provider", None) or "openai") != "github_copilot" or "github_domain" in data:
                data["base_url"] = copilot.fallback_base(domain)
        else:
            data["github_domain"] = data.get("github_domain", "") or ""
            if "base_url" in data:
                _need(data["base_url"].strip().startswith(("http://", "https://")), 400,
                      "base_url must start with http:// or https://")
                data["base_url"] = data["base_url"].strip().rstrip("/")
            elif (getattr(e, "provider", None) or "openai") == "github_copilot":
                raise HTTPException(400, "base_url must start with http:// or https://")
        data["provider"] = provider
        if "api_key" in data:
            e.api_key_enc = config.encrypt(data.pop("api_key"))
        for k, v in data.items():
            setattr(e, k, v.strip() if isinstance(v, str) else v)
        s.add(e)
        s.commit()
    return _ep_out(e)


@router.delete("/endpoints/{endpoint_id}")
def delete_endpoint(endpoint_id: int, user: User = Depends(current_user)):
    with session() as s:
        e = s.get(Endpoint, endpoint_id)
        _need(e and e.owner_id == user.id, 404, "endpoint not found")
        _need(not s.exec(select(Bot).where(Bot.endpoint_id == e.id)).first(), 409, "endpoint is still used by a bot")
        s.delete(e)
        s.commit()
    return {"ok": True}


@router.post("/endpoints/{endpoint_id}/test")
async def test_endpoint(endpoint_id: int, user: User = Depends(current_user)):
    """Checks reachability and whether the model answers with a tool call."""
    with session() as s:
        e = s.get(Endpoint, endpoint_id)
        _need(e and e.owner_id == user.id, 404, "endpoint not found")
    tool = {"type": "function", "function": {"name": "ping", "description": "Call this tool.", "parameters": {
        "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}}}
    try:
        reply = await llm.chat(e, "", [{"role": "user", "content": "Call the ping tool with ok=true."}], [tool])
    except llm.LLMError as err:
        return {"reachable": False, "tool_calling": False, "detail": str(err)}
    return {"reachable": True, "tool_calling": bool(reply["tool_calls"]),
            "detail": "ok" if reply["tool_calls"] else "the model answered in text instead of calling the tool"}


@router.get("/endpoints/{endpoint_id}/models")
async def list_endpoint_models(endpoint_id: int, user: User = Depends(current_user)):
    """Best-effort model list. The key is never returned."""
    with session() as s:
        e = s.get(Endpoint, endpoint_id)
        _need(e, 404, "endpoint not found")
        _need(e.owner_id == user.id or (e.shared and team_role(s, user.id, e.team_id)), 404, "endpoint not found")
    try:
        models = await llm.list_models(e)
    except llm.LLMError as err:
        return {"models": [], "detail": str(err)}
    return {"models": models, "detail": "ok"}


# ---------------------------------------------------------------- bots and sharing

class BotIn(BaseModel):
    name: str
    handle: str
    instructions: str = ""
    endpoint_id: int
    model: str = ""
    is_team: bool = False
    approval_mode: str = "ask"
    computer_use: bool = False


class BotPatch(BaseModel):
    computer_use: Optional[bool] = None
    name: Optional[str] = None
    handle: Optional[str] = None
    instructions: Optional[str] = None
    endpoint_id: Optional[int] = None
    model: Optional[str] = None
    approval_mode: Optional[str] = None


def _usable_endpoint(s, endpoint_id: int, team_id: int, user: User) -> None:
    e = s.get(Endpoint, endpoint_id)
    _need(e and e.team_id == team_id and (e.shared or e.owner_id == user.id), 400, "endpoint not available")


@router.get("/teams/{team_id}/bots")
def list_bots(team_id: int, user: User = Depends(current_user)):
    with session() as s:
        _need(team_role(s, user.id, team_id))
        rows = s.exec(select(Bot).where(Bot.team_id == team_id)).all()
        return [_bot_out(s, b, user) for b in rows if bot_role(s, user.id, b)]


@router.post("/teams/{team_id}/bots")
def create_bot(team_id: int, body: BotIn, user: User = Depends(current_user)):
    _need(re.fullmatch(r"[a-z0-9_-]{2,32}", body.handle), 400, "handle: 2-32 chars of a-z 0-9 _ -")
    _need(body.approval_mode in ("ask", "auto"), 400, "approval_mode: ask | auto")
    with session() as s:
        _need(team_role(s, user.id, team_id))
        _usable_endpoint(s, body.endpoint_id, team_id, user)
        _need(not s.exec(select(Bot).where(Bot.team_id == team_id, Bot.handle == body.handle)).first(),
              409, "handle is taken")
        b = Bot(team_id=team_id, owner_id=user.id, **body.model_dump())
        s.add(b)
        s.commit()
        _add_bot_to_team_channels(s, b)
        return _bot_out(s, b, user)


@router.patch("/bots/{bot_id}")
def patch_bot(bot_id: int, body: BotPatch, user: User = Depends(current_user)):
    with session() as s:
        b = _bot(s, bot_id, user, manage=True)
        data = body.model_dump(exclude_none=True)
        if "endpoint_id" in data:
            _usable_endpoint(s, data["endpoint_id"], b.team_id, user)
        _need(data.get("approval_mode", "ask") in ("ask", "auto"), 400, "approval_mode: ask | auto")
        _need(data.get("name", "x").strip(), 400, "the bot needs a name")
        if data.get("handle", b.handle) != b.handle:
            _need(re.fullmatch(r"[a-z0-9_-]{2,32}", data["handle"]), 400, "handle: 2-32 chars of a-z 0-9 _ -")
            _need(not s.exec(select(Bot).where(Bot.team_id == b.team_id, Bot.handle == data["handle"])).first(),
                  409, "handle is taken")
        renamed = "name" in data and data["name"].strip() != b.name
        for k, v in data.items():
            setattr(b, k, v.strip() if k == "name" else v)
        s.add(b)
        if renamed:  # the bot's chats and its activity feed carry its name
            for c in s.exec(select(Conversation).where(Conversation.bot_id == b.id)).all():
                c.name = f"{b.name} activity" if c.kind == "feed" else b.name
                s.add(c)
        s.commit()
        return _bot_out(s, b, user)


@router.delete("/bots/{bot_id}")
async def delete_bot(bot_id: int, user: User = Depends(current_user)):
    """Owner only. Removes the bot for everyone, with its chats, memory, skills and routines."""
    with session() as s:
        b = s.get(Bot, bot_id)
        _need(b and b.owner_id == user.id, 404, "bot not found")
        _need(not b.is_home, 400, "the Overview bot cannot be deleted")
        for r in s.exec(select(Routine).where(Routine.bot_id == bot_id)).all():
            routines.remove(r.id)
        for conv in s.exec(select(Conversation).where(Conversation.bot_id == bot_id)).all():
            for m in s.exec(select(Message).where(Message.conversation_id == conv.id)).all():
                s.delete(m)
            s.delete(conv)
        for model in (BotAccess, Memory, Skill, Routine, Secret, McpServer, Approval, ChannelMember):
            for row in s.exec(select(model).where(model.bot_id == bot_id)).all():
                s.delete(row)
        s.delete(b)
        s.commit()
    return {"ok": True}


class AccessEntry(BaseModel):
    user_id: int
    role: str = "collaborator"


class ShareIn(BaseModel):
    published: bool
    access: str = "team"
    members: list[AccessEntry] = []


@router.get("/bots/{bot_id}/share")
def get_share(bot_id: int, user: User = Depends(current_user)):
    with session() as s:
        b = _bot(s, bot_id, user, manage=True)
        rows = s.exec(select(BotAccess).where(BotAccess.bot_id == bot_id)).all()
        return {"published": b.published, "access": b.access,
                "members": [{"user_id": r.user_id, "role": r.role} for r in rows]}


@router.put("/bots/{bot_id}/share")
def share_bot(bot_id: int, body: ShareIn, user: User = Depends(current_user)):
    _need(body.access in ("team", "selected"), 400, "access: team | selected")
    with session() as s:
        b = _bot(s, bot_id, user, manage=True)
        _need(b.is_team, 400, "only a Team Bot can be shared")
        team = s.get(Team, b.team_id)
        _need(team.bot_sharing == "everyone" or team_role(s, user.id, b.team_id) == "admin",
              403, "bot sharing is limited to team admins")
        b.published, b.access = body.published, body.access
        s.add(b)
        for r in s.exec(select(BotAccess).where(BotAccess.bot_id == bot_id)).all():
            s.delete(r)
        for m in body.members:
            _need(m.role in ("manager", "collaborator"), 400, "role: manager | collaborator")
            if m.user_id != b.owner_id and team_role(s, m.user_id, b.team_id):
                s.add(BotAccess(bot_id=bot_id, user_id=m.user_id, role=m.role))
        s.commit()
    return {"ok": True}


# ---------------------------------------------------------------- memory, skills, secrets, MCP

@router.get("/bots/{bot_id}/memories")
def list_memories(bot_id: int, user: User = Depends(current_user)):
    """Team memory for everyone; your own personal memory; pending learnings for owner and managers."""
    with session() as s:
        b = _bot(s, bot_id, user)
        manage = can_manage(s, user.id, b)
        rows = s.exec(select(Memory).where(Memory.bot_id == bot_id)).all()
        names = {u.id: u.display_name for u in s.exec(select(User)).all()}
    out = []
    for m in rows:
        mine = m.scope == "personal" and m.user_id == user.id
        team_ok = m.scope == "team" and (m.status == "active" or manage or m.proposed_by == user.id)
        if mine or team_ok:
            out.append({"id": m.id, "scope": m.scope, "content": m.content, "status": m.status,
                        "proposed_by": names.get(m.proposed_by, "")})
    return out


class MemoryIn(BaseModel):
    content: str
    scope: str = "personal"


@router.post("/bots/{bot_id}/memories")
def add_memory(bot_id: int, body: MemoryIn, user: User = Depends(current_user)):
    with session() as s:
        b = _bot(s, bot_id, user)
        if body.scope == "team":
            m = Memory(bot_id=bot_id, scope="team", content=body.content, proposed_by=user.id,
                       status="active" if can_manage(s, user.id, b) else "pending")
        else:
            m = Memory(bot_id=bot_id, scope="personal", user_id=user.id, content=body.content)
        s.add(m)
        s.commit()
    return {"id": m.id, "status": m.status}


class ReviewIn(BaseModel):
    approve: bool


@router.post("/memories/{memory_id}/review")
def review_memory(memory_id: int, body: ReviewIn, user: User = Depends(current_user)):
    """The human gate on shared knowledge: an owner or manager approves or rejects a learning."""
    with session() as s:
        m = s.get(Memory, memory_id)
        _need(m and m.status == "pending", 404, "no pending learning with this id")
        _bot(s, m.bot_id, user, manage=True)
        if body.approve:
            m.status = "active"
            s.add(m)
        else:
            s.delete(m)
        s.commit()
    return {"ok": True}


@router.delete("/memories/{memory_id}")
def delete_memory(memory_id: int, user: User = Depends(current_user)):
    with session() as s:
        m = s.get(Memory, memory_id)
        _need(m, 404, "memory not found")
        b = _bot(s, m.bot_id, user)
        _need((m.scope == "personal" and m.user_id == user.id) or (m.scope == "team" and can_manage(s, user.id, b)))
        s.delete(m)
        s.commit()
    return {"ok": True}


class SkillIn(BaseModel):
    name: str
    description: str = ""
    instructions: str


@router.get("/bots/{bot_id}/skills")
def list_skills(bot_id: int, user: User = Depends(current_user)):
    with session() as s:
        _bot(s, bot_id, user)
        return [k.model_dump() for k in s.exec(select(Skill).where(Skill.bot_id == bot_id)).all()]


@router.post("/bots/{bot_id}/skills")
def add_skill(bot_id: int, body: SkillIn, user: User = Depends(current_user)):
    with session() as s:
        _bot(s, bot_id, user, manage=True)
        k = Skill(bot_id=bot_id, created_by=user.id, **body.model_dump())
        s.add(k)
        s.commit()
        return k.model_dump()


@router.delete("/skills/{skill_id}")
def delete_skill(skill_id: int, user: User = Depends(current_user)):
    with session() as s:
        k = s.get(Skill, skill_id)
        _need(k, 404, "skill not found")
        _bot(s, k.bot_id, user, manage=True)
        s.delete(k)
        s.commit()
    return {"ok": True}


class SecretIn(BaseModel):
    name: str
    value: str
    hosts: str = ""  # comma-separated hosts this secret may be sent to, e.g. "api.github.com"


@router.get("/bots/{bot_id}/secrets")
def list_secrets(bot_id: int, user: User = Depends(current_user)):
    with session() as s:
        _bot(s, bot_id, user, manage=True)
        return [{"id": r.id, "name": r.name, "hosts": r.hosts}
                for r in s.exec(select(Secret).where(Secret.bot_id == bot_id)).all()]


@router.post("/bots/{bot_id}/secrets")
def add_secret(bot_id: int, body: SecretIn, user: User = Depends(current_user)):
    _need(re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", body.name), 400, "name must look like AN_ENV_VAR")
    with session() as s:
        _bot(s, bot_id, user, manage=True)
        for r in s.exec(select(Secret).where(Secret.bot_id == bot_id, Secret.name == body.name)).all():
            s.delete(r)
        hosts = ",".join(h.strip().lower() for h in body.hosts.split(",") if h.strip())
        _need(all(re.fullmatch(r"[a-z0-9.-]+", h) for h in hosts.split(",") if h), 400,
              "hosts: plain host names, comma-separated")
        r = Secret(bot_id=bot_id, name=body.name, value_enc=config.encrypt(body.value), hosts=hosts)
        s.add(r)
        s.commit()
    return {"id": r.id, "name": r.name, "hosts": r.hosts}


@router.delete("/secrets/{secret_id}")
def delete_secret(secret_id: int, user: User = Depends(current_user)):
    with session() as s:
        r = s.get(Secret, secret_id)
        _need(r, 404, "secret not found")
        _bot(s, r.bot_id, user, manage=True)
        s.delete(r)
        s.commit()
    return {"ok": True}


class McpIn(BaseModel):
    name: str
    url: str
    headers: dict[str, str] = {}
    trusted: bool = False


@router.get("/bots/{bot_id}/mcp")
def list_mcp(bot_id: int, user: User = Depends(current_user)):
    with session() as s:
        _bot(s, bot_id, user, manage=True)
        rows = s.exec(select(McpServer).where(McpServer.bot_id == bot_id)).all()
    return [{"id": r.id, "name": r.name, "url": r.url, "trusted": r.trusted} for r in rows]


@router.post("/bots/{bot_id}/mcp")
async def add_mcp(bot_id: int, body: McpIn, user: User = Depends(current_user)):
    _need(re.fullmatch(r"[a-z0-9_-]{1,24}", body.name), 400, "name: 1-24 chars of a-z 0-9 _ -")
    with session() as s:
        _bot(s, bot_id, user, manage=True)
        r = McpServer(bot_id=bot_id, name=body.name, url=body.url, trusted=body.trusted,
                      headers_enc=config.encrypt(json.dumps(body.headers)) if body.headers else "")
        s.add(r)
        s.commit()
    try:
        tools = [t["name"] for t in await asyncio.wait_for(mcp_client.list_tools(r, ttl=0), 20)]
        error = ""
    except Exception as e:  # noqa: BLE001
        tools, error = [], f"saved, but the server did not answer: {e.__class__.__name__}"
    return {"id": r.id, "name": r.name, "tools": tools, "error": error}


@router.delete("/mcp/{server_id}")
def delete_mcp(server_id: int, user: User = Depends(current_user)):
    with session() as s:
        r = s.get(McpServer, server_id)
        _need(r, 404, "server not found")
        _bot(s, r.bot_id, user, manage=True)
        s.delete(r)
        s.commit()
    mcp_client.invalidate(server_id)
    return {"ok": True}


# ---------------------------------------------------------------- routines

class RoutineIn(BaseModel):
    name: str
    cron: str
    prompt: str
    conversation_id: int


class RoutinePatch(BaseModel):
    enabled: Optional[bool] = None
    name: Optional[str] = None
    cron: Optional[str] = None
    prompt: Optional[str] = None


def _routine(s, routine_id: int, user: User) -> Routine:
    r = s.get(Routine, routine_id)
    _need(r and r.owner_id == user.id, 404, "routine not found")
    return r


@router.get("/routines")
def list_all_routines(user: User = Depends(current_user)):
    """Every routine the caller owns, across projects, with next/last run in the server timezone."""
    with session() as s:
        rows = s.exec(select(Routine).where(Routine.owner_id == user.id)).all()
        views = [routines.view(s, r) for r in rows]
    views.sort(key=lambda v: (not v["enabled"], v["next_run"] or "z", v["name"].lower(), v["id"]))
    return views


@router.get("/bots/{bot_id}/routines")
def list_routines(bot_id: int, user: User = Depends(current_user)):
    with session() as s:
        _bot(s, bot_id, user)
        rows = s.exec(select(Routine).where(Routine.bot_id == bot_id, Routine.owner_id == user.id)).all()
        views = [routines.view(s, r) for r in rows]
    views.sort(key=lambda v: (not v["enabled"], v["next_run"] or "z", v["name"].lower(), v["id"]))
    return views


@router.post("/bots/{bot_id}/routines")
async def add_routine(bot_id: int, body: RoutineIn, user: User = Depends(current_user)):
    try:
        routines.cron_trigger(body.cron)
    except ValueError as e:
        raise HTTPException(400, f"invalid cron expression: {e}") from e
    with session() as s:
        _bot(s, bot_id, user)
        conv = s.get(Conversation, body.conversation_id)
        _need(conv and can_read_conversation(s, user.id, conv), 404, "conversation not found")
        r = Routine(bot_id=bot_id, owner_id=user.id, **body.model_dump())
        s.add(r)
        s.commit()
    routines.sync(r)
    return r.model_dump(mode="json")


@router.patch("/routines/{routine_id}")
async def patch_routine(routine_id: int, body: RoutinePatch, user: User = Depends(current_user)):
    data = body.model_dump(exclude_unset=True)
    if "cron" in data:
        try:
            routines.cron_trigger(data["cron"])
        except ValueError as e:
            raise HTTPException(400, f"invalid cron expression: {e}") from e
    with session() as s:
        r = _routine(s, routine_id, user)
        for key, value in data.items():
            setattr(r, key, value)
        s.add(r)
        s.commit()
        out = routines.view(s, r)
    routines.sync(r)
    return out


@router.delete("/routines/{routine_id}")
async def delete_routine(routine_id: int, user: User = Depends(current_user)):
    with session() as s:
        r = _routine(s, routine_id, user)
        s.delete(r)
        s.commit()
    routines.remove(routine_id)
    return {"ok": True}


@router.post("/routines/{routine_id}/run")
async def run_routine(routine_id: int, user: User = Depends(current_user)):
    with session() as s:
        _routine(s, routine_id, user)
    agent.spawn(routines.fire(routine_id, force=True))
    return {"ok": True}


# ---------------------------------------------------------------- conversations

@router.post("/bots/{bot_id}/dm")
def open_dm(bot_id: int, user: User = Depends(current_user)):
    """Get or create the private chat between this person and this bot."""
    with session() as s:
        b = _bot(s, bot_id, user)
        c = s.exec(select(Conversation).where(Conversation.kind == "dm", Conversation.bot_id == bot_id,
                                              Conversation.user_id == user.id)).first()
        if not c:
            c = Conversation(team_id=b.team_id, kind="dm", name=b.name, user_id=user.id, bot_id=bot_id)
            s.add(c)
            s.commit()
    return c.model_dump()


@router.get("/bots/{bot_id}/feed")
def open_feed(bot_id: int, user: User = Depends(current_user)):
    """The Team Bot's shared activity feed: short summaries of finished tasks, for everyone with access."""
    with session() as s:
        b = _bot(s, bot_id, user)
        _need(b.is_team, 400, "only a Team Bot has an activity feed")
    return agent.feed_conversation(b).model_dump()


class ConversationPatch(BaseModel):
    share_activity: Optional[bool] = None
    name: Optional[str] = None


@router.patch("/conversations/{conv_id}")
def patch_conversation(conv_id: int, body: ConversationPatch, user: User = Depends(current_user)):
    """A person decides per private chat whether summaries of it go to the shared feed.
    A channel can be renamed by its members."""
    with session() as s:
        c = s.get(Conversation, conv_id)
        _need(c and can_read_conversation(s, user.id, c), 404, "conversation not found")
        if body.share_activity is not None:
            _need(c.kind == "dm", 400, "only a private chat has this setting")
            c.share_activity = body.share_activity
        if body.name is not None:
            _need(c.kind == "channel", 400, "only a channel can be renamed here")
            _need(body.name.strip(), 400, "the channel needs a name")
            c.name = body.name.strip()
        s.add(c)
        s.commit()
    return c.model_dump()


def _screen_bot(bot_id: int, user: User) -> Bot:
    with session() as s:
        b = _bot(s, bot_id, user)
        _need(b.computer_use, 400, "computer use is off for this bot")
        s.expunge(b)
        return b


@router.get("/bots/{bot_id}/screen.jpg")
async def bot_screen(bot_id: int, user: User = Depends(current_user)):
    """Live view of a bot's screen, for everyone in the project who can use the bot."""
    b = _screen_bot(bot_id, user)
    try:
        data = await screen.screenshot(home.sandbox_key(b), b.id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"screen not available: {e}") from e
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


def _cookie_flags(request: Request | None) -> dict:
    """Secure cookies when the original request was HTTPS, including behind a reverse proxy."""
    scheme = ""
    if request is not None:
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        scheme = proto or request.url.scheme
    return {"httponly": True, "samesite": "lax", "secure": scheme == "https"}


def _vnc_cookie(response: Response, token: str, request: Request | None = None) -> None:
    # Lets noVNC's own JS/CSS and the websocket reuse the session without putting the token in every URL.
    if token:
        response.set_cookie("tb_token", token, path="/api/bots/", max_age=60 * 60 * 24 * 30,
                            **_cookie_flags(request))


@router.get("/bots/{bot_id}/screen/vnc")
@router.get("/bots/{bot_id}/screen/vnc/")
async def vnc_index(bot_id: int, request: Request, user: User = Depends(current_user),
                    authorization: str = Header(default=""), token: str = Query(default=""),
                    tb_token: str = Cookie(default="")):
    """Open the interactive screen (noVNC) behind TeamBots auth; no extra LAN port."""
    b = _screen_bot(bot_id, user)
    try:
        await screen.ensure_vnc(home.sandbox_key(b), b.id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"interactive screen not available: {e}") from e
    dest = f"/api/bots/{bot_id}/screen/vnc/vnc.html?autoconnect=1&resize=scale&reconnect=1&path=ws"
    resp = RedirectResponse(dest, status_code=302)
    _vnc_cookie(resp, authorization.removeprefix("Bearer ").strip() or token or tb_token, request)
    return resp


@router.websocket("/bots/{bot_id}/screen/vnc/ws")
async def vnc_ws(ws: WebSocket, bot_id: int, token: str = Query(default="")):
    """Authenticated proxy from the browser to websockify inside the sandbox."""
    tok = token or ws.cookies.get("tb_token") or ""
    user = _user_for_token(tok)
    if not user:
        await ws.close(code=4401, reason="not signed in")
        return
    try:
        b = _screen_bot(bot_id, user)
        host, port = await screen.ensure_vnc(home.sandbox_key(b), b.id)
    except HTTPException as e:
        await ws.close(code=4400 + (e.status_code % 100), reason=e.detail[:80])
        return
    except Exception as e:  # noqa: BLE001
        await ws.close(code=1011, reason=str(e)[:80])
        return
    requested = ws.headers.get("sec-websocket-protocol", "")
    sub = "binary" if "binary" in requested else ("base64" if "base64" in requested else None)
    await ws.accept(subprotocol=sub)
    upstream = f"ws://{host}:{port}/"
    try:
        async with websockets.connect(upstream, subprotocols=[sub] if sub else None, open_timeout=10) as up:
            async def down():
                try:
                    while True:
                        msg = await ws.receive()
                        if msg["type"] == "websocket.disconnect":
                            return
                        data = msg.get("bytes") if msg.get("bytes") is not None else msg.get("text")
                        if data is not None:
                            await up.send(data)
                except (WebSocketDisconnect, Exception):
                    return

            async def up_to_client():
                try:
                    async for msg in up:
                        if isinstance(msg, (bytes, bytearray)):
                            await ws.send_bytes(msg)
                        else:
                            await ws.send_text(msg)
                except Exception:
                    return

            await asyncio.gather(down(), up_to_client())
    except Exception as e:  # noqa: BLE001
        try:
            await ws.close(code=1011, reason=f"vnc upstream: {e}"[:80])
        except Exception:
            return


@router.api_route("/bots/{bot_id}/screen/vnc/{path:path}", methods=["GET"])
async def vnc_http(bot_id: int, path: str, user: User = Depends(current_user)):
    """Proxy noVNC static files from websockify in the sandbox (same auth as the rest of the API)."""
    if path == "ws":
        raise HTTPException(404, "use the websocket")
    b = _screen_bot(bot_id, user)
    try:
        host, port = await screen.ensure_vnc(home.sandbox_key(b), b.id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"interactive screen not available: {e}") from e
    url = f"http://{host}:{port}/{path}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        raise HTTPException(503, f"interactive screen not available: {e}") from e
    media = r.headers.get("content-type", "application/octet-stream")
    return Response(r.content, status_code=r.status_code, media_type=media,
                    headers={"Cache-Control": "no-store"})


@router.get("/messages/{message_id}/image")
def message_image(message_id: int, user: User = Depends(current_user)):
    with session() as s:
        m = s.get(Message, message_id)
        _need(m and m.image_path, 404, "no image")
        _conv(s, m.conversation_id, user)
    return FileResponse(m.image_path, media_type="image/jpeg")


class ChannelIn(BaseModel):
    name: str
    user_ids: list[int] = []
    bot_ids: list[int] = []


def _add_bot_to_team_channels(s, bot: Bot) -> None:
    """Add a newly created bot to every existing channel in its project. Not used on startup."""
    if bot.is_home or not bot.team_id:
        return
    for ch in s.exec(select(Conversation).where(Conversation.team_id == bot.team_id,
                                                Conversation.kind == "channel")).all():
        if not s.exec(select(ChannelMember).where(ChannelMember.conversation_id == ch.id,
                                                  ChannelMember.bot_id == bot.id)).first():
            s.add(ChannelMember(conversation_id=ch.id, bot_id=bot.id, mention_only=False))
    s.commit()


def _add_members(s, conv: Conversation, user: User, user_ids: list[int], bot_ids: list[int],
                 *, all_team_bots: bool = False) -> None:
    have_u = {m.user_id for m in s.exec(select(ChannelMember).where(ChannelMember.conversation_id == conv.id)).all()}
    have_b = {m.bot_id for m in s.exec(select(ChannelMember).where(ChannelMember.conversation_id == conv.id)).all()}
    for uid in set(user_ids) - have_u:
        if team_role(s, uid, conv.team_id):
            s.add(ChannelMember(conversation_id=conv.id, user_id=uid))
    if all_team_bots:
        bot_ids = list(bot_ids) + [b.id for b in s.exec(select(Bot).where(Bot.team_id == conv.team_id, Bot.is_home == False)).all()]  # noqa: E712
    for bid in set(bot_ids) - have_b:
        b = s.get(Bot, bid)
        # Auto-adding every project bot (channel create) skips the manage check; a manual add still needs it.
        if b and b.team_id == conv.team_id and (all_team_bots or can_manage(s, user.id, b)):
            s.add(ChannelMember(conversation_id=conv.id, bot_id=bid, mention_only=False))
    s.commit()


@router.get("/teams/{team_id}/channels")
def list_channels(team_id: int, user: User = Depends(current_user)):
    with session() as s:
        _need(team_role(s, user.id, team_id))
        rows = s.exec(select(Conversation).where(Conversation.team_id == team_id,
                                                 Conversation.kind == "channel")).all()
        return [c.model_dump() for c in rows if can_read_conversation(s, user.id, c)]


@router.post("/teams/{team_id}/channels")
def create_channel(team_id: int, body: ChannelIn, user: User = Depends(current_user)):
    with session() as s:
        _need(team_role(s, user.id, team_id))
        c = Conversation(team_id=team_id, kind="channel", name=body.name)
        s.add(c)
        s.commit()
        _add_members(s, c, user, [user.id, *body.user_ids], body.bot_ids, all_team_bots=True)
    return c.model_dump()


class MembersIn(BaseModel):
    user_ids: list[int] = []
    bot_ids: list[int] = []


def _conv(s, conv_id: int, user: User) -> Conversation:
    c = s.get(Conversation, conv_id)
    _need(c and can_read_conversation(s, user.id, c), 404, "conversation not found")
    return c


@router.post("/channels/{conv_id}/members")
def add_channel_members(conv_id: int, body: MembersIn, user: User = Depends(current_user)):
    with session() as s:
        c = _conv(s, conv_id, user)
        _need(c.kind == "channel", 400, "not a channel")
        _add_members(s, c, user, body.user_ids, body.bot_ids)
    return {"ok": True}


@router.get("/channels/{conv_id}/members")
def channel_members(conv_id: int, user: User = Depends(current_user)):
    with session() as s:
        _conv(s, conv_id, user)
        rows = s.exec(select(ChannelMember).where(ChannelMember.conversation_id == conv_id)).all()
        return {"users": [{"id": m.user_id, "display_name": s.get(User, m.user_id).display_name}
                          for m in rows if m.user_id],
                "bots": [{"id": m.bot_id, "handle": s.get(Bot, m.bot_id).handle, "name": s.get(Bot, m.bot_id).name,
                           "mention_only": bool(m.mention_only)}
                         for m in rows if m.bot_id and s.get(Bot, m.bot_id)]}


class BotMemberPatch(BaseModel):
    mention_only: bool


def _channel_bot_member(s, conv_id: int, bot_id: int, user: User) -> ChannelMember:
    c = _conv(s, conv_id, user)
    _need(c.kind == "channel", 400, "not a channel")
    m = s.exec(select(ChannelMember).where(ChannelMember.conversation_id == conv_id,
                                           ChannelMember.bot_id == bot_id)).first()
    _need(m, 404, "bot is not in this channel")
    bot = s.get(Bot, bot_id)
    _need(bot and (can_manage(s, user.id, bot) or team_role(s, user.id, c.team_id) in ("admin", "owner")),
          403, "not allowed")
    return m


@router.patch("/channels/{conv_id}/members/bots/{bot_id}")
def patch_channel_bot(conv_id: int, bot_id: int, body: BotMemberPatch, user: User = Depends(current_user)):
    """Set whether a bot replies to every message or only when @mentioned."""
    with session() as s:
        m = _channel_bot_member(s, conv_id, bot_id, user)
        m.mention_only = body.mention_only
        s.add(m)
        s.commit()
    return {"ok": True, "mention_only": body.mention_only}


@router.delete("/channels/{conv_id}/members/bots/{bot_id}")
def remove_channel_bot(conv_id: int, bot_id: int, user: User = Depends(current_user)):
    """Remove a bot from a channel. It is not added back on restart."""
    with session() as s:
        m = _channel_bot_member(s, conv_id, bot_id, user)
        s.delete(m)
        s.commit()
    return {"ok": True}


@router.get("/conversations/{conv_id}/messages")
def list_messages(conv_id: int, after: int = 0, user: User = Depends(current_user)):
    with session() as s:
        _conv(s, conv_id, user)
        rows = s.exec(select(Message).where(Message.conversation_id == conv_id, Message.id > after)
                      .order_by(Message.id)).all()
        users = {u.id: u.display_name for u in s.exec(select(User)).all()}
        bots = {b.id: b.name for b in s.exec(select(Bot)).all()}
    return [{"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at.isoformat(),
             "author": users.get(m.author_user_id) if m.role == "user" else bots.get(m.author_bot_id, "bot"),
             "author_user_id": m.author_user_id, "author_bot_id": m.author_bot_id,
             "is_bot": m.role != "user", "tool_calls": json.loads(m.tool_calls) if m.tool_calls else [],
             "tool_call_id": m.tool_call_id, "has_image": bool(m.image_path)} for m in rows]


class MessageIn(BaseModel):
    content: str
    mode: str = "auto"  # what to do when the bot is busy: auto | steer | queue | interrupt
    allow_secret: bool = False  # the person looked at the warning and sends it anyway


@router.post("/conversations/{conv_id}/messages")
async def send_message(conv_id: int, body: MessageIn, user: User = Depends(current_user)):
    _need(body.content.strip(), 400, "empty message")
    _need(body.mode in agent.MODES, 400, "mode: " + " | ".join(agent.MODES))
    with session() as s:
        conv = _conv(s, conv_id, user)
        _need(conv.kind != "feed", 400, "the activity feed is written by the bot only")
        # Only someone who manages a bot may learn that a typed value equals one of its secrets.
        bot_ids = [conv.bot_id] if conv.kind in ("dm", "home") else [m.bot_id for m in s.exec(select(ChannelMember).where(
            ChannelMember.conversation_id == conv_id, ChannelMember.bot_id != None)).all()]  # noqa: E711
        managed = [i for i in bot_ids if (b := s.get(Bot, i)) and can_manage(s, user.id, b)]
    if not body.allow_secret:
        found = secrets_http.find_leaks(body.content, managed)
        if found:  # nothing is stored; the person decides what to do with it
            raise HTTPException(409, {"code": "secret", "found": found,
                                      "message": "this message seems to contain a secret"})
    m = agent.handle_user_message(conv_id, user.id, body.content.strip(), body.mode)
    return {"id": m.id}


@router.get("/conversations/{conv_id}/state")
def conversation_state(conv_id: int, user: User = Depends(current_user)):
    """Which bots are working right now, and what happened to messages sent meanwhile."""
    with session() as s:
        _conv(s, conv_id, user)
        st = agent.state(conv_id)
        for r in st["running"]:
            bot = s.get(Bot, r["bot_id"])
            r.update(bot=bot.name if bot else "bot", mine=r.pop("requester_id") == user.id)
    return st


@router.post("/conversations/{conv_id}/stop")
async def stop_bots(conv_id: int, user: User = Depends(current_user)):
    """The stop button: ends the turns that work for this person in this conversation."""
    with session() as s:
        _conv(s, conv_id, user)
    return {"stopped": agent.stop(conv_id, user.id)}


@router.get("/conversations/{conv_id}/approvals")
def list_approvals(conv_id: int, user: User = Depends(current_user)):
    with session() as s:
        _conv(s, conv_id, user)
        rows = s.exec(select(Approval).where(Approval.conversation_id == conv_id,
                                             Approval.status == "pending")).all()
        return [{"id": a.id, "tool_name": a.tool_name, "arguments": json.loads(a.arguments),
                 "tool_call_id": a.tool_call_id,
                 "bot": s.get(Bot, a.bot_id).name, "requester": s.get(User, a.requester_id).display_name,
                 "mine": a.requester_id == user.id} for a in rows]


class DecisionIn(BaseModel):
    allow: bool
    always: bool = False


@router.post("/approvals/{approval_id}")
async def decide(approval_id: int, body: DecisionIn, user: User = Depends(current_user)):
    try:
        with session() as s:
            a = s.get(Approval, approval_id)
            _need(a and a.status == "pending", 404, "approval not found or already answered")
            _need(a.requester_id == user.id, 403, "only the person who asked can approve this action")
        agent.spawn(agent.resolve_approval(approval_id, user.id, body.allow, body.always))
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    return {"ok": True}


@router.get("/conversations/{conv_id}/events")
async def stream_events(conv_id: int, user: User = Depends(current_user)):
    with session() as s:
        _conv(s, conv_id, user)

    async def gen():
        q = events.subscribe(conv_id)
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 20)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            events.unsubscribe(conv_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
