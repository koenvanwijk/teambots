"""Data model. One SQLite/Postgres database; every table is a SQLModel class."""
from datetime import datetime, timezone
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


class AuthToken(SQLModel, table=True):
    token: str = Field(primary_key=True)
    user_id: int = Field(index=True)


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


class BotAccess(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(index=True)
    user_id: int = Field(index=True)
    role: str = "collaborator"  # manager | collaborator


class Conversation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    team_id: int = Field(index=True)
    kind: str  # dm | channel | feed (shared activity feed of one Team Bot)
    name: str = ""
    share_activity: bool = True  # dm only: summaries of finished tasks go to the bot's feed
    user_id: Optional[int] = Field(default=None, index=True)  # dm only
    bot_id: Optional[int] = Field(default=None, index=True)  # dm only


class ChannelMember(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(index=True)
    user_id: Optional[int] = None
    bot_id: Optional[int] = None


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


def session() -> Session:
    return Session(engine, expire_on_commit=False)


__all__ = [n for n in dir() if not n.startswith("_")] + ["select"]
