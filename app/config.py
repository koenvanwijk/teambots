"""Runtime configuration, read from environment variables."""
import base64
import hashlib
import os

from cryptography.fernet import Fernet

DATA_DIR = os.environ.get("TB_DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_URL = os.environ.get("TB_DB_URL", f"sqlite:///{DATA_DIR}/teambots.db")
SECRET_KEY = os.environ.get("TB_SECRET_KEY", "dev-insecure-change-me")
# "docker" = one container per sandbox key; "local" = plain subprocess (development only).
SANDBOX_BACKEND = os.environ.get("TB_SANDBOX", "local")
SANDBOX_IMAGE = os.environ.get("TB_SANDBOX_IMAGE", "teambots-sandbox:latest")
SANDBOX_NETWORK = os.environ.get("TB_SANDBOX_NETWORK", "bridge")
TIMEZONE = os.environ.get("TB_TIMEZONE", "Europe/Amsterdam")
SANDBOX_BROWSER = os.environ.get("TB_BROWSER", "google-chrome")
# http_request may only reach public addresses unless this is set (development, internal APIs).
HTTP_ALLOW_PRIVATE = os.environ.get("TB_HTTP_ALLOW_PRIVATE", "0") == "1"
MAX_STEPS = int(os.environ.get("TB_MAX_STEPS", "12"))
LLM_TIMEOUT = float(os.environ.get("TB_LLM_TIMEOUT", "180"))
# Completion budget. Thinking models spend tokens on reasoning_content first;
# too low a cap yields an empty visible answer.
LLM_MAX_TOKENS = int(os.environ.get("TB_MAX_TOKENS", "8192"))

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

_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest()))


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode() if value else ""


def decrypt(value: str) -> str:
    return _fernet.decrypt(value.encode()).decode() if value else ""
