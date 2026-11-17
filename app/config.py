"""Runtime configuration, read from environment variables."""
import base64
import hashlib
import os

from cryptography.fernet import Fernet

DATA_DIR = os.environ.get("TB_DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_URL = os.environ.get("TB_DB_URL", f"sqlite:///{DATA_DIR}/teambots.db")
# Where per-project export archives (scripts/export_project.py) live; the UI lists and serves them.
EXPORTS_DIR = os.environ.get("TB_EXPORTS_DIR",
                             os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "exports"))
SECRET_KEY = os.environ.get("TB_SECRET_KEY", "dev-insecure-change-me")
# "docker" = one container per sandbox key; "local" = plain subprocess (development only).
SANDBOX_BACKEND = os.environ.get("TB_SANDBOX", "local")
SANDBOX_IMAGE = os.environ.get("TB_SANDBOX_IMAGE", "teambots-sandbox:latest")
SANDBOX_NETWORK = os.environ.get("TB_SANDBOX_NETWORK", "bridge")
# Container runtime for sandboxes, e.g. "runsc" (gVisor). Empty = Docker's default (runc).
SANDBOX_RUNTIME = os.environ.get("TB_SANDBOX_RUNTIME", "")
# Host path of a resolv.conf mounted into sandboxes. Needed with gVisor on a user-defined Docker network:
# Docker's embedded DNS (127.0.0.11) relies on iptables inside the container, which gVisor does not apply.
SANDBOX_RESOLV_CONF = os.environ.get("TB_SANDBOX_RESOLV_CONF", "")
# Unix socket of sandboxd (app/sandboxd.py). When set, the app has no Docker access itself and asks
# sandboxd to run commands in sandboxes; only sandboxd holds the Docker socket.
SANDBOXD_SOCKET = os.environ.get("TB_SANDBOXD_SOCKET", "")
TIMEZONE = os.environ.get("TB_TIMEZONE", "Europe/Amsterdam")
SANDBOX_BROWSER = os.environ.get("TB_BROWSER", "google-chrome")
# http_request may only reach public addresses unless this is set (development, internal APIs).
HTTP_ALLOW_PRIVATE = os.environ.get("TB_HTTP_ALLOW_PRIVATE", "0") == "1"
# A Git credential is more powerful than a typical API key.  Keep pushes to public
# HTTPS hosts by default even on deployments that let http_request reach internal
# services.  Set this only for a deliberately trusted self-hosted Git service.
GIT_PUSH_ALLOW_PRIVATE = os.environ.get("TB_GIT_PUSH_ALLOW_PRIVATE", "0") == "1"
MAX_STEPS = int(os.environ.get("TB_MAX_STEPS", "40"))
# Background learning: after some turns a cheap side call (Spark when available) saves memory and
# skills, and long chats get a rolling summary of what fell out of the history. "0" turns both off.
LEARNING = os.environ.get("TB_LEARNING", "1") != "0"
LLM_TIMEOUT = float(os.environ.get("TB_LLM_TIMEOUT", "180"))
MAX_UPLOAD_MB = int(os.environ.get("TB_MAX_UPLOAD_MB", "25"))  # largest file a person can upload
# Completion budget. Thinking models spend tokens on reasoning_content first;
# too low a cap yields an empty visible answer.
LLM_MAX_TOKENS = int(os.environ.get("TB_MAX_TOKENS", "8192"))

# --- Claude CLI provider. Shells out to the local `claude` binary in
# headless mode; it authenticates with its own stored Claude Code credentials, so
# the endpoint carries no API key. CLAUDE_CLI_CWD is a neutral directory kept apart
# from any repo so the CLI does not pick up a project's CLAUDE.md, settings or hooks.
CLAUDE_CLI_BIN = os.environ.get("TB_CLAUDE_CLI", "claude")
CLAUDE_CLI_CWD = os.environ.get("TB_CLAUDE_CLI_CWD", os.path.join(DATA_DIR, "claude-cli-cwd"))

# --- Sign in with GitHub or Google (OAuth2 / OIDC).
# A provider is enabled when both its client id and secret are set.
# TB_PUBLIC_URL is the browser-facing base URL; it builds the OAuth redirect URI and must match
# what you registered with the provider (e.g. https://bots.example.com).
PUBLIC_URL = os.environ.get("TB_PUBLIC_URL", "").rstrip("/")
GOOGLE_CLIENT_ID = os.environ.get("TB_GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("TB_GOOGLE_CLIENT_SECRET", "")
GITHUB_CLIENT_ID = os.environ.get("TB_GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("TB_GITHUB_CLIENT_SECRET", "")
# Only these emails or @domains may create a new account through OAuth. Existing users and
# already-linked identities can always sign in. Empty list = no automatic account creation.
OAUTH_ALLOWLIST = [x.strip().lower() for x in os.environ.get("TB_OAUTH_ALLOWLIST", "").split(",") if x.strip()]
# Login tokens (password or OAuth) expire after this many days. 0 = never. Tokens created before
# this setting existed have no timestamp and stay valid.
TOKEN_TTL_DAYS = int(os.environ.get("TB_TOKEN_TTL_DAYS", "30"))
# Once a first user exists, POST /api/register requires a valid project invite
# code unless this is set. The first account can always be created.
OPEN_REGISTRATION = os.environ.get("TB_OPEN_REGISTRATION", "0") == "1"
# Serve /docs, /redoc and /openapi.json. Off by default so a public URL does
# not advertise the API.
ENABLE_DOCS = os.environ.get("TB_ENABLE_DOCS", "0") == "1"
# Max /api/login and /api/register attempts per IP per minute. 0 disables.
AUTH_RATE_LIMIT = int(os.environ.get("TB_AUTH_RATE_LIMIT", "10"))

_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest()))


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode() if value else ""


def decrypt(value: str) -> str:
    return _fernet.decrypt(value.encode()).decode() if value else ""
