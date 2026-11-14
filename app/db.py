"""Data model. One SQLite/Postgres database; every table is a SQLModel class."""
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Session, SQLModel, create_engine, select

from . import config


def now() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    display_name: str
    password_hash: str
    email: str = Field(default="", index=True)  # verified address from an OAuth identity, if any
    home_endpoint_id: Optional[int] = None  # model endpoint for the overview bot
    home_model: str = ""  # empty = endpoint default


class Identity(SQLModel, table=True):
    """One sign-in identity (GitHub / Google). A user may have several; membership stays per project."""
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_identity_provider_subject"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str = Field(index=True)  # "google" | "github"
    subject: str = Field(index=True)  # the provider's stable user id
    user_id: int = Field(index=True)
    email: str = ""
    created_at: datetime = Field(default_factory=now)
    last_login_at: Optional[datetime] = None


class OAuthState(SQLModel, table=True):
    """Short-lived state for an OAuth round trip (CSRF protection, optional account linking)."""
    state: str = Field(primary_key=True)
    provider: str
    link_user_id: Optional[int] = None  # set when an already signed-in user links another identity
    invite_code: str = ""  # a project invite that grants account creation and membership on sign-up
    created_at: datetime = Field(default_factory=now)


class AuthToken(SQLModel, table=True):
    token: str = Field(primary_key=True)
    user_id: int = Field(index=True)
    created_at: Optional[datetime] = None  # None for tokens issued before this column existed


class ApiToken(SQLModel, table=True):
    """Personal API token for the overview bot (voice / external agents). The raw token is shown
    once at creation and only a hash is stored."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    name: str = ""
    scope: str = "home-bot"
    token_hash: str = Field(index=True, unique=True)
    prefix: str = ""
    created_at: datetime = Field(default_factory=now)
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class Team(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str  # the project
    client: str = ""  # optional customer name; one client can have several projects
    invite_code: str = Field(index=True, unique=True)
    bot_sharing: str = "everyone"  # everyone | admins
    monthly_budget: Optional[float] = None  # amount in budget_currency; None = no budget
    budget_currency: str = "USD"  # USD | EUR
    budget_block: bool = False  # if True, new LLM calls are refused once the month is over budget


class TeamMember(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    team_id: int = Field(index=True)
    user_id: int = Field(index=True)
    role: str = "member"  # admin | member


class Endpoint(SQLModel, table=True):
    """An OpenAI-compatible model endpoint. The API key is stored encrypted and never returned."""
    id: Optional[int] = Field(default=None, primary_key=True)
    team_id: int = Field(index=True)
    owner_id: int
    shared: bool = False  # usable by every team member when creating bots
    name: str
    base_url: str  # display / OpenAI base; for Copilot, the fallback API host; empty for claude_cli
    api_key_enc: str = ""  # OpenAI key, or GitHub OAuth token for github_copilot; empty for claude_cli
    default_model: str
    provider: str = "openai"  # openai | github_copilot | claude_cli | router
    github_domain: str = ""  # github.com or org.ghe.com; unused for openai
    models_json: str = ""  # last successful /models list, cached for the UI
    models_fetched_at: Optional[datetime] = None
    rules_json: str = ""  # router rules (JSON); unused for other providers


class Bot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    team_id: int = Field(index=True)
    owner_id: int = Field(index=True)
    name: str
    handle: str = Field(index=True)  # @handle, unique per team
    instructions: str = ""
    endpoint_id: int
    model: str = ""  # empty = endpoint default
    is_team: bool = False
    published: bool = False  # team bots start as a draft only the owner sees
    access: str = "team"  # team | selected
    approval_mode: str = "ask"  # ask | auto
    computer_use: bool = False  # own screen + mouse/keyboard tools; needs a model with image input
    is_home: bool = False  # per-user overview bot; not tied to a project (team_id is 0)
    local_only: bool = False  # router must keep this bot on a local (Spark) route


class BotAccess(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(index=True)
    user_id: int = Field(index=True)
    role: str = "collaborator"  # manager | collaborator


class Conversation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    team_id: int = Field(index=True)
    kind: str  # dm | channel | feed | home (the per-user overview chat)
    name: str = ""
    share_activity: bool = True  # dm only: summaries of finished tasks go to the project's activity feed
    user_id: Optional[int] = Field(default=None, index=True)  # dm only
    bot_id: Optional[int] = Field(default=None, index=True)  # dm/home; None for channels and project activity


class ChannelMember(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(index=True)
    user_id: Optional[int] = None
    bot_id: Optional[int] = None
    # For bot members: False = reply to every message (the default); True = only on @handle.
    mention_only: bool = False


class AppSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""


class LlmUsage(SQLModel, table=True):
    """One attempt at an LLM call. Tokens and a cost snapshot at write time; never contains secrets."""
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=now, index=True)
    user_id: Optional[int] = Field(default=None, index=True)
    team_id: Optional[int] = Field(default=None, index=True)
    bot_id: Optional[int] = Field(default=None, index=True)
    conversation_id: Optional[int] = Field(default=None, index=True)
    endpoint_id: Optional[int] = Field(default=None, index=True)
    provider: str = "openai"
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: int = 0
    success: bool = True
    error: str = ""
    retry: bool = False
    origin: str = "chat"  # chat, routine, ask_bot, voice, overview, test, feed, route
    billing_mode: str = "token"  # token | premium_request | zero | electricity | subscription
    premium_requests: float = 0
    cost_usd: Optional[float] = None
    cost_unknown: bool = False
    notes: str = ""
    route: str = ""  # router destination name, when a Router chose this call
    route_reason: str = ""


class ModelPrice(SQLModel, table=True):
    """Editable price for one model in one billing mode. Seeded from official pages; user edits win."""
    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str = Field(index=True)  # openai, anthropic, github_copilot, ...
    model: str = Field(index=True)
    billing_mode: str = "token"  # token | premium_request
    currency: str = "USD"
    input_per_million: Optional[float] = None
    output_per_million: Optional[float] = None
    cached_per_million: Optional[float] = None
    cache_write_per_million: Optional[float] = None
    premium_multiplier: Optional[float] = None
    source_url: str = ""
    source_date: str = ""  # YYYY-MM-DD
    notes: str = ""
    aliases_json: str = "[]"
    user_edited: bool = False
    updated_at: datetime = Field(default_factory=now)


class Message(SQLModel, table=True):
    """Stored in OpenAI chat format so an agent run can always be resumed from the database."""
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(index=True)
    role: str  # user | assistant | tool
    content: str = ""
    author_user_id: Optional[int] = None
    author_bot_id: Optional[int] = None
    requester_id: Optional[int] = None  # the person the bot is acting for
    tool_calls: str = ""  # JSON list, assistant messages only
    tool_call_id: str = ""
    image_path: str = ""  # screenshot that belongs to a tool result
    created_at: datetime = Field(default_factory=now)
    route: str = ""  # short router label shown on the step line, when present
    route_reason: str = ""


class Memory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(index=True)
    scope: str  # team | personal
    user_id: Optional[int] = None  # owner of a personal memory
    content: str
    status: str = "active"  # active | pending (a learning awaiting review)
    proposed_by: Optional[int] = None
    created_at: datetime = Field(default_factory=now)


class Skill(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(index=True)
    name: str
    description: str = ""
    instructions: str
    created_by: int


class Routine(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(index=True)
    owner_id: int
    conversation_id: int
    name: str
    cron: str  # 5-field crontab
    prompt: str
    enabled: bool = True
    last_run: Optional[datetime] = None


class Approval(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(index=True)
    bot_id: int
    message_id: int  # the assistant message holding the tool call
    tool_call_id: str
    tool_name: str
    arguments: str
    requester_id: int
    status: str = "pending"  # pending | allowed | denied
    created_at: datetime = Field(default_factory=now)


class ApprovalRule(SQLModel, table=True):
    """'Always allow' for one person, one bot, one tool."""
    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(index=True)
    user_id: int
    tool_name: str


class Secret(SQLModel, table=True):
    """Owner-provided secrets. The value never enters the sandbox or the model's context: the
    app fills it into outgoing http_request calls, and only for the hosts it is bound to."""
    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(index=True)
    name: str
    value_enc: str
    hosts: str = ""  # comma-separated host names this secret may be sent to


class McpServer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(index=True)
    name: str
    url: str  # streamable HTTP endpoint
    headers_enc: str = ""  # JSON object of extra static headers, encrypted
    trusted: bool = False  # trusted servers skip the approval gate
    # Auth. mode: "none" | "token" (static bearer/API key) | "oauth" (Phase 2, per user).
    auth_mode: str = "none"
    auth_scope: str = "shared"  # "shared" = one credential for the bot; "user" = each member connects their own
    auth_header: str = "Authorization"  # header the token goes in
    auth_prefix: str = "Bearer"  # scheme word joined to the token with a space ("" for a bare API key)
    token_enc: str = ""  # the shared token (auth_scope == "shared"), encrypted
    # OAuth (auth_mode == "oauth", always per user). The admin configures the client once;
    # each member authorizes and gets their own access/refresh tokens in McpCredential.
    oauth_authorize_url: str = ""
    oauth_token_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret_enc: str = ""  # encrypted; may be empty for a public client (PKCE only)
    oauth_scopes: str = ""  # space-separated


class McpCredential(SQLModel, table=True):
    """A per-user credential for an MCP server (auth_scope == "user" or OAuth): each person
    connects their own account, so a shared team bot uses the requester's own access."""
    __table_args__ = (UniqueConstraint("mcp_server_id", "user_id", name="uq_mcpcred_server_user"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    mcp_server_id: int = Field(index=True)
    user_id: int = Field(index=True)
    value_enc: str = ""  # the user's token (static) or OAuth access token, encrypted
    refresh_enc: str = ""  # OAuth refresh token, encrypted
    expires_at: Optional[datetime] = None  # OAuth access-token expiry; None = no expiry known
    updated_at: datetime = Field(default_factory=now)


class McpOAuthState(SQLModel, table=True):
    """Short-lived state for an MCP OAuth authorization (CSRF state + PKCE verifier)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    state: str = Field(index=True)
    mcp_server_id: int
    user_id: int
    code_verifier: str
    created_at: datetime = Field(default_factory=now)


_connect_args = {"check_same_thread": False} if config.DB_URL.startswith("sqlite") else {}
engine = create_engine(config.DB_URL, connect_args=_connect_args)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _ensure_project_activity_feed_index()
    _ensure_channelmember_mention_only()
    _ensure_home_columns()
    _ensure_endpoint_provider_columns()
    _ensure_oauth_columns()
    _ensure_costs_columns()
    _ensure_router_columns()
    _ensure_mcp_auth_columns()
    from .usage import seed_prices
    seed_prices()
    migrate_channel_bots()
    from .home import migrate_home_bots
    migrate_home_bots()


def _ensure_project_activity_feed_index() -> None:
    """Keep exactly one new-style activity feed per project.

    Old activity feeds have a bot_id and deliberately stay untouched: moving those
    historic summaries into a project-wide feed would broaden their audience.
    """
    with Session(engine) as s:
        feeds = s.exec(select(Conversation).where(
            Conversation.kind == "feed", Conversation.bot_id.is_(None)
        ).order_by(Conversation.team_id, Conversation.id)).all()
        primary_by_team: dict[int, Conversation] = {}
        for feed in feeds:
            primary = primary_by_team.setdefault(feed.team_id, feed)
            if primary is feed:
                continue
            for message in s.exec(select(Message).where(Message.conversation_id == feed.id)).all():
                message.conversation_id = primary.id
                s.add(message)
            s.delete(feed)
        s.commit()
    # NULL values are normally distinct in a composite unique constraint, so use
    # a partial index for just the bot-less, project-wide feeds.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_project_activity_feed "
            "ON conversation (team_id) WHERE kind = 'feed' AND bot_id IS NULL"
        )


def _ensure_channelmember_mention_only() -> None:
    """Add ChannelMember.mention_only on databases created before that column existed."""
    if not config.DB_URL.startswith("sqlite"):
        return
    with engine.connect() as conn:
        cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(channelmember)").fetchall()]
        if "mention_only" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE channelmember ADD COLUMN mention_only BOOLEAN NOT NULL DEFAULT 0")
            conn.commit()


def _ensure_oauth_columns() -> None:
    """Add User.email and AuthToken.created_at on databases created before OAuth sign-in."""
    if not config.DB_URL.startswith("sqlite"):
        return
    with engine.connect() as conn:
        user_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(user)").fetchall()]
        if "email" not in user_cols:
            conn.exec_driver_sql("ALTER TABLE user ADD COLUMN email VARCHAR NOT NULL DEFAULT ''")
        token_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(authtoken)").fetchall()]
        if "created_at" not in token_cols:
            conn.exec_driver_sql("ALTER TABLE authtoken ADD COLUMN created_at DATETIME")
        state_tbl = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='oauthstate'").fetchall()
        if state_tbl:
            state_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(oauthstate)").fetchall()]
            if "invite_code" not in state_cols:
                conn.exec_driver_sql("ALTER TABLE oauthstate ADD COLUMN invite_code VARCHAR NOT NULL DEFAULT ''")
        conn.commit()


def _ensure_home_columns() -> None:
    """Add User.home_* and Bot.is_home on databases created before those columns existed."""
    if not config.DB_URL.startswith("sqlite"):
        return
    with engine.connect() as conn:
        user_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(user)").fetchall()]
        if "home_endpoint_id" not in user_cols:
            conn.exec_driver_sql("ALTER TABLE user ADD COLUMN home_endpoint_id INTEGER")
        if "home_model" not in user_cols:
            conn.exec_driver_sql("ALTER TABLE user ADD COLUMN home_model VARCHAR DEFAULT ''")
        bot_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(bot)").fetchall()]
        if "is_home" not in bot_cols:
            conn.exec_driver_sql("ALTER TABLE bot ADD COLUMN is_home BOOLEAN NOT NULL DEFAULT 0")
        conn.commit()


def _ensure_endpoint_provider_columns() -> None:
    """Add Endpoint.provider and github_domain on databases created before those columns existed."""
    if not config.DB_URL.startswith("sqlite"):
        return
    with engine.connect() as conn:
        cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(endpoint)").fetchall()]
        if "provider" not in cols:
            conn.exec_driver_sql("ALTER TABLE endpoint ADD COLUMN provider VARCHAR DEFAULT 'openai'")
        if "github_domain" not in cols:
            conn.exec_driver_sql("ALTER TABLE endpoint ADD COLUMN github_domain VARCHAR DEFAULT ''")
        if "models_json" not in cols:
            conn.exec_driver_sql("ALTER TABLE endpoint ADD COLUMN models_json VARCHAR DEFAULT ''")
        if "models_fetched_at" not in cols:
            conn.exec_driver_sql("ALTER TABLE endpoint ADD COLUMN models_fetched_at DATETIME")
        conn.commit()



def _ensure_router_columns() -> None:
    """Router rules, local-only bots, and route labels on older databases."""
    if not config.DB_URL.startswith("sqlite"):
        return
    with engine.connect() as conn:
        ep_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(endpoint)").fetchall()]
        if "rules_json" not in ep_cols:
            conn.exec_driver_sql("ALTER TABLE endpoint ADD COLUMN rules_json VARCHAR DEFAULT ''")
        bot_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(bot)").fetchall()]
        if "local_only" not in bot_cols:
            conn.exec_driver_sql("ALTER TABLE bot ADD COLUMN local_only BOOLEAN NOT NULL DEFAULT 0")
        usage_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(llmusage)").fetchall()]
        if "route" not in usage_cols:
            conn.exec_driver_sql("ALTER TABLE llmusage ADD COLUMN route VARCHAR DEFAULT ''")
        if "route_reason" not in usage_cols:
            conn.exec_driver_sql("ALTER TABLE llmusage ADD COLUMN route_reason VARCHAR DEFAULT ''")
        msg_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(message)").fetchall()]
        if "route" not in msg_cols:
            conn.exec_driver_sql("ALTER TABLE message ADD COLUMN route VARCHAR DEFAULT ''")
        if "route_reason" not in msg_cols:
            conn.exec_driver_sql("ALTER TABLE message ADD COLUMN route_reason VARCHAR DEFAULT ''")
        conn.commit()


def _ensure_mcp_auth_columns() -> None:
    """Add McpServer auth columns on databases created before per-user MCP auth existed."""
    if not config.DB_URL.startswith("sqlite"):
        return
    with engine.connect() as conn:
        cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(mcpserver)").fetchall()]
        adds = {
            "auth_mode": "VARCHAR NOT NULL DEFAULT 'none'",
            "auth_scope": "VARCHAR NOT NULL DEFAULT 'shared'",
            "auth_header": "VARCHAR NOT NULL DEFAULT 'Authorization'",
            "auth_prefix": "VARCHAR NOT NULL DEFAULT 'Bearer'",
            "token_enc": "VARCHAR NOT NULL DEFAULT ''",
        }
        oauth_adds = {
            "oauth_authorize_url": "VARCHAR NOT NULL DEFAULT ''",
            "oauth_token_url": "VARCHAR NOT NULL DEFAULT ''",
            "oauth_client_id": "VARCHAR NOT NULL DEFAULT ''",
            "oauth_client_secret_enc": "VARCHAR NOT NULL DEFAULT ''",
            "oauth_scopes": "VARCHAR NOT NULL DEFAULT ''",
        }
        for name, decl in {**adds, **oauth_adds}.items():
            if name not in cols:
                conn.exec_driver_sql(f"ALTER TABLE mcpserver ADD COLUMN {name} {decl}")
        cred_tbl = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mcpcredential'").fetchall()
        if cred_tbl:
            cred_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(mcpcredential)").fetchall()]
            if "refresh_enc" not in cred_cols:
                conn.exec_driver_sql("ALTER TABLE mcpcredential ADD COLUMN refresh_enc VARCHAR NOT NULL DEFAULT ''")
            if "expires_at" not in cred_cols:
                conn.exec_driver_sql("ALTER TABLE mcpcredential ADD COLUMN expires_at DATETIME")
        conn.commit()


def _ensure_costs_columns() -> None:
    """Add project budget columns on databases created before cost tracking existed."""
    if not config.DB_URL.startswith("sqlite"):
        return
    with engine.connect() as conn:
        cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(team)").fetchall()]
        if "monthly_budget" not in cols:
            conn.exec_driver_sql("ALTER TABLE team ADD COLUMN monthly_budget FLOAT")
        if "budget_currency" not in cols:
            conn.exec_driver_sql("ALTER TABLE team ADD COLUMN budget_currency VARCHAR DEFAULT 'USD'")
        if "budget_block" not in cols:
            conn.exec_driver_sql("ALTER TABLE team ADD COLUMN budget_block BOOLEAN NOT NULL DEFAULT 0")
        conn.commit()


def _sqlite_path():
    if not config.DB_URL.startswith("sqlite"):
        return None
    # sqlite:////abs or sqlite:///rel
    return Path(config.DB_URL.split("sqlite:///", 1)[1])


def migrate_channel_bots() -> dict:
    """One-time: every project bot joins every project channel, and they reply by default.

    Runs only once (flag in AppSetting). Does not re-add a bot that was later removed.
    """
    out = {"added": 0, "already": True}
    with Session(engine) as s:
        flag = s.get(AppSetting, "channel_bots_autojoin_v1")
        if flag and flag.value == "done":
            return out
        path = _sqlite_path()
        if path and path.exists():
            bak = path.with_name(path.name + ".bak-channel-bots")
            if not bak.exists():
                import shutil
                shutil.copy2(path, bak)
        out["already"] = False
        channels = s.exec(select(Conversation).where(Conversation.kind == "channel")).all()
        added = 0
        for ch in channels:
            for bot in s.exec(select(Bot).where(Bot.team_id == ch.team_id)).all():
                row = s.exec(select(ChannelMember).where(
                    ChannelMember.conversation_id == ch.id, ChannelMember.bot_id == bot.id)).first()
                if row:
                    if row.mention_only:
                        row.mention_only = False
                        s.add(row)
                else:
                    s.add(ChannelMember(conversation_id=ch.id, bot_id=bot.id, mention_only=False))
                    added += 1
        s.add(AppSetting(key="channel_bots_autojoin_v1", value="done"))
        s.commit()
        out["added"] = added
    return out


def session() -> Session:
    return Session(engine, expire_on_commit=False)


__all__ = [n for n in dir() if not n.startswith("_")] + ["select"]
