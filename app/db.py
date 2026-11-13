"""Data model. One SQLite/Postgres database; every table is a SQLModel class."""
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine, select

from . import config


def now() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    display_name: str
    password_hash: str
    home_endpoint_id: Optional[int] = None  # model endpoint for the overview bot
    home_model: str = ""  # empty = endpoint default


class AuthToken(SQLModel, table=True):
    token: str = Field(primary_key=True)
    user_id: int = Field(index=True)


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
    base_url: str
    api_key_enc: str = ""
    default_model: str


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
    share_activity: bool = True  # dm only: summaries of finished tasks go to the bot's feed
    user_id: Optional[int] = Field(default=None, index=True)  # dm only
    bot_id: Optional[int] = Field(default=None, index=True)  # dm only


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
    headers_enc: str = ""  # JSON object, encrypted
    trusted: bool = False  # trusted servers skip the approval gate


_connect_args = {"check_same_thread": False} if config.DB_URL.startswith("sqlite") else {}
engine = create_engine(config.DB_URL, connect_args=_connect_args)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _ensure_channelmember_mention_only()
    _ensure_home_columns()
    migrate_channel_bots()
    from .home import migrate_home_bots
    migrate_home_bots()


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
