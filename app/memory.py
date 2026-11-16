"""Memory, profiles and skills on top of the knowledge base (app/knowledge.py).

* team memory: the files in ``memory/`` (the `memory` tool edits ``memory/notes.md``);
* user profile: ``users/<username>.md``, only shown in that person's private chats;
* skills: ``skills/*.md`` with optional front matter (name, description) above the steps.

What is injected into the prompt has a budget (team memory 3000, profile 1500 characters); the files
themselves may hold more, and the prompt then lists what it left out with a pointer to read it.
The `memory` and `skill_manage` tools are thin aliases: they read the file, change it and write it
back through the store with the hash they read, so every change is a git commit with its author.
"""
from __future__ import annotations

import logging
import re

from . import knowledge as kb
from .knowledge import Author, Conflict, KnowledgeError

log = logging.getLogger("teambots.memory")

TEAM_LIMIT = 3000
USER_LIMIT = 1500
INDEX_LIMIT = 1500
LIMITS = {"team": TEAM_LIMIT, "user": USER_LIMIT}
TEAM_FILE = "memory/notes.md"
CONTEXT_FILE = "AGENTS.md"
_BULLET = re.compile(r"^\s*[-*] ")


def meter(chars: int, limit: int) -> str:
    pct = min(100, int(chars * 100 / limit)) if limit else 0
    return f"{pct}% — {chars:,}/{limit:,} chars"


def _slug(text: str, fallback: str = "x") -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60] or fallback


def user_path(user) -> str:
    return f"users/{_slug(user.username, f'user-{user.id}')}.md"


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line and line != "---":
            return line[:100]
    return ""


def _cut(text: str, limit: int, path: str) -> str:
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.7), int(limit * 0.2)
    return (text[:head] + f"\n[... {len(text) - head - tail:,} chars left out; read all of it with "
            f"knowledge_read {path} ...]\n" + text[-tail:])


# ---------------------------------------------------------------- prompt blocks

def render_team(key: str) -> tuple[str, int]:
    """Team memory for the prompt, and how many characters the memory files hold in total."""
    paths = sorted(kb.files(key, "memory"), key=lambda p: (p != TEAM_FILE, p))
    texts = {p: kb.peek(key, p) or "" for p in paths}
    total = sum(len(t.strip()) for t in texts.values())
    head = f"## TEAM MEMORY (knowledge base memory/, shared by everyone in this project) [{meter(total, TEAM_LIMIT)}]"
    blocks, size, rest = [], 0, []
    for p in paths:
        body = texts[p].strip()
        if not body:
            continue
        block = f"### {p} (hash {kb.digest(texts[p])})\n{body}"
        if size + len(block) <= TEAM_LIMIT:
            blocks.append(block)
            size += len(block)
        elif not blocks:
            blocks.append(_cut(block, TEAM_LIMIT, p))
            size = TEAM_LIMIT
        else:
            rest.append(p)
    if not blocks:
        return head + "\n(empty)", total
    if rest:
        blocks.append("Not shown, over the budget (read with knowledge_read; consolidate when you can):\n" + "\n".join(
            f"- {p} ({len(texts[p]):,} chars): {_first_line(texts[p])}" for p in rest[:30]))
    return head + "\n" + "\n\n".join(blocks), total


def render_user(key: str, user) -> str:
    path = user_path(user)
    text = kb.peek(key, path)
    body = (text or "").strip()
    head = (f"## USER PROFILE of {user.display_name} (knowledge base {path}"
            + (f", hash {kb.digest(text)}" if text is not None else "") + f") [{meter(len(body), USER_LIMIT)}]")
    return head + "\n" + (_cut(body, USER_LIMIT, path) if body else "(empty)")


def render_index(key: str) -> str:
    """Pointers to the other files, so bots know what else the knowledge base holds."""
    other = [p for p in kb.files(key) if p != CONTEXT_FILE and not p.startswith(("memory/", "skills/", "users/"))]
    if not other:
        return ""
    rows, size = [], 0
    for p in other:
        row = f"- {p}: {_first_line(kb.peek(key, p) or '')}"
        if size + len(row) > INDEX_LIMIT:
            rows.append(f"(and {len(other) - len(rows)} more; see knowledge_list)")
            break
        rows.append(row)
        size += len(row)
    return "## Other files in the knowledge base\n" + "\n".join(rows)


# ---------------------------------------------------------------- the memory tool (alias)

def _bullets(lines: list[str]) -> list[int]:
    return [i for i, line in enumerate(lines) if _BULLET.match(line)]


def _listing(text: str, limit: int) -> str:
    lines = text.splitlines()
    rows = [f"[{n}] {lines[i].strip()[2:]}" for n, i in enumerate(_bullets(lines), 1)]
    return ("\n".join(rows) or "(no entries)") + f"\n[{meter(len(text.strip()), limit)}]"


def apply(key: str, user, target: str, operations: list[dict], author: Author, note: str = "") -> str:
    """Apply a batch of add / patch / remove to the team notes or the person's profile. Never raises."""
    if target not in LIMITS:
        return "error: target must be 'team' or 'user'"
    if not isinstance(operations, list) or not operations:
        return "error: operations must be a non-empty list of {action, content?, old_text?, id?}"
    path = TEAM_FILE if target == "team" else user_path(user)
    limit = LIMITS[target]
    kb.ensure(key)
    with kb._locks[key]:
        current = kb.peek(key, path)
        lines = (current or "").splitlines()
        if current is None:
            lines = ["# Team memory" if target == "team" else f"# Profile of {user.display_name}", ""]
        added = changed = removed = 0
        for i, op in enumerate(operations, 1):
            if not isinstance(op, dict):
                return f"error: operation {i} is not an object"
            action = (op.get("action") or "").strip().lower()
            content = " ".join(str(op.get("content") or "").split())
            entries = _bullets(lines)
            if action == "add":
                if not content:
                    return f"error: operation {i}: add needs content"
                if any(lines[j].strip()[2:] == content for j in entries):
                    continue
                lines.append(f"- {content}")
                added += 1
                continue
            if action not in ("patch", "remove", "replace", "delete"):
                return f"error: operation {i}: action must be add, patch or remove"
            old = (op.get("old_text") or "").strip()
            hit = None
            if op.get("id") not in (None, "") and not old:
                try:
                    n = int(op["id"])
                except (TypeError, ValueError):
                    return f"error: operation {i}: id must be the entry number"
                hit = entries[n - 1] if 1 <= n <= len(entries) else None
                err = f"there is no entry {op['id']}"
            elif old:
                hits = [j for j in entries if old in lines[j]]
                hit = hits[0] if len(hits) == 1 else None
                err = f"no entry contains {old!r}" if not hits else f"{old!r} matches {len(hits)} entries; be more specific"
            else:
                err = "pass old_text (or the entry number as id) to say which entry"
            if hit is None:
                return f"error: operation {i}: {err}\nCurrent entries:\n{_listing(current or '', limit)}"
            if action in ("remove", "delete"):
                del lines[hit]
                removed += 1
                continue
            if not content:
                return f"error: operation {i}: patch needs content"
            text = lines[hit].strip()[2:]
            text = text.replace(old, content, 1) if old and op.get("id") in (None, "") else content
            lines[hit] = "- " + " ".join(text.split())
            changed += 1
        new = "\n".join(lines).strip() + "\n"
        size, before = len(new.strip()), len((current or "").strip())
        if size > limit and size > before:
            return (f"error: {path} would be {size:,}/{limit:,} chars, over its budget. Nothing was saved. Send one "
                    "batch that removes or shortens stale entries and adds the new one, or move details to another "
                    f"file with knowledge_write.\nCurrent entries:\n{_listing(current or '', limit)}")
        if new == (current or ""):
            return f"{path}: nothing changed."
        parts = [f"{n} {what}" for n, what in ((added, "added"), (changed, "changed"), (removed, "removed")) if n]
        try:
            res = kb.write(key, path, new, kb.base_for(key, path, current), author,
                           f"memory: {', '.join(parts)} in {path}{note}")
        except Conflict as c:
            if kb.pinned.get() is not None:
                return "skipped: " + c.info["message"]
            return kb.describe(c)
    return (f"{path} updated: {', '.join(parts)}. Now {meter(size, limit)}, hash {res['hash']}. "
            "The change is visible from your next turn.")


# ---------------------------------------------------------------- skills

def parse_skill(path: str, text: str) -> dict:
    meta, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            for line in text[3:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip()
            body = text[end + 4:].lstrip("\n")
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if stem.upper() == "SKILL" and "/" in path[len("skills/"):]:
        stem = path.split("/")[-2]
    return {"name": meta.get("name") or stem, "description": meta.get("description", ""), "instructions": body,
            "path": path, "hash": kb.digest(text)}


def skills(key: str) -> list[dict]:
    return [parse_skill(p, kb.peek(key, p) or "") for p in kb.files(key, "skills") if p.endswith(".md")]


def find_skill(key: str, name: str) -> dict | None:
    name = (name or "").strip()
    if name.startswith("skills/"):
        return next((k for k in skills(key) if k["path"] == name), None)
    rows = skills(key)
    return next((k for k in rows if k["name"] == name), None) or next(
        (k for k in rows if name.lower() in (k["name"].lower(), k["path"].rsplit("/", 1)[-1][:-3].lower())), None)


def skill_text(name: str, description: str, instructions: str) -> str:
    desc = " ".join((description or "").split())
    return f"---\nname: {' '.join(name.split())}\ndescription: {desc}\n---\n\n{(instructions or '').strip()}\n"


def skill_manage(key: str, args: dict, author: Author, note: str = "") -> str:
    """create / patch / delete a skill file. Never raises."""
    action, name = (args.get("action") or "").lower(), (args.get("name") or "").strip()
    if not name:
        return "error: name is required"
    kb.ensure(key)
    sk = find_skill(key, name)
    try:
        with kb._locks[key]:
            if action == "create":
                if not (args.get("instructions") or "").strip():
                    return "error: create needs instructions"
                path = sk["path"] if sk else f"skills/{_slug(name, 'skill')}.md"
                current = kb.peek(key, path)
                text = skill_text(sk["name"] if sk else name, args.get("description") or (sk or {}).get("description", ""),
                                  args["instructions"])
                res = kb.write(key, path, text, kb.base_for(key, path, current), author,
                               f"skill: {'update' if current is not None else 'create'} {path}{note}")
                return f"skill '{name}' saved in {path} (hash {res['hash']})"
            if not sk:
                return f"error: no skill named {name!r}"
            path, current = sk["path"], kb.peek(key, sk["path"])
            if action == "delete":
                kb.delete(key, path, kb.base_for(key, path, current), author, f"skill: delete {path}{note}")
                return f"skill '{name}' deleted"
            if action != "patch":
                return "error: action must be create, patch or delete"
            instructions, description = sk["instructions"], sk["description"]
            old, new = args.get("old_text"), args.get("new_text")
            if old:
                if new is None:
                    return "error: patch with old_text needs new_text"
                n = instructions.count(old)
                if n != 1:
                    return f"error: old_text must occur exactly once in the instructions (found {n} times)"
                instructions = instructions.replace(old, new, 1)
            if args.get("instructions"):
                instructions = args["instructions"]
            if args.get("description"):
                description = args["description"]
            if not (old or args.get("instructions") or args.get("description")):
                return "error: patch needs old_text and new_text, or a new description or instructions"
            res = kb.write(key, path, skill_text(sk["name"], description, instructions),
                           kb.base_for(key, path, current), author, f"skill: edit {path}{note}")
            return f"skill '{sk['name']}' updated (hash {res['hash']})"
    except Conflict as c:
        if kb.pinned.get() is not None:
            return "skipped: " + c.info["message"]
        return kb.describe(c)
    except KnowledgeError as e:
        return f"error: {e}"


# ---------------------------------------------------------------- migration from the old tables

def legacy_files(key: str) -> dict[str, str]:
    """The old database memory and skills of one knowledge base, as files (imported once)."""
    from .db import Bot, Memory, Skill, User, select, session
    with session() as s:
        if key.startswith("p"):
            tid = int(key[1:])
            mem = list(s.exec(select(Memory).where(Memory.team_id == tid).order_by(Memory.id)).all())
            bots = list(s.exec(select(Bot).where(Bot.team_id == tid).order_by(Bot.id)).all())
        else:
            b = s.get(Bot, int(key[1:]))
            bots = [b] if b else []
            mem = list(s.exec(select(Memory).where(Memory.team_id == 0, Memory.bot_id == int(key[1:]))
                              .order_by(Memory.id)).all())
        users = {u.id: u for u in s.exec(select(User)).all()}
        sk_rows = [(b, k) for b in bots for k in s.exec(select(Skill).where(Skill.bot_id == b.id)
                                                         .order_by(Skill.id)).all()]
    out: dict[str, str] = {}
    team = [m.content for m in mem if m.scope == "team" and m.status == "active"]
    if team:
        out[TEAM_FILE] = "# Team memory\n\n" + "\n".join(f"- {c}" for c in team) + "\n"
    pending = [m for m in mem if m.status != "active"]
    if pending:
        out["inbox/proposed-memory.md"] = (
            "# Proposed memory\n\nSuggested by the old learning review and never accepted. Move what is right "
            "into memory/notes.md or a profile, then delete this file.\n\n"
            + "\n".join(f"- ({'team' if m.scope == 'team' else 'profile of ' + getattr(users.get(m.user_id), 'display_name', '?')}) "
                        f"{m.content}" for m in pending) + "\n")
    by_user: dict[int, list[str]] = {}
    for m in mem:
        if m.scope != "team" and m.status == "active" and m.user_id in users:
            by_user.setdefault(m.user_id, []).append(m.content)
    for uid, rows in by_user.items():
        u = users[uid]
        out[user_path(u)] = f"# Profile of {u.display_name}\n\n" + "\n".join(f"- {c}" for c in rows) + "\n"
    for b, k in sk_rows:
        text = skill_text(k.name, k.description, k.instructions)
        path = f"skills/{_slug(k.name, 'skill')}.md"
        if path in out and out[path] != text:
            path = f"skills/{_slug(k.name, 'skill')}-{_slug(b.handle, str(b.id))}.md"
        out[path] = text
    return out


def migrate_all() -> int:
    """Create every knowledge base at startup (idempotent: an existing repository is left alone)."""
    from .db import Bot, Memory, Skill, Team, select, session
    with session() as s:
        keys = [kb.key_for_team(t.id) for t in s.exec(select(Team)).all()]
        homes = {m.bot_id for m in s.exec(select(Memory).where(Memory.team_id == 0)).all()}
        homes |= {k.bot_id for k in s.exec(select(Skill)).all()
                  if (b := s.get(Bot, k.bot_id)) is not None and not b.team_id}
    keys += [f"b{i}" for i in sorted(homes)]
    for key in keys:
        try:
            kb.ensure(key)
        except Exception as e:  # noqa: BLE001 - one broken repository must not stop the app
            log.warning("knowledge base %s not ready: %s", key, e)
    return len(keys)
