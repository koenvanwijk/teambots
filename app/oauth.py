"""Sign in with GitHub or Google.

One identity per provider, several identities per user. Membership stays per project
(TeamMember / BotAccess), so one person can work in projects of different clients.
"""
import base64
import json
import time
from urllib.parse import urlencode

import httpx

from . import config

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GITHUB_AUTH = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"

NAMES = {"google": "Google", "github": "GitHub"}


class OAuthError(Exception):
    """A sign-in attempt that cannot continue; the message is safe to show to the user."""


def enabled() -> dict:
    return {
        "google": bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET),
        "github": bool(config.GITHUB_CLIENT_ID and config.GITHUB_CLIENT_SECRET),
    }


def providers_public() -> list:
    on = enabled()
    return [{"id": p, "name": NAMES[p]} for p in ("google", "github") if on[p]]


def redirect_uri(provider: str) -> str:
    if not config.PUBLIC_URL:
        raise OAuthError("TB_PUBLIC_URL is not set, so the OAuth redirect URI is unknown.")
    return f"{config.PUBLIC_URL}/api/auth/{provider}/callback"


def authorize_url(provider: str, state: str) -> str:
    if not enabled().get(provider):
        raise OAuthError(f"{NAMES.get(provider, provider)} sign-in is not configured.")
    ru = redirect_uri(provider)
    if provider == "google":
        q = urlencode({
            "client_id": config.GOOGLE_CLIENT_ID,
            "redirect_uri": ru,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "prompt": "select_account",
        })
        return f"{GOOGLE_AUTH}?{q}"
    q = urlencode({
        "client_id": config.GITHUB_CLIENT_ID,
        "redirect_uri": ru,
        "scope": "read:user user:email",
        "state": state,
    })
    return f"{GITHUB_AUTH}?{q}"


def exchange_code(provider: str, code: str) -> dict:
    """Trade an authorization code for a verified identity: {subject, email, name}."""
    if not enabled().get(provider):
        raise OAuthError(f"{NAMES.get(provider, provider)} sign-in is not configured.")
    try:
        if provider == "google":
            return _google_identity(code)
        if provider == "github":
            return _github_identity(code)
    except httpx.HTTPError as e:
        raise OAuthError("Could not reach the sign-in provider. Try again.") from e
    raise OAuthError("Unknown sign-in provider.")


def _google_identity(code: str) -> dict:
    with httpx.Client(timeout=20) as c:
        r = c.post(GOOGLE_TOKEN, data={
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri("google"),
        })
    if r.status_code != 200:
        raise OAuthError("Google rejected the sign-in. Try again.")
    body = r.json()
    id_token = body.get("id_token")
    if not id_token:
        raise OAuthError("Google did not return an identity token.")
    # The token came straight from Google's token endpoint over TLS, so the signature does not
    # need to be verified; the claims below are what matters.
    claims = _jwt_payload(id_token)
    if claims.get("aud") != config.GOOGLE_CLIENT_ID:
        raise OAuthError("Google returned a token for another client.")
    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise OAuthError("Google returned a token from an unexpected issuer.")
    if int(claims.get("exp", 0)) < time.time():
        raise OAuthError("Google returned an expired token.")
    if not claims.get("email_verified"):
        raise OAuthError("Your Google account has no verified email address.")
    email = (claims.get("email") or "").lower()
    if not email or not claims.get("sub"):
        raise OAuthError("Google did not return an email address.")
    return {"subject": str(claims["sub"]), "email": email,
            "name": claims.get("name") or email.split("@")[0]}


def _github_identity(code: str) -> dict:
    with httpx.Client(timeout=20, headers={"Accept": "application/json",
                                           "User-Agent": "TeamBots"}) as c:
        r = c.post(GITHUB_TOKEN, data={
            "client_id": config.GITHUB_CLIENT_ID,
            "client_secret": config.GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": redirect_uri("github"),
        })
        if r.status_code != 200 or "access_token" not in r.json():
            raise OAuthError("GitHub rejected the sign-in. Try again.")
        token = r.json()["access_token"]
        h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        me = c.get(f"{GITHUB_API}/user", headers=h)
        if me.status_code != 200:
            raise OAuthError("Could not read your GitHub profile.")
        profile = me.json()
        emails = c.get(f"{GITHUB_API}/user/emails", headers=h)
        email = ""
        if emails.status_code == 200:
            rows = emails.json()
            primary = next((e for e in rows if e.get("primary") and e.get("verified")), None)
            verified = primary or next((e for e in rows if e.get("verified")), None)
            email = (verified or {}).get("email", "").lower()
    if not profile.get("id"):
        raise OAuthError("GitHub did not return an account id.")
    if not email:
        raise OAuthError("Your GitHub account has no verified email address. Verify one on GitHub "
                         "or sign in with Google.")
    return {"subject": str(profile["id"]), "email": email,
            "name": profile.get("name") or profile.get("login") or email.split("@")[0]}


def _jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except (IndexError, ValueError) as e:
        raise OAuthError("Could not read the identity token.") from e
