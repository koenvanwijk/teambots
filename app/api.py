"""REST API. Every route checks permissions through app.access."""
import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel
import httpx
import websockets

from . import agent, coding, config, copilot, events, home, llm, mcp_client, oauth, ratelimit, routines, sandbox, screen, secrets_http
from .access import bot_role, can_manage, can_read_conversation, can_share, team_role
from .db import (Approval, ApprovalRule, AuthToken, Bot, BotAccess, ChannelMember, Conversation, Endpoint, Identity,
                 LlmUsage, McpCredential, McpServer, Memory, Message, OAuthState, Routine, Secret, Skill, Team,
                 TeamMember, User, now, select, session)

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


def _bot(s, bot_id: int, user: User, manage: bool = False, share: bool = False) -> Bot:
    bot = s.get(Bot, bot_id)
    _need(bot and bot_role(s, user.id, bot), 404, "bot not found")
    if manage:
        _need(can_manage(s, user.id, bot))
    if share:
        _need(can_share(s, user.id, bot), 403, "only the owner or a manager of this bot can change who may use it")
    return bot


def _bot_out(s, bot: Bot, user: User) -> dict:
    return {**bot.model_dump(), "role": bot_role(s, user.id, bot)}


# ---------------------------------------------------------------- auth and teams

class Credentials(BaseModel):
    username: str
    password: str
    display_name: str = ""
    invite_code: str = ""


def _auth_rate_limit(request: Request) -> None:
    if config.AUTH_RATE_LIMIT <= 0:
        return
    ip = ratelimit.client_ip(request)
    if not ratelimit.AUTH_LIMITER.allow(ip, config.AUTH_RATE_LIMIT):
        retry = ratelimit.AUTH_LIMITER.retry_after(ip)
        raise HTTPException(429, "too many attempts, try again later",
                            headers={"Retry-After": str(retry)})


def _password_login(body: Credentials) -> dict:
    with session() as s:
        u = s.exec(select(User).where(User.username == body.username)).first()
        salt, _, digest = (u.password_hash if u else "x$x").partition("$")
        _need(u and secrets.compare_digest(_hash(body.password, salt), digest), 401, "wrong username or password")
        tok = secrets.token_urlsafe(32)
        s.add(AuthToken(token=tok, user_id=u.id))
        s.commit()
    return {"token": tok, "user": {"id": u.id, "username": u.username, "display_name": u.display_name}}


@router.post("/register")
def register(body: Credentials, request: Request):
    _auth_rate_limit(request)
    _need(re.fullmatch(r"[a-z0-9_.-]{2,32}", body.username), 400, "username: 2-32 chars of a-z 0-9 _ . -")
    _need(len(body.password) >= 8, 400, "password needs at least 8 characters")
    invite = (body.invite_code or "").strip()
    with session() as s:
        first_user = s.exec(select(User).limit(1)).first() is None
        team = s.exec(select(Team).where(Team.invite_code == invite)).first() if invite else None
        if not first_user and not config.OPEN_REGISTRATION:
            _need(team, 403, "You need a valid invite code to create an account.")
        _need(not s.exec(select(User).where(User.username == body.username)).first(), 409, "username is taken")
        salt = secrets.token_hex(8)
        u = User(username=body.username, display_name=body.display_name or body.username,
                 password_hash=f"{salt}${_hash(body.password, salt)}")
        s.add(u)
        s.commit()
        s.refresh(u)
        # Same as the OAuth invite flow: a valid project invite also joins the team.
        _maybe_join(s, u.id, invite)
        s.commit()
    return _password_login(body)


@router.post("/login")
def login(body: Credentials, request: Request):
    _auth_rate_limit(request)
    return _password_login(body)


@router.get("/me")
def me(user: User = Depends(current_user)):
    from . import usage
    with session() as s:
        can_costs = usage.user_can_view_costs(s, user.id)
    return {"id": user.id, "username": user.username, "display_name": user.display_name,
            "can_view_costs": can_costs}


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
                d["monthly_budget"] = getattr(t, "monthly_budget", None)
                d["budget_currency"] = getattr(t, "budget_currency", None) or "USD"
                d["budget_block"] = bool(getattr(t, "budget_block", False))
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
            feed = s.exec(select(Conversation).where(
                Conversation.team_id == t.id, Conversation.kind == "feed", Conversation.bot_id.is_(None))).first()
            recent = s.exec(select(Message).where(Message.conversation_id == (feed.id if feed else 0))
                            .order_by(Message.id.desc()).limit(5)).all()
            names = {b.id: b.name for b in s.exec(select(Bot).where(Bot.team_id == t.id)).all()}
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
async def team_files(team_id: int, detail: bool = False, user: User = Depends(current_user)):
    """Files on the project computer: paths for the '@' menu, or with detail=1 also size and date.
    Empty when the computer cannot be reached."""
    with session() as s:
        _need(team_role(s, user.id, team_id))
    try:
        return await (sandbox.file_info(f"p{team_id}") if detail else sandbox.list_files(f"p{team_id}"))
    except Exception:  # noqa: BLE001 - the menu simply shows no files
        return []


UPLOAD_DIR = "uploads"
SAFE_INLINE = {"image/png", "image/jpeg", "image/gif", "image/webp"}  # never html or svg: same origin as the app


def _file_name(name: str) -> str:
    """A plain file name from whatever the browser sent: no folders, no control or shell-awkward characters."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip(" .")
    return (name or "file")[-120:]


def _folder(folder: str) -> str:
    folder = folder.strip().strip("/")
    if folder:
        sandbox._check_path(folder)
        _need(not any(p.startswith(".") for p in folder.split("/")), 400, "hidden folders are not allowed")
    return folder


@router.post("/teams/{team_id}/files")
async def upload_file(team_id: int, request: Request, name: str, folder: str = UPLOAD_DIR,
                      allow_secret: bool = False, user: User = Depends(current_user)):
    """Store a file on the project computer, where every person and bot of the project can use it.
    The body is the raw file. A file with the same name gets a number, nothing is overwritten."""
    with session() as s:
        _need(team_role(s, user.id, team_id))
    try:
        folder = _folder(folder)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    _need(int(request.headers.get("content-length") or 0) <= limit, 413, f"files can be at most {config.MAX_UPLOAD_MB} MB")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        _need(size <= limit, 413, f"files can be at most {config.MAX_UPLOAD_MB} MB")
        chunks.append(chunk)
    data = b"".join(chunks)
    # Secrets stay out of the project computer too: a text file with a credential in it is held back.
    if not allow_secret and size <= 2 * 1024 * 1024 and b"\x00" not in data[:8192]:
        found = secrets_http.find_leaks(data.decode("utf-8", errors="ignore"), [])
        if found:
            raise HTTPException(409, {"code": "secret", "found": found,
                                      "message": "this file seems to contain a secret"})
    key, base = f"p{team_id}", _file_name(name)
    try:
        taken = {f["path"] for f in await sandbox.file_info(key, limit=100000)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"the project computer is not available: {e}") from e
    stem, dot, ext = base.rpartition(".") if "." in base.lstrip(".") else (base, "", "")
    path, n = f"{folder}/{base}" if folder else base, 1
    while path in taken:
        n += 1
        path = f"{folder + '/' if folder else ''}{stem} ({n}){dot}{ext}"
    try:
        await sandbox.put_bytes(key, path, data)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"the project computer is not available: {e}") from e
    return {"path": path, "size": size}


@router.get("/teams/{team_id}/files/content")
async def download_file(team_id: int, path: str, download: bool = True, user: User = Depends(current_user)):
    with session() as s:
        _need(team_role(s, user.id, team_id))
    try:
        data = await sandbox.get_bytes(f"p{team_id}", path)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _need(data is not None, 404, "file not found")
    media = mimetypes.guess_type(path)[0] or "application/octet-stream"
    inline = not download and media in SAFE_INLINE
    name = path.rsplit("/", 1)[-1]
    # Only plain images are shown in the page; everything else is a download, so an uploaded html or
    # svg file can never run as part of the app.
    return Response(data, media_type=media if media in SAFE_INLINE else "application/octet-stream",
                    headers={"Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name)}",
                             "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox",
                             "Cache-Control": "private, no-store"})


@router.delete("/teams/{team_id}/files")
async def delete_file(team_id: int, path: str, user: User = Depends(current_user)):
    """Every project member can remove a file: the workspace is shared, as it is for the bots."""
    with session() as s:
        _need(team_role(s, user.id, team_id))
    try:
        _need(await sandbox.remove(f"p{team_id}", path), 404, "file not found")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@router.get("/teams/{team_id}/backups")
def list_team_backups(team_id: int, user: User = Depends(current_user)):
    """Per-project export archives (made by scripts/export_project.py). Project admins only."""
    with session() as s:
        _need(team_role(s, user.id, team_id) == "admin", 403, "project admins only")
    prefix, out = f"teambots-project-p{team_id}-", []
    d = config.EXPORTS_DIR
    if os.path.isdir(d):
        for name in os.listdir(d):
            if name.startswith(prefix) and name.endswith(".tgz"):
                try:
                    st = os.stat(os.path.join(d, name))
                except OSError:
                    continue
                out.append({"name": name, "size": st.st_size,
                            "created_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()})
    out.sort(key=lambda r: r["name"], reverse=True)
    return out


@router.get("/teams/{team_id}/backups/{name}")
def download_team_backup(team_id: int, name: str, user: User = Depends(current_user)):
    """Serve one of this project's export archives. Admins only; the name is validated strictly."""
    with session() as s:
        _need(team_role(s, user.id, team_id) == "admin", 403, "project admins only")
    prefix = f"teambots-project-p{team_id}-"
    _need("/" not in name and "\\" not in name and ".." not in name
          and name.startswith(prefix) and name.endswith(".tgz"), 404, "not found")
    path = os.path.join(config.EXPORTS_DIR, name)
    _need(os.path.isfile(path), 404, "not found")
    return FileResponse(path, media_type="application/gzip", filename=name)


class TeamPatch(BaseModel):
    bot_sharing: Optional[str] = None
    name: Optional[str] = None
    client: Optional[str] = None
    monthly_budget: Optional[float] = None
    budget_currency: Optional[str] = None
    budget_block: Optional[bool] = None


@router.patch("/teams/{team_id}")
def patch_team(team_id: int, body: TeamPatch, user: User = Depends(current_user)):
    _need(body.bot_sharing in (None, "everyone", "admins"), 400, "bot_sharing: everyone | admins")
    _need(body.name is None or body.name.strip(), 400, "the project needs a name")
    data = body.model_dump(exclude_unset=True)
    if "budget_currency" in data:
        _need(data["budget_currency"] in ("USD", "EUR"), 400, "budget_currency: USD | EUR")
    if "monthly_budget" in data and data["monthly_budget"] is not None:
        _need(data["monthly_budget"] >= 0, 400, "monthly_budget must be 0 or more")
    with session() as s:
        _need(team_role(s, user.id, team_id) == "admin")
        t = s.get(Team, team_id)
        if body.bot_sharing is not None:
            t.bot_sharing = body.bot_sharing
        if body.name is not None:
            t.name = body.name.strip()
        if body.client is not None:
            t.client = body.client.strip()
        if "monthly_budget" in data:
            t.monthly_budget = data["monthly_budget"]
        if "budget_currency" in data:
            t.budget_currency = data["budget_currency"]
        if "budget_block" in data:
            t.budget_block = bool(data["budget_block"])
        s.add(t)
        s.commit()
    return {"ok": True}


@router.delete("/teams/{team_id}")
def delete_team(team_id: int, confirm: str = Query(default=""), user: User = Depends(current_user)):
    """Delete a project and everything in it (bots, chats, messages, memory, secrets, MCP, usage)
    plus its workspace. Admin only; `confirm` must equal the project name. A full export is written
    first as a safety net; if that export fails, nothing is deleted."""
    with session() as s:
        _need(team_role(s, user.id, team_id) == "admin", 403, "only a project admin can delete the project")
        team = s.get(Team, team_id)
        _need(team, 404, "no such project")
        _need(confirm == team.name, 400, "type the project name exactly to confirm")
        name = team.name
    # 1) Safety net: export first; abort the whole delete if it fails.
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "export_project.py")
    try:
        subprocess.run([sys.executable, script, str(team_id), "--out", config.EXPORTS_DIR],
                       check=True, capture_output=True, timeout=180)
    except Exception as e:  # noqa: BLE001
        detail = getattr(e, "stderr", b"") or b""
        raise HTTPException(500, "backup before delete failed, nothing was deleted: "
                            + (detail.decode(errors="replace")[-200:] if detail else str(e)))
    # 2) Delete every row scoped to the project.
    with session() as s:
        bot_ids = [b.id for b in s.exec(select(Bot).where(Bot.team_id == team_id)).all()]
        conv_ids = [c.id for c in s.exec(select(Conversation).where(Conversation.team_id == team_id)).all()]
        mcp_ids = [m.id for m in s.exec(select(McpServer).where(McpServer.bot_id.in_(bot_ids))).all()] if bot_ids else []
        for r in (s.exec(select(Routine).where(Routine.bot_id.in_(bot_ids))).all() if bot_ids else []):
            routines.remove(r.id)

        def wipe(model, cond):
            for row in s.exec(select(model).where(cond)).all():
                s.delete(row)
        if mcp_ids:
            wipe(McpCredential, McpCredential.mcp_server_id.in_(mcp_ids))
        if conv_ids:
            wipe(Message, Message.conversation_id.in_(conv_ids))
            wipe(ChannelMember, ChannelMember.conversation_id.in_(conv_ids))
            wipe(Approval, Approval.conversation_id.in_(conv_ids))
        if bot_ids:
            wipe(Approval, Approval.bot_id.in_(bot_ids))
            wipe(ApprovalRule, ApprovalRule.bot_id.in_(bot_ids))
            wipe(Memory, Memory.bot_id.in_(bot_ids))
            wipe(Skill, Skill.bot_id.in_(bot_ids))
            wipe(Routine, Routine.bot_id.in_(bot_ids))
            wipe(BotAccess, BotAccess.bot_id.in_(bot_ids))
            wipe(Secret, Secret.bot_id.in_(bot_ids))
            wipe(McpServer, McpServer.bot_id.in_(bot_ids))
        wipe(Memory, Memory.team_id == team_id)
        wipe(Conversation, Conversation.team_id == team_id)
        wipe(Bot, Bot.team_id == team_id)
        wipe(Endpoint, Endpoint.team_id == team_id)
        wipe(TeamMember, TeamMember.team_id == team_id)
        wipe(LlmUsage, LlmUsage.team_id == team_id)
        s.delete(s.get(Team, team_id))
        s.commit()
    # 3) Remove the project's workspace: the Docker container and volume, and any local dir.
    sandbox.destroy(f"p{team_id}")
    from . import knowledge
    knowledge.remove_all(knowledge.key_for_team(team_id))  # the project's knowledge base and its history
    return {"deleted": name}


# ---------------------------------------------------------------- endpoints

class EndpointIn(BaseModel):
    name: str
    provider: str = "openai"  # openai | github_copilot | claude_cli | router
    base_url: str = ""
    github_domain: str = ""
    api_key: str = ""
    device_login: str = ""  # login_id of a finished Copilot device login, used instead of api_key
    default_model: str
    shared: bool = False
    rules_json: str = ""



def _rules_out(e: Endpoint):
    raw = getattr(e, "rules_json", "") or ""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _ep_out(e: Endpoint) -> dict:  # the key never leaves the server
    return {"id": e.id, "name": e.name, "base_url": e.base_url, "default_model": e.default_model,
            "shared": e.shared, "owner_id": e.owner_id, "has_key": bool(e.api_key_enc),
            "provider": getattr(e, "provider", None) or "openai",
            "github_domain": getattr(e, "github_domain", None) or "",
            "models": __import__("app.usage", fromlist=["cached_models"]).cached_models(e),
            "models_fetched_at": e.models_fetched_at.isoformat() if getattr(e, "models_fetched_at", None) else None,
            "rules": _rules_out(e)}


def _normalize_provider(value: str) -> str:
    provider = (value or "openai").strip()
    _need(provider in ("openai", "github_copilot", "claude_cli", "router"), 400,
          "provider: openai | github_copilot | claude_cli | router")
    return provider


def _github_domain_or_400(raw: str) -> str:
    try:
        return copilot.normalize_domain(raw)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# Copilot device logins in progress: login_id -> user, domain, device_code, expiry and, once the
# person approved, the GitHub token. In memory only; the token leaves it when an endpoint uses it.
_device_logins: dict[str, dict] = {}


class DeviceStartIn(BaseModel):
    github_domain: str = "github.com"


class DevicePollIn(BaseModel):
    login_id: str


def _device_login_or_404(login_id: str, user: User) -> dict:
    now = datetime.now(timezone.utc).timestamp()
    for k in [k for k, v in _device_logins.items() if v["expires"] < now]:
        del _device_logins[k]
    row = _device_logins.get(login_id)
    _need(row and row["user_id"] == user.id, 404, "device login not found or expired")
    return row


def _take_device_token(login_id: str, user: User, domain: str) -> str:
    """The GitHub token of a finished device login, for one endpoint only."""
    try:
        row = _device_login_or_404(login_id, user)
    except HTTPException:
        raise HTTPException(400, "device login not found or expired; sign in again") from None
    _need(row.get("token"), 400, "the device login is not finished yet")
    _need(row["domain"] == domain, 400, "the device login was for another GitHub domain")
    del _device_logins[login_id]
    return row["token"]


@router.post("/copilot/device/start")
async def copilot_device_start(body: DeviceStartIn, user: User = Depends(current_user)):
    """Start a GitHub device login for a Copilot endpoint. The person opens verification_uri and
    enters user_code; the browser then polls. The device code and the token stay on the server."""
    domain = _github_domain_or_400(body.github_domain)
    try:
        flow = await copilot.device_start(domain)
    except llm.LLMError as e:
        raise HTTPException(502, str(e)) from e
    login_id = secrets.token_urlsafe(24)
    _device_logins[login_id] = {"user_id": user.id, "domain": domain, "device_code": flow["device_code"],
                                "expires": datetime.now(timezone.utc).timestamp() + flow["expires_in"]}
    return {"login_id": login_id, "user_code": flow["user_code"], "verification_uri": flow["verification_uri"],
            "interval": flow["interval"], "expires_in": flow["expires_in"]}


@router.post("/copilot/device/poll")
async def copilot_device_poll(body: DevicePollIn, user: User = Depends(current_user)):
    row = _device_login_or_404(body.login_id, user)
    if row.get("token"):
        return {"status": "done"}
    try:
        result = await copilot.device_poll(row["domain"], row["device_code"])
    except llm.LLMError as e:
        raise HTTPException(502, str(e)) from e
    if result["status"] == "done":
        row["token"] = result["token"]
    elif result["status"] in ("expired", "denied"):
        _device_logins.pop(body.login_id, None)
    return {"status": result["status"]}


@router.get("/teams/{team_id}/endpoints")
def list_endpoints(team_id: int, user: User = Depends(current_user)):
    with session() as s:
        _need(team_role(s, user.id, team_id))
        rows = s.exec(select(Endpoint).where(Endpoint.team_id == team_id)).all()
    return [_ep_out(e) for e in rows if e.shared or e.owner_id == user.id]


@router.post("/teams/{team_id}/endpoints")
def create_endpoint(team_id: int, body: EndpointIn, user: User = Depends(current_user)):
    _need(body.name.strip(), 400, "name cannot be empty")
    if (body.provider or "openai") != "router":
        _need(body.default_model.strip(), 400, "name and default model cannot be empty")
    elif not body.default_model.strip():
        body = body.model_copy(update={"default_model": "auto"})
    provider = _normalize_provider(body.provider)
    if provider == "claude_cli":
        # No HTTP base URL and no API key (the CLI uses its own stored credentials).
        fields = dict(provider=provider, github_domain="", base_url="", api_key_enc="", rules_json="")
    elif provider == "router":
        from . import llm_router
        raw_rules = (body.rules_json or "").strip()
        if raw_rules:
            try:
                rules = llm_router.validate_rules(raw_rules, team_id=team_id)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        else:
            try:
                rules = llm_router.default_rules_for_team(team_id)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        fields = dict(provider=provider, github_domain="", base_url="", api_key_enc="",
                      rules_json=__import__("json").dumps(rules))
        if not body.default_model.strip():
            body = body.model_copy(update={"default_model": "auto"})
    elif provider == "github_copilot":
        domain = _github_domain_or_400(body.github_domain or "github.com")
        if body.device_login:
            body = body.model_copy(update={"api_key": _take_device_token(body.device_login, user, domain)})
        _need(body.api_key.strip(), 400, "a GitHub token is required")
        base = body.base_url.strip().rstrip("/")
        if base:
            _need(base.startswith(("http://", "https://")), 400, "base_url must start with http:// or https://")
        else:
            base = copilot.fallback_base(domain)
        fields = dict(provider=provider, github_domain=domain, base_url=base,
                      api_key_enc=config.encrypt(body.api_key.strip()), rules_json="")
    else:
        _need(body.base_url.startswith(("http://", "https://")), 400, "base_url must start with http:// or https://")
        fields = dict(provider="openai", github_domain="", base_url=body.base_url.rstrip("/"),
                      api_key_enc=config.encrypt(body.api_key), rules_json="")
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
    device_login: Optional[str] = None  # finished Copilot device login, replaces the stored key
    default_model: Optional[str] = None
    shared: Optional[bool] = None
    rules_json: Optional[str] = None


@router.patch("/endpoints/{endpoint_id}")
def patch_endpoint(endpoint_id: int, body: EndpointPatch, user: User = Depends(current_user)):
    data = body.model_dump(exclude_none=True)
    device_login = data.pop("device_login", None)
    _need(all(str(data[k]).strip() for k in ("name", "default_model") if k in data), 400,
          "name and default model cannot be empty")
    with session() as s:
        e = s.get(Endpoint, endpoint_id)
        _need(e and e.owner_id == user.id, 404, "endpoint not found")
        provider = _normalize_provider(data.get("provider", getattr(e, "provider", None) or "openai"))
        if provider == "claude_cli":
            data["github_domain"] = ""
            data["base_url"] = ""
            data["api_key"] = ""  # the CLI uses its own credentials, drop any stored key
            data["rules_json"] = ""
        elif provider == "router":
            data["github_domain"] = ""
            data["base_url"] = ""
            data["api_key"] = ""
            raw_rules = data.pop("rules_json") if "rules_json" in data else (e.rules_json or "")
            from . import llm_router
            try:
                if raw_rules and str(raw_rules).strip():
                    rules = llm_router.validate_rules(raw_rules, team_id=e.team_id)
                else:
                    rules = llm_router.default_rules_for_team(e.team_id)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            data["rules_json"] = __import__("json").dumps(rules)
            if not data.get("default_model") and not e.default_model:
                data["default_model"] = "auto"
        elif provider == "github_copilot":
            raw_domain = data["github_domain"] if "github_domain" in data else (e.github_domain or "github.com")
            domain = _github_domain_or_400(raw_domain)
            data["github_domain"] = domain
            if device_login:
                data["api_key"] = _take_device_token(device_login, user, domain)
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


class EndpointCopy(BaseModel):
    team_id: int  # the project to copy this endpoint into


@router.post("/endpoints/{endpoint_id}/copy")
def copy_endpoint(endpoint_id: int, body: EndpointCopy, user: User = Depends(current_user)):
    """Duplicate an endpoint into another project as an independent copy. The encrypted
    key is copied on the server and never leaves it. You must be able to use the source
    (own it, or it is shared in its project where you are a member) and be a member of the
    target project."""
    with session() as s:
        e = s.get(Endpoint, endpoint_id)
        _need(e and (e.owner_id == user.id or (e.shared and team_role(s, user.id, e.team_id))),
              404, "endpoint not found")
        _need(team_role(s, user.id, body.team_id), 403, "you are not a member of that project")
        copy = Endpoint(team_id=body.team_id, owner_id=user.id, shared=e.shared, name=e.name,
                        base_url=e.base_url, api_key_enc=e.api_key_enc, default_model=e.default_model,
                        provider=getattr(e, "provider", None) or "openai",
                        github_domain=getattr(e, "github_domain", None) or "")
        s.add(copy)
        s.commit()
    return _ep_out(copy)


@router.post("/endpoints/{endpoint_id}/test")
async def test_endpoint(endpoint_id: int, user: User = Depends(current_user)):
    """Checks reachability and whether the model answers with a tool call."""
    with session() as s:
        e = s.get(Endpoint, endpoint_id)
        _need(e and e.owner_id == user.id, 404, "endpoint not found")
    tool = {"type": "function", "function": {"name": "ping", "description": "Call this tool.", "parameters": {
        "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}}}
    try:
        from . import usage
        from . import llm_router
        llm_router.begin_turn()
        with usage.bind(origin="test", user_id=user.id, team_id=e.team_id, endpoint_id=e.id,
                        billable_request=True, route_hint="simple"):
            reply = await llm.chat(e, "", [{"role": "user", "content": "Call the ping tool with ok=true."}], [tool])
    except llm.LLMError as err:
        return {"reachable": False, "tool_calling": False, "detail": str(err)}
    return {"reachable": True, "tool_calling": bool(reply["tool_calls"]),
            "detail": "ok" if reply["tool_calls"] else "the model answered in text instead of calling the tool"}



@router.get("/teams/{team_id}/router-defaults")
def router_defaults(team_id: int, user: User = Depends(current_user)):
    """Suggested Option A rules built from this project's Spark, Copilot and Claude CLI endpoints."""
    with session() as s:
        _need(team_role(s, user.id, team_id))
    from . import llm_router
    try:
        return llm_router.default_rules_for_team(team_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


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
    local_only: bool = False


class BotPatch(BaseModel):
    computer_use: Optional[bool] = None
    local_only: Optional[bool] = None
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
            _need(not b.runner, 400, "a coding bot is personal and cannot be shared")
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
        if renamed:  # only private chats are named after a bot; activity belongs to the project
            for c in s.exec(select(Conversation).where(Conversation.bot_id == b.id)).all():
                if c.kind != "feed":
                    c.name = b.name
                    s.add(c)
        s.commit()
        return _bot_out(s, b, user)


# ---------------------------------------------------------------- coding bots (Claude Code, personal)

class CodingBotIn(BaseModel):
    name: str
    model: str = "opus"
    workdir: str = ""
    instructions: str = ""


class CodingBotPatch(BaseModel):
    name: Optional[str] = None
    model: Optional[str] = None
    workdir: Optional[str] = None
    instructions: Optional[str] = None


def _coding_on() -> None:
    _need(config.CODING_BOTS, 404, "not found")


def _coding_settings(name: Optional[str], model: Optional[str], workdir: Optional[str]) -> None:
    _need(name is None or name.strip(), 400, "the bot needs a name")
    _need(model is None or coding.MODEL_RE.fullmatch(model), 400, "model: letters, digits and . _ : - only")
    if workdir is not None:
        try:
            coding.clean_workdir(workdir)
        except ValueError as e:
            raise HTTPException(400, str(e))


@router.get("/coding-bots")
def list_coding_bots(user: User = Depends(current_user)):
    _coding_on()
    with session() as s:
        bots = s.exec(select(Bot).where(Bot.owner_id == user.id, Bot.runner == "claude_code").order_by(Bot.id)).all()
        return [coding.bot_out(b, coding.bot_conv(s, b)) for b in bots]


@router.post("/coding-bots")
def create_coding_bot(body: CodingBotIn, user: User = Depends(current_user)):
    _coding_on()
    _coding_settings(body.name, body.model, body.workdir)
    with session() as s:
        bot, conv = coding.create_bot(s, user, body.name, body.model, body.workdir, body.instructions)
        s.commit()
        return coding.bot_out(bot, conv)


@router.patch("/coding-bots/{bot_id}")
def patch_coding_bot(bot_id: int, body: CodingBotPatch, user: User = Depends(current_user)):
    _coding_on()
    _coding_settings(body.name, body.model, body.workdir)
    with session() as s:
        b = s.get(Bot, bot_id)
        _need(b and b.runner == "claude_code" and b.owner_id == user.id, 404, "bot not found")
        data = body.model_dump(exclude_none=True)
        if "name" in data:
            data["name"] = data["name"].strip()
        if "workdir" in data:
            data["workdir"] = coding.clean_workdir(data["workdir"])
        for k, v in data.items():
            setattr(b, k, v)
        s.add(b)
        conv = coding.bot_conv(s, b)
        if "name" in data:
            conv.name = b.name
            s.add(conv)
        s.commit()
        return coding.bot_out(b, conv)


@router.delete("/bots/{bot_id}")
async def delete_bot(bot_id: int, user: User = Depends(current_user)):
    """Owner only. Removes the bot for everyone, with its chats and routines. The project's knowledge base
    (memory, skills, AGENTS.md) stays."""
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
        for srv in s.exec(select(McpServer).where(McpServer.bot_id == bot_id)).all():
            for cred in s.exec(select(McpCredential).where(McpCredential.mcp_server_id == srv.id)).all():
                s.delete(cred)
        for model in (BotAccess, Memory, Skill, Routine, Secret, McpServer, Approval, ChannelMember):
            q = select(model).where(model.bot_id == bot_id)
            if model is Memory:  # project memory outlives the bot that wrote it
                q = q.where(Memory.team_id == 0)
            for row in s.exec(q).all():
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
        b = _bot(s, bot_id, user, share=True)
        rows = s.exec(select(BotAccess).where(BotAccess.bot_id == bot_id)).all()
        return {"published": b.published, "access": b.access,
                "members": [{"user_id": r.user_id, "role": r.role} for r in rows]}


@router.put("/bots/{bot_id}/share")
def share_bot(bot_id: int, body: ShareIn, user: User = Depends(current_user)):
    _need(body.access in ("team", "selected"), 400, "access: team | selected")
    with session() as s:
        b = _bot(s, bot_id, user, share=True)
        _need(not b.runner, 400, "a coding bot is personal and cannot be shared")
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


# ---------------------------------------------------------------- knowledge base (memory, skills, AGENTS.md)
# A git-backed directory tree per project (app/knowledge.py). Every project member may read and change
# everything; each write is a commit authored by that person and must pass the hash it is based on.

class KnowledgeWrite(BaseModel):
    path: str
    content: str
    base_hash: Optional[str] = None  # None: create a new file
    message: str = ""
    allow_secret: bool = False


class KnowledgePath(BaseModel):
    path: str


class KnowledgeRevert(BaseModel):
    path: str
    commit: str
    base_hash: Optional[str] = None


def _kb(bot_id: int, user: User) -> tuple[str, list[int]]:
    from . import knowledge
    with session() as s:
        b = _bot(s, bot_id, user)
        bot_ids = [x.id for x in s.exec(select(Bot).where(Bot.team_id == b.team_id)).all()] if b.team_id else [b.id]
        key = knowledge.key_for(b)
    knowledge.ensure(key)
    return key, bot_ids


def _kb_do(fn):
    from . import knowledge
    try:
        return fn()
    except knowledge.Conflict as c:
        raise HTTPException(409, c.info) from None
    except knowledge.KnowledgeError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/bots/{bot_id}/knowledge")
def knowledge_tree(bot_id: int, user: User = Depends(current_user)):
    """Directories and files of the bot's knowledge base, with the prompt budgets."""
    from . import knowledge, memory, prompt
    key, _ = _kb(bot_id, user)
    return {**knowledge.tree(key), "profile_path": memory.user_path(user),
            "limits": {memory.CONTEXT_FILE: prompt.PROJECT_CONTEXT_LIMIT, "memory/": memory.TEAM_LIMIT,
                       memory.user_path(user): memory.USER_LIMIT, "skills/": prompt.SKILLS_LIMIT}}


@router.get("/bots/{bot_id}/knowledge/file")
def knowledge_read(bot_id: int, path: str, user: User = Depends(current_user)):
    from . import knowledge
    key, _ = _kb(bot_id, user)
    return _kb_do(lambda: knowledge.read(key, path))


@router.put("/bots/{bot_id}/knowledge/file")
def knowledge_write(bot_id: int, body: KnowledgeWrite, user: User = Depends(current_user)):
    from . import knowledge
    key, bot_ids = _kb(bot_id, user)
    if not body.allow_secret:
        found = secrets_http.find_leaks(body.content, bot_ids)
        if found:
            raise HTTPException(409, {"code": "secret", "found": found, "message":
                                      "this text seems to contain a secret; store secrets with the bot instead"})
    return _kb_do(lambda: knowledge.write(key, body.path, body.content, body.base_hash or None,
                                          knowledge.user_author(user), body.message.strip()))


@router.delete("/bots/{bot_id}/knowledge/file")
def knowledge_delete(bot_id: int, path: str, base_hash: str = "", user: User = Depends(current_user)):
    from . import knowledge
    key, _ = _kb(bot_id, user)
    return _kb_do(lambda: knowledge.delete(key, path, base_hash or None, knowledge.user_author(user)))


@router.post("/bots/{bot_id}/knowledge/dir")
def knowledge_mkdir(bot_id: int, body: KnowledgePath, user: User = Depends(current_user)):
    from . import knowledge
    key, _ = _kb(bot_id, user)
    return _kb_do(lambda: knowledge.mkdir(key, body.path, knowledge.user_author(user)))


@router.get("/bots/{bot_id}/knowledge/history")
def knowledge_history(bot_id: int, path: str = "", user: User = Depends(current_user)):
    from . import knowledge
    key, _ = _kb(bot_id, user)
    return _kb_do(lambda: knowledge.history(key, path, 50))


@router.get("/bots/{bot_id}/knowledge/version")
def knowledge_version(bot_id: int, path: str, commit: str, user: User = Depends(current_user)):
    from . import knowledge
    key, _ = _kb(bot_id, user)
    content = _kb_do(lambda: knowledge.version(key, path, commit))
    return {"path": path, "commit": commit, "exists": content is not None, "content": content or "",
            "hash": knowledge.digest(content)}


@router.post("/bots/{bot_id}/knowledge/revert")
def knowledge_revert(bot_id: int, body: KnowledgeRevert, user: User = Depends(current_user)):
    from . import knowledge
    key, _ = _kb(bot_id, user)
    return _kb_do(lambda: knowledge.revert(key, body.path, body.commit, body.base_hash or None,
                                           knowledge.user_author(user)))


@router.get("/bots/{bot_id}/skills")
def list_skills(bot_id: int, user: User = Depends(current_user)):
    """Skills of the bot's project (files in skills/), for the '@skill' menu."""
    from . import memory
    key, _ = _kb(bot_id, user)
    return [{k: v for k, v in sk.items() if k != "instructions"} for sk in memory.skills(key)]


# ---------------------------------------------------------------- prompt preview

@router.get("/bots/{bot_id}/prompt-preview")
async def prompt_preview(bot_id: int, user: User = Depends(current_user)):
    """The system prompt this bot gets in your private chat with it, section by section."""
    from . import agent
    with session() as s:
        b = _bot(s, bot_id, user)
        conv = s.exec(select(Conversation).where(Conversation.kind.in_(("dm", "home")),
                                                 Conversation.bot_id == b.id,
                                                 Conversation.user_id == user.id)).first()
        if conv is None:  # no chat yet: preview as a fresh private chat
            conv = Conversation(team_id=b.team_id, kind="home" if b.is_home else "dm", name=b.name,
                                bot_id=b.id, user_id=user.id)
    ctx = agent.Ctx(b.id, user.id, conv.id or 0, False, team_id=b.team_id, is_home=b.is_home)
    parts = await agent.build_prompt(b, conv, user, ctx)
    out = parts.preview()
    out["note"] = ("Built for your private chat. The current time and, for some models, a tool-use rule are "
                   "added per call; the conversation history follows the prompt.")
    return out


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
    auth_mode: str = "none"       # none | token | oauth
    auth_scope: str = "shared"    # shared (one token for the bot) | user (each member connects their own)
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    token: str = ""               # the shared token, when auth_mode=token and auth_scope=shared
    # OAuth client config (auth_mode == oauth); each member then authorizes their own account.
    oauth_authorize_url: str = ""
    oauth_token_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_scopes: str | list[str] = ""


def _mcp_out(r: McpServer, user_id: int, s) -> dict:
    """The token value is never returned; `connected` says whether this user has their own credential."""
    mode = getattr(r, "auth_mode", "none") or "none"
    per_user = mode == "oauth" or (mode == "token" and r.auth_scope == "user")
    connected = bool(s.exec(select(McpCredential).where(
        McpCredential.mcp_server_id == r.id, McpCredential.user_id == user_id)).first()) if per_user else None
    return {"id": r.id, "name": r.name, "url": r.url, "trusted": r.trusted,
            "auth_mode": mode, "auth_scope": getattr(r, "auth_scope", "shared"),
            "auth_header": getattr(r, "auth_header", "Authorization"), "auth_prefix": getattr(r, "auth_prefix", ""),
            "has_shared_token": bool(getattr(r, "token_enc", "")),
            "oauth_configured": bool(getattr(r, "oauth_client_id", "")),
            "oauth_authorize_url": getattr(r, "oauth_authorize_url", "") or "",
            "oauth_token_url": getattr(r, "oauth_token_url", "") or "",
            "oauth_client_id": getattr(r, "oauth_client_id", "") or "",
            "oauth_scopes": getattr(r, "oauth_scopes", "") or "",
            "has_oauth_secret": bool(getattr(r, "oauth_client_secret_enc", "")),
            "connected": connected}


@router.get("/bots/{bot_id}/mcp")
def list_mcp(bot_id: int, user: User = Depends(current_user)):
    # Any user of the bot may list servers and see whether they are connected, so a team
    # member can connect their own per-user token without being a manager.
    with session() as s:
        _bot(s, bot_id, user)
        rows = s.exec(select(McpServer).where(McpServer.bot_id == bot_id)).all()
        return [_mcp_out(r, user.id, s) for r in rows]


@router.post("/bots/{bot_id}/mcp")
async def add_mcp(bot_id: int, body: McpIn, user: User = Depends(current_user)):
    _need(re.fullmatch(r"[a-z0-9_-]{1,24}", body.name), 400, "name: 1-24 chars of a-z 0-9 _ -")
    _need(body.auth_mode in ("none", "token", "oauth"), 400, "auth_mode: none | token | oauth")
    _need(body.auth_scope in ("shared", "user"), 400, "auth_scope: shared | user")
    token_enc, oauth_secret_enc = "", ""
    scope = body.auth_scope
    if body.auth_mode == "token" and body.auth_scope == "shared":
        _need(body.token.strip(), 400, "a shared-token server needs a token")
        token_enc = config.encrypt(body.token.strip())
    if body.auth_mode == "oauth":
        _need(body.oauth_authorize_url.strip() and body.oauth_token_url.strip() and body.oauth_client_id.strip(),
              400, "OAuth needs an authorize URL, a token URL and a client id")
        _need(config.PUBLIC_URL, 400, "TB_PUBLIC_URL must be set to use OAuth")
        from . import mcp_oauth
        body.oauth_scopes = mcp_oauth.normalize_scopes(body.oauth_scopes)
        oauth_secret_enc = config.encrypt(body.oauth_client_secret.strip()) if body.oauth_client_secret.strip() else ""
        scope = "user"  # OAuth is always per user
    with session() as s:
        _bot(s, bot_id, user, manage=True)
        r = McpServer(bot_id=bot_id, name=body.name, url=body.url, trusted=body.trusted,
                      headers_enc=config.encrypt(json.dumps(body.headers)) if body.headers else "",
                      auth_mode=body.auth_mode, auth_scope=scope,
                      auth_header=(body.auth_header or "Authorization").strip(),
                      auth_prefix=body.auth_prefix, token_enc=token_enc,
                      oauth_authorize_url=body.oauth_authorize_url.strip(),
                      oauth_token_url=body.oauth_token_url.strip(),
                      oauth_client_id=body.oauth_client_id.strip(),
                      oauth_client_secret_enc=oauth_secret_enc, oauth_scopes=body.oauth_scopes.strip())
        s.add(r)
        s.commit()
        r_id = r.id
    events.publish_bot(bot_id, "mcp", action="add", id=r_id)
    # A per-user or OAuth server has no credentials yet, so don't probe it as the adder.
    try:
        probe_user = user.id if scope != "user" else None
        with session() as s:
            r = s.get(McpServer, r_id)
        tools = [t["name"] for t in await asyncio.wait_for(mcp_client.list_tools(r, probe_user, ttl=0), 20)]
        error = ""
    except Exception as e:  # noqa: BLE001
        tools, error = [], f"saved, but the server did not answer: {e.__class__.__name__}"
    return {"id": r_id, "name": body.name, "tools": tools, "error": error}



class McpPatch(BaseModel):
    name: str | None = None
    url: str | None = None
    trusted: bool | None = None
    headers: dict[str, str] | None = None  # omitted = keep; {} = clear static headers
    auth_mode: str | None = None
    auth_scope: str | None = None
    auth_header: str | None = None
    auth_prefix: str | None = None
    token: str | None = None  # omitted or empty = keep the stored shared token
    oauth_authorize_url: str | None = None
    oauth_token_url: str | None = None
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None  # omitted or empty = keep the stored secret
    oauth_scopes: str | list[str] | None = None


@router.patch("/mcp/{server_id}")
async def patch_mcp(server_id: int, body: McpPatch, user: User = Depends(current_user)):
    """Update an MCP server (owner or manager). The client secret is never returned."""
    from . import mcp_oauth
    data = body.model_dump(exclude_none=True)
    if "auth_mode" in data:
        _need(data["auth_mode"] in ("none", "token", "oauth"), 400, "auth_mode: none | token | oauth")
    if "auth_scope" in data:
        _need(data["auth_scope"] in ("shared", "user"), 400, "auth_scope: shared | user")
    if "name" in data:
        _need(re.fullmatch(r"[a-z0-9_-]{1,24}", data["name"]), 400, "name: 1-24 chars of a-z 0-9 _ -")
    with session() as s:
        r = s.get(McpServer, server_id)
        _need(r, 404, "server not found")
        _bot(s, r.bot_id, user, manage=True)
        mode = data.get("auth_mode", getattr(r, "auth_mode", "none") or "none")
        scope = data.get("auth_scope", getattr(r, "auth_scope", "shared") or "shared")
        if mode == "oauth":
            scope = "user"
            authorize = (data.get("oauth_authorize_url", r.oauth_authorize_url) or "").strip()
            token_url = (data.get("oauth_token_url", r.oauth_token_url) or "").strip()
            client_id = (data.get("oauth_client_id", r.oauth_client_id) or "").strip()
            _need(authorize and token_url and client_id, 400,
                  "OAuth needs an authorize URL, a token URL and a client id")
            _need(config.PUBLIC_URL, 400, "TB_PUBLIC_URL must be set to use OAuth")
            r.oauth_authorize_url, r.oauth_token_url, r.oauth_client_id = authorize, token_url, client_id
            if "oauth_scopes" in data:
                r.oauth_scopes = mcp_oauth.normalize_scopes(data["oauth_scopes"])
            if data.get("oauth_client_secret"):
                r.oauth_client_secret_enc = config.encrypt(data["oauth_client_secret"].strip())
            data.pop("oauth_authorize_url", None)
            data.pop("oauth_token_url", None)
            data.pop("oauth_client_id", None)
            data.pop("oauth_scopes", None)
            data.pop("oauth_client_secret", None)
        else:
            if "oauth_scopes" in data:
                r.oauth_scopes = mcp_oauth.normalize_scopes(data.pop("oauth_scopes"))
            if "oauth_client_secret" in data:
                secret = (data.pop("oauth_client_secret") or "").strip()
                if secret:
                    r.oauth_client_secret_enc = config.encrypt(secret)
        r.auth_mode, r.auth_scope = mode, scope
        data.pop("auth_mode", None)
        data.pop("auth_scope", None)
        if mode == "token" and scope == "shared" and data.get("token"):
            r.token_enc = config.encrypt(data.pop("token").strip())
        else:
            data.pop("token", None)
        if "headers" in data:
            headers = data.pop("headers") or {}
            r.headers_enc = config.encrypt(json.dumps(headers)) if headers else ""
        if "auth_header" in data:
            r.auth_header = (data.pop("auth_header") or "Authorization").strip()
        if "auth_prefix" in data:
            r.auth_prefix = data.pop("auth_prefix")
        for k, v in data.items():
            setattr(r, k, v.strip() if isinstance(v, str) and k in ("name", "url", "oauth_authorize_url",
                                                                    "oauth_token_url", "oauth_client_id") else v)
        s.add(r)
        s.commit()
        out = _mcp_out(r, user.id, s)
        bot_id = r.bot_id
    events.publish_bot(bot_id, "mcp", action="update", id=server_id)
    return out


class McpCredIn(BaseModel):
    value: str


@router.put("/mcp/{server_id}/credential")
def set_mcp_credential(server_id: int, body: McpCredIn, user: User = Depends(current_user)):
    """Connect the current user's own token to a per-user MCP server. Usable by any member."""
    _need(body.value.strip(), 400, "token cannot be empty")
    with session() as s:
        r = s.get(McpServer, server_id)
        _need(r, 404, "server not found")
        _bot(s, r.bot_id, user)
        _need(r.auth_mode == "token" and r.auth_scope == "user", 400,
              "this server does not use per-user tokens")
        cred = s.exec(select(McpCredential).where(
            McpCredential.mcp_server_id == server_id, McpCredential.user_id == user.id)).first()
        if cred:
            cred.value_enc = config.encrypt(body.value.strip())
            cred.updated_at = now()
        else:
            cred = McpCredential(mcp_server_id=server_id, user_id=user.id,
                                 value_enc=config.encrypt(body.value.strip()))
        s.add(cred)
        s.commit()
        bot_id = r.bot_id
    mcp_client.invalidate(server_id)
    events.publish_bot(bot_id, "mcp", action="update", id=server_id)
    return {"ok": True, "connected": True}


@router.delete("/mcp/{server_id}/credential")
def clear_mcp_credential(server_id: int, user: User = Depends(current_user)):
    with session() as s:
        r = s.get(McpServer, server_id)
        _need(r, 404, "server not found")
        _bot(s, r.bot_id, user)
        cred = s.exec(select(McpCredential).where(
            McpCredential.mcp_server_id == server_id, McpCredential.user_id == user.id)).first()
        bot_id = r.bot_id
        if cred:
            s.delete(cred)
            s.commit()
    mcp_client.invalidate(server_id)
    events.publish_bot(bot_id, "mcp", action="update", id=server_id)
    return {"ok": True, "connected": False}


@router.get("/mcp/{server_id}/oauth/start")
def mcp_oauth_start(server_id: int, user: User = Depends(current_user)):
    """Begin OAuth for the current user: returns the provider authorize URL to redirect to."""
    from . import mcp_oauth
    with session() as s:
        r = s.get(McpServer, server_id)
        _need(r, 404, "server not found")
        _bot(s, r.bot_id, user)
        _need(getattr(r, "auth_mode", "") == "oauth", 400, "this server does not use OAuth")
        try:
            url = mcp_oauth.build_authorize_url(r, user.id)
        except mcp_oauth.OAuthError as e:
            raise HTTPException(400, str(e)) from e
    return {"url": url}


@router.api_route("/mcp/oauth/callback", methods=["GET", "POST"])
async def mcp_oauth_callback(request: Request, code: str = Query(default=""),
                            state: str = Query(default=""), error: str = Query(default="")):
    """Provider redirect (GET query or POST form_post): exchange the code; identity comes from state."""
    from . import mcp_oauth

    def back(flag: str) -> RedirectResponse:
        return RedirectResponse(f"/#mcp={flag}", status_code=302)

    if request.method == "POST":
        form = await request.form()
        code = (form.get("code") or code) or ""
        state = (form.get("state") or state) or ""
        error = (form.get("error") or error) or ""

    if error or not code or not state:
        return back("error")
    st = mcp_oauth.take_state(state)
    if not st:
        return back("error")
    with session() as s:
        r = s.get(McpServer, st.mcp_server_id)
    if not r or getattr(r, "auth_mode", "") != "oauth":
        return back("error")
    try:
        access, refresh_tok, expires_at = await mcp_oauth.exchange_code(r, code, st.code_verifier)
    except mcp_oauth.OAuthError:
        return back("error")
    with session() as s:
        cred = s.exec(select(McpCredential).where(
            McpCredential.mcp_server_id == r.id, McpCredential.user_id == st.user_id)).first()
        if cred:
            cred.value_enc = config.encrypt(access)
            cred.refresh_enc = config.encrypt(refresh_tok or "")
            cred.expires_at = expires_at
            cred.updated_at = now()
        else:
            cred = McpCredential(mcp_server_id=r.id, user_id=st.user_id, value_enc=config.encrypt(access),
                                 refresh_enc=config.encrypt(refresh_tok or ""), expires_at=expires_at)
        s.add(cred)
        s.commit()
    mcp_client.invalidate(r.id)
    return back("connected")


@router.delete("/mcp/{server_id}")
def delete_mcp(server_id: int, user: User = Depends(current_user)):
    with session() as s:
        r = s.get(McpServer, server_id)
        _need(r, 404, "server not found")
        _bot(s, r.bot_id, user, manage=True)
        bot_id = r.bot_id
        for cred in s.exec(select(McpCredential).where(McpCredential.mcp_server_id == server_id)).all():
            s.delete(cred)
        s.delete(r)
        s.commit()
    mcp_client.invalidate(server_id)
    events.publish_bot(bot_id, "mcp", action="delete", id=server_id)
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
    """Compatibility URL for old clients; activity is shared once per project, not per bot."""
    with session() as s:
        b = _bot(s, bot_id, user)
        _need(b.is_team, 400, "only a Team Bot has project activity")
        _need(team_role(s, user.id, b.team_id), 404, "project not found")
    return agent.project_feed(b.team_id).model_dump()


@router.get("/teams/{team_id}/feed")
def team_feed(team_id: int, user: User = Depends(current_user)):
    """The project-wide activity feed: finished-task summaries from published Team Bots."""
    with session() as s:
        _need(team_role(s, user.id, team_id), 404, "project not found")
    return agent.project_feed(team_id).model_dump()


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
            _need(body.name.strip(), 400, "the team chat needs a name")
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
    ext = os.path.splitext(m.image_path)[1].lower()
    mime = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "image/jpeg")
    return FileResponse(m.image_path, media_type=mime)


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



@router.get("/teams/{team_id}/handoffs")
def list_handoffs(team_id: int, user: User = Depends(current_user)):
    """Bot-to-bot threads for this person in this project (visible, with approvals)."""
    from .db import Approval
    with session() as s:
        _need(team_role(s, user.id, team_id), 404, "team not found")
        rows = s.exec(select(Conversation).where(
            Conversation.team_id == team_id, Conversation.kind == "handoff",
            Conversation.user_id == user.id).order_by(Conversation.id.desc())).all()
        out = []
        for c in rows:
            bot, peer = s.get(Bot, c.bot_id), s.get(Bot, getattr(c, "peer_bot_id", None) or 0)
            pending = s.exec(select(Approval).where(
                Approval.conversation_id == c.id, Approval.status == "pending")).first()
            out.append({
                "id": c.id, "team_id": c.team_id, "kind": c.kind, "name": c.name,
                "bot_id": c.bot_id, "peer_bot_id": getattr(c, "peer_bot_id", None),
                "user_id": c.user_id,
                "bot_name": bot.name if bot else "", "bot_handle": bot.handle if bot else "",
                "peer_name": peer.name if peer else "", "peer_handle": peer.handle if peer else "",
                "pending_approval": bool(pending),
            })
        return out


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
             "tool_call_id": m.tool_call_id, "has_image": bool(m.image_path),
             "route": getattr(m, "route", "") or "", "route_reason": getattr(m, "route_reason", "") or ""} for m in rows]


_IMG_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


def _save_user_image(data_url: str) -> str:
    """Decode a base64 image data URL from the composer (a pasted screenshot) and store it."""
    m = re.match(r"data:(image/(?:png|jpe?g|gif|webp));base64,(.+)$", data_url, re.S)
    _need(m, 400, "image must be a base64 data URL (png, jpg, gif or webp)")
    mime = "image/jpeg" if m.group(1) in ("image/jpg", "image/jpeg") else m.group(1)
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except Exception:
        raise HTTPException(400, "image is not valid base64")
    _need(len(raw) <= 10 * 1024 * 1024, 400, "image too large (max 10 MB)")
    folder = os.path.join(config.DATA_DIR, "uploads")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{uuid.uuid4().hex}{_IMG_EXT[mime]}")
    with open(path, "wb") as f:
        f.write(raw)
    return path


class MessageIn(BaseModel):
    content: str
    mode: str = "auto"  # what to do when the bot is busy: auto | steer | queue | interrupt
    allow_secret: bool = False  # the person looked at the warning and sends it anyway
    image: str = ""  # optional base64 image data URL: a pasted screenshot


@router.post("/conversations/{conv_id}/messages")
async def send_message(conv_id: int, body: MessageIn, user: User = Depends(current_user)):
    _need(body.content.strip() or body.image, 400, "empty message")
    _need(body.mode in agent.MODES, 400, "mode: " + " | ".join(agent.MODES))
    with session() as s:
        conv = _conv(s, conv_id, user)
        _need(conv.kind != "feed", 400, "the activity feed is written by the bot only")
        # Only someone who may change a bot (any project member who can use it) learns that a typed value is a secret.
        bot_ids = [conv.bot_id] if conv.kind in ("dm", "home") else [m.bot_id for m in s.exec(select(ChannelMember).where(
            ChannelMember.conversation_id == conv_id, ChannelMember.bot_id != None)).all()]  # noqa: E711
        managed = [i for i in bot_ids if (b := s.get(Bot, i)) and can_manage(s, user.id, b)]
    if not body.allow_secret:
        found = secrets_http.find_leaks(body.content, managed)
        if found:  # nothing is stored; the person decides what to do with it
            raise HTTPException(409, {"code": "secret", "found": found,
                                      "message": "this message seems to contain a secret"})
    image_path = _save_user_image(body.image) if body.image else None
    m = agent.handle_user_message(conv_id, user.id, body.content.strip(), body.mode, image_path=image_path)
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


@router.get("/bots/{bot_id}/events")
async def stream_bot_events(bot_id: int, user: User = Depends(current_user)):
    # Live updates for the settings drawer (MCP servers added by a tool or another member).
    with session() as s:
        _bot(s, bot_id, user)

    async def gen():
        q = events.subscribe_bot(bot_id)
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 20)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            events.unsubscribe_bot(bot_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
