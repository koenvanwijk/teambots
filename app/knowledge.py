"""The knowledge base of a project: a directory tree of text files in a git repository.

Layout (all optional; people and bots may add any directories and files):

    AGENTS.md            project context, read by every bot of the project
    memory/*.md          team memory notes (memory/notes.md is what the `memory` tool edits)
    users/<username>.md  the profile of one person in this project
    skills/<name>.md     a skill: optional front matter (name, description) and the steps

Every change, from the UI, the API, a bot tool or the learning review, is one git commit authored by
the person ("Koen <koen@…>") or the bot ("Helper (bot)"), so history shows who changed what.

Writes use optimistic concurrency. Reading a file returns its hash (the first 16 hex characters of
the sha256 of its content). A write, patch, delete or revert must pass the hash it is based on as
``base_hash``; creating a file passes ``None`` and requires that it does not exist yet. When the file
changed in between, the write is refused with a ``Conflict`` that says who changed it last and shows a
diff, so a bot knows it would overwrite a person's (or another bot's) work.

The overview bot has no project; its knowledge base belongs to that bot (key ``b<bot id>``).
"""
from __future__ import annotations

import contextvars
import difflib
import hashlib
import os
import re
import subprocess
import threading
from collections import defaultdict
from dataclasses import dataclass

from . import config

MAX_FILE_CHARS = 200_000
MAX_DEPTH = 8
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._@+-]{0,99}")
_KEY = re.compile(r"[pb][0-9]+")
KEEP = ".gitkeep"  # git does not track empty directories
_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
_ready: set[str] = set()


class KnowledgeError(ValueError):
    """Bad input (path, missing file, …). The message is meant for the person or the model."""


class Conflict(Exception):
    """The file changed since the version the caller based its change on."""

    def __init__(self, info: dict):
        super().__init__(info.get("message", "conflict"))
        self.info = info


@dataclass(frozen=True)
class Author:
    name: str
    email: str

    def label(self) -> str:
        return self.name


SYSTEM = Author("TeamBots", "system@teambots.local")
MIGRATION = Author("TeamBots migration", "migration@teambots.local")
LEARNING = Author("learning-review (bot)", "learning-review@bots.teambots.local")


def _clean_name(name: str) -> str:
    return re.sub(r"[<>\n\r\x00]", "", name or "").strip() or "unknown"


def user_author(user) -> Author:
    email = (user.email or "").strip() or f"{user.username}@users.teambots.local"
    return Author(_clean_name(user.display_name or user.username), _clean_name(email))


def bot_author(bot) -> Author:
    return Author(_clean_name(f"{bot.name} (bot)"), f"bot-{bot.id}@bots.teambots.local")


# While set (learning review), bot tool writes are authored as this instead of the bot.
acting_as: contextvars.ContextVar[Author | None] = contextvars.ContextVar("knowledge_acting_as", default=None)
# While set (learning review), aliases base their writes on this snapshot {path: hash} instead of the
# current file, so a change someone made after the snapshot is a conflict and the write is skipped.
pinned: contextvars.ContextVar[dict | None] = contextvars.ContextVar("knowledge_pinned", default=None)


# ---------------------------------------------------------------- keys, paths, hashes

def key_for_team(team_id: int) -> str:
    return f"p{int(team_id)}"


def key_for(bot) -> str:
    return key_for_team(bot.team_id) if bot.team_id else f"b{bot.id}"


def root(key: str) -> str:
    if not _KEY.fullmatch(key or ""):
        raise KnowledgeError("bad knowledge base key")
    return os.path.abspath(os.path.join(config.DATA_DIR, "knowledge", key))


def clean(path: str, allow_root: bool = False) -> str:
    """A safe relative path: no '..', no hidden or absolute parts, at most MAX_DEPTH levels."""
    p = (path or "").replace("\\", "/").strip()
    if p.startswith("knowledge/"):  # bots sometimes prefix the directory name
        p = p[len("knowledge/"):]
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts:
        if allow_root:
            return ""
        raise KnowledgeError("path is required")
    if len(parts) > MAX_DEPTH:
        raise KnowledgeError(f"path is nested too deep (at most {MAX_DEPTH} levels)")
    for x in parts:
        if x == ".." or not _SEGMENT.fullmatch(x) or x.lower() == ".git":
            raise KnowledgeError(f"bad path {path!r}: use letters, digits, spaces and . _ - @ +, "
                                 "no leading dot and no '..'")
    return "/".join(parts)


def digest(content: str | None) -> str | None:
    if content is None:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def same_hash(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return not a and not b
    a, b = a.strip().lower(), b.strip().lower()
    n = min(len(a), len(b), 16)
    return n >= 8 and a[:n] == b[:n]


# ---------------------------------------------------------------- git

def _env(author: Author | None) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
           "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_TERMINAL_PROMPT": "0"}
    a = author or SYSTEM
    env.update(GIT_AUTHOR_NAME=a.name, GIT_AUTHOR_EMAIL=a.email, GIT_COMMITTER_NAME=a.name,
               GIT_COMMITTER_EMAIL=a.email)
    return env


def _git(key: str, *args: str, author: Author | None = None, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", root(key), "-c", "core.quotepath=off", "-c", "commit.gpgsign=false",
                        "-c", "core.hooksPath=" + os.devnull, *args],
                       capture_output=True, text=True, env=_env(author), timeout=30)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def _commit(key: str, paths: list[str], author: Author, message: str) -> str | None:
    _git(key, "add", "-A", "--", *paths)
    if subprocess.run(["git", "-C", root(key), "diff", "--cached", "--quiet"], env=_env(author)).returncode == 0:
        return None  # nothing changed
    _git(key, "commit", "-q", "-m", message[:200], author=author)
    return _git(key, "rev-parse", "HEAD").strip()


def ensure(key: str) -> str:
    """Create the repository on first use (importing the old database memory and skills once)."""
    base = root(key)
    if key in _ready and os.path.isdir(os.path.join(base, ".git")):
        return base
    with _locks[key]:
        if not os.path.isdir(os.path.join(base, ".git")):
            os.makedirs(base, exist_ok=True)
            _git(key, "init", "-q")
            _git(key, "symbolic-ref", "HEAD", "refs/heads/main")
            from .memory import legacy_files
            try:
                files = legacy_files(key) or {}
            except Exception:  # noqa: BLE001 - a failed import must not block the knowledge base
                files = {}
            for path, content in files.items():
                _write_raw(key, clean(path), content)
            if files:
                _commit(key, ["."], MIGRATION, "Import memory and skills from the database")
            else:
                _git(key, "commit", "-q", "--allow-empty", "-m", "Start the knowledge base", author=SYSTEM)
        _ready.add(key)
    return base


def _abs(key: str, path: str) -> str:
    full = os.path.abspath(os.path.join(root(key), path))
    if not (full == root(key) or full.startswith(root(key) + os.sep)):
        raise KnowledgeError("path escapes the knowledge base")
    return full


def _read_raw(key: str, path: str) -> str | None:
    full = _abs(key, path)
    if not os.path.isfile(full):
        return None
    with open(full, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def _write_raw(key: str, path: str, content: str) -> None:
    full = _abs(key, path)
    parent = os.path.dirname(full)
    if os.path.isfile(parent):
        raise KnowledgeError(f"{os.path.relpath(parent, root(key))} is a file, not a directory")
    os.makedirs(parent, exist_ok=True)
    tmp = full + ".tmp-tb"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, full)
    keep = os.path.join(parent, KEEP)  # the directory is no longer empty
    if os.path.exists(keep) and parent != root(key):
        os.remove(keep)


# ---------------------------------------------------------------- reading

def read(key: str, path: str) -> dict:
    ensure(key)
    path = clean(path)
    if os.path.isdir(_abs(key, path)):
        raise KnowledgeError(f"{path} is a directory; use knowledge_list")
    content = _read_raw(key, path)
    return {"path": path, "exists": content is not None, "content": content or "", "hash": digest(content),
            "size": len(content or "")}


def peek(key: str, path: str) -> str | None:
    """Content without creating the repository (prompt building); None when absent."""
    try:
        return _read_raw(key, clean(path))
    except (KnowledgeError, OSError):
        return None


def files(key: str, under: str = "", create: bool = True) -> list[str]:
    """All files (relative paths, sorted), optionally under a directory."""
    if create:
        ensure(key)
    base = root(key)
    start = _abs(key, clean(under, allow_root=True))
    if not os.path.isdir(start):
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for f in filenames:
            if not f.startswith(".") and not f.endswith(".tmp-tb"):
                out.append(os.path.relpath(os.path.join(dirpath, f), base).replace(os.sep, "/"))
    return sorted(out)


def tree(key: str) -> dict:
    """Every directory and file, for the UI and knowledge_list."""
    ensure(key)
    base = root(key)
    dirs, out = [], []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel = os.path.relpath(dirpath, base).replace(os.sep, "/")
        if rel != ".":
            dirs.append(rel)
        for f in sorted(filenames):
            if f.startswith(".") or f.endswith(".tmp-tb"):
                continue
            p = f if rel == "." else f"{rel}/{f}"
            c = _read_raw(key, p) or ""
            out.append({"path": p, "size": len(c), "hash": digest(c)})
    return {"dirs": sorted(dirs), "files": out}


def listing(key: str, path: str = "") -> str:
    """knowledge_list for the model: the directory, one entry per line, with sizes and hashes."""
    path = clean(path, allow_root=True)
    t = tree(key)
    pre = f"{path}/" if path else ""
    if path and path not in t["dirs"]:
        raise KnowledgeError(f"no directory {path!r}")
    lines = [f"{d}/" for d in t["dirs"] if d.startswith(pre)]
    lines += [f"{f['path']}  ({f['size']} chars, hash {f['hash']})" for f in t["files"] if f["path"].startswith(pre)]
    return "\n".join(sorted(lines)) or "(empty)"


# ---------------------------------------------------------------- history

_SEP = "\x1f"


def history(key: str, path: str = "", limit: int = 30) -> list[dict]:
    ensure(key)
    path = clean(path, allow_root=True)
    args = ["log", f"-n{max(1, min(int(limit or 30), 200))}", f"--format=%H{_SEP}%an{_SEP}%ae{_SEP}%aI{_SEP}%s"]
    if path:
        args += ["--follow", "--", path]
    out = []
    for line in _git(key, *args, check=False).splitlines():
        parts = line.split(_SEP)
        if len(parts) == 5:
            out.append({"commit": parts[0], "short": parts[0][:8], "author": parts[1], "email": parts[2],
                        "date": parts[3], "message": parts[4]})
    return out


def version(key: str, path: str, commit: str) -> str | None:
    """The file as it was in that commit; None when it did not exist there."""
    ensure(key)
    path = clean(path)
    if not re.fullmatch(r"[0-9a-f]{4,40}", commit or ""):
        raise KnowledgeError("commit must be a commit hash from the history")
    r = subprocess.run(["git", "-C", root(key), "show", f"{commit}:{path}"], capture_output=True, env=_env(None),
                       timeout=30)
    return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else None


def last_change(key: str, path: str) -> dict | None:
    h = history(key, path, 1)
    return h[0] if h else None


def _diff(old: str, new: str, path: str, limit: int = 4000) -> str:
    d = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                     f"a/{path} (the version you started from)", f"b/{path} (current)", n=2))
    return d if len(d) <= limit else d[:limit] + "\n[... diff cut off ...]"


def _conflict(key: str, path: str, base_hash: str | None, current: str | None) -> Conflict:
    last = last_change(key, path) if current is not None or base_hash else None
    base_text = None
    if base_hash:  # find the version the caller saw, to show what changed since
        for h in history(key, path, 50):
            old = version(key, path, h["commit"])
            if old is not None and same_hash(digest(old), base_hash):
                base_text = old
                break
    cur_hash = digest(current)
    if current is None:
        what = f"{path} was deleted"
    elif not base_hash:
        what = f"{path} already exists"
    else:
        what = f"{path} changed since you read it"
    who = f" by {last['author']} at {last['date']} ({last['message']})" if last else ""
    if base_text is not None or not base_hash:
        diff = _diff(base_text or "", current or "", path)
    else:
        diff = "(the version you based your change on is not in the history)\n" + (current or "")[:2000]
    msg = (f"conflict: {what}{who}. Nothing was written. Read the current version (hash {cur_hash}), merge your "
           "change into it and try again with that hash; do not overwrite someone else's change blindly.")
    return Conflict({"error": "conflict", "message": msg, "path": path, "base_hash": base_hash,
                     "current_hash": cur_hash, "exists": current is not None, "last_change": last, "diff": diff})


def describe(c: Conflict) -> str:
    return c.info["message"] + ("\n" + c.info["diff"] if c.info.get("diff") else "")


# ---------------------------------------------------------------- writing

def _check(key: str, path: str, base_hash: str | None) -> str | None:
    current = _read_raw(key, path)
    if base_hash in ("", None):
        if current is not None:
            raise _conflict(key, path, None, current)
    elif current is None or not same_hash(digest(current), base_hash):
        raise _conflict(key, path, base_hash, current)
    return current


def _limit(content: str) -> str:
    if not isinstance(content, str):
        raise KnowledgeError("content must be text")
    if len(content) > MAX_FILE_CHARS:
        raise KnowledgeError(f"file too large: {len(content):,} chars (at most {MAX_FILE_CHARS:,})")
    return content


def write(key: str, path: str, content: str, base_hash: str | None, author: Author,
          message: str = "") -> dict:
    """Create (base_hash None: must not exist) or replace (base_hash = hash you read) a file."""
    ensure(key)
    path = clean(path)
    content = _limit(content)
    with _locks[key]:
        if os.path.isdir(_abs(key, path)):
            raise KnowledgeError(f"{path} is a directory")
        current = _check(key, path, base_hash)
        _write_raw(key, path, content)
        commit = _commit(key, ["."], author, message or f"{'Update' if current is not None else 'Create'} {path}")
    return {"path": path, "hash": digest(content), "commit": commit, "created": current is None}


def patch(key: str, path: str, old: str, new: str, base_hash: str | None, author: Author,
          message: str = "") -> dict:
    """Replace one exact occurrence of old with new."""
    ensure(key)
    path = clean(path)
    if not old:
        raise KnowledgeError("old_text is required (the exact text to replace)")
    with _locks[key]:
        current = _read_raw(key, path)
        if current is None:
            raise KnowledgeError(f"no file {path!r}; create it with knowledge_write")
        if not base_hash:
            raise KnowledgeError("base_hash is required: read the file first and pass its hash")
        _check(key, path, base_hash)
        n = current.count(old)
        if n != 1:
            raise KnowledgeError(f"old_text must occur exactly once in {path} (found {n} times)")
        content = _limit(current.replace(old, new or "", 1))
        _write_raw(key, path, content)
        commit = _commit(key, ["."], author, message or f"Edit {path}")
    return {"path": path, "hash": digest(content), "commit": commit, "created": False}


def delete(key: str, path: str, base_hash: str | None, author: Author, message: str = "") -> dict:
    """Delete a file (base_hash required) or an empty directory."""
    ensure(key)
    path = clean(path)
    with _locks[key]:
        full = _abs(key, path)
        if os.path.isdir(full):
            if [f for f in os.listdir(full) if f != KEEP]:
                raise KnowledgeError(f"directory {path} is not empty")
            if os.path.exists(os.path.join(full, KEEP)):
                os.remove(os.path.join(full, KEEP))
            os.rmdir(full)
            commit = _commit(key, ["."], author, message or f"Delete directory {path}")
            return {"path": path, "hash": None, "commit": commit}
        if _read_raw(key, path) is None:
            raise KnowledgeError(f"no file {path!r}")
        if not base_hash:
            raise KnowledgeError("base_hash is required: read the file first and pass its hash")
        _check(key, path, base_hash)
        os.remove(full)
        parent = os.path.dirname(full)
        if parent != root(key) and not os.listdir(parent):  # keep the directory the person made
            open(os.path.join(parent, KEEP), "w").close()
        commit = _commit(key, ["."], author, message or f"Delete {path}")
    return {"path": path, "hash": None, "commit": commit}


def mkdir(key: str, path: str, author: Author) -> dict:
    ensure(key)
    path = clean(path)
    with _locks[key]:
        full = _abs(key, path)
        if os.path.exists(full):
            if os.path.isdir(full):
                return {"path": path, "commit": None}
            raise KnowledgeError(f"{path} is a file")
        os.makedirs(full)
        open(os.path.join(full, KEEP), "w").close()
        commit = _commit(key, ["."], author, f"Create directory {path}")
    return {"path": path, "commit": commit}


def revert(key: str, path: str, commit: str, base_hash: str | None, author: Author) -> dict:
    """Put a file back as it was in an earlier commit (deleting it if it did not exist then)."""
    old = version(key, path, commit)
    path = clean(path)
    msg = f"Revert {path} to {commit[:8]}"
    if old is None:
        return delete(key, path, base_hash, author, msg)
    return write(key, path, old, base_hash, author, msg)


def snapshot(key: str) -> dict[str, str]:
    """{path: hash} of every file, to pin a later batch of writes (learning review)."""
    return {p: digest(_read_raw(key, p)) for p in files(key)}


def base_for(key: str, path: str, current: str | None) -> str | None:
    """The base hash an alias writes against: the pinned snapshot when set, else the current file."""
    snap = pinned.get()
    if snap is not None:
        return snap.get(path)
    return digest(current)


def remove_all(key: str) -> None:
    import shutil
    with _locks[key]:
        shutil.rmtree(root(key), ignore_errors=True)
        _ready.discard(key)
