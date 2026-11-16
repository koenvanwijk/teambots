"""Budgeted memory, Hermes-style: small, curated, edited by the bot itself.

Two stores, both rendered into every system prompt with a usage meter:

* team memory: facts for everyone in the project (per project, shared by all its bots);
* user profile: what matters about one person in this project (only shown in their private chats).

Each store has a hard character budget. A change is a batch of add / patch / remove operations
that is applied as a whole and checked against the budget on the final result, so a bot can make
room and add something in one call. When a store is full the add is refused and the current
entries are shown, so the bot consolidates instead of silently losing the oldest facts.
"""
from __future__ import annotations

from sqlmodel import Session, select

from .db import Bot, Memory

TEAM_LIMIT = 3000
USER_LIMIT = 1500
LIMITS = {"team": TEAM_LIMIT, "user": USER_LIMIT}
TITLES = {"team": "TEAM MEMORY (shared by everyone in this project)", "user": "USER PROFILE"}


def scope_of(target: str) -> str:
    return "team" if target == "team" else "personal"


def entries(s: Session, bot: Bot, target: str, user_id: int | None = None) -> list[Memory]:
    """Active entries of one store, oldest first. The overview bot (no project) keeps its own."""
    q = select(Memory).where(Memory.team_id == (bot.team_id or 0), Memory.scope == scope_of(target),
                             Memory.status == "active")
    if not bot.team_id:
        q = q.where(Memory.bot_id == bot.id)
    if target == "user":
        q = q.where(Memory.user_id == user_id)
    return list(s.exec(q.order_by(Memory.id)).all())


def used(rows: list[Memory]) -> int:
    return sum(len(m.content) for m in rows)


def meter(chars: int, limit: int) -> str:
    pct = min(100, int(chars * 100 / limit)) if limit else 0
    return f"{pct}% — {chars:,}/{limit:,} chars"


def render(rows: list[Memory], target: str) -> str:
    """The prompt block. Over budget (old data), the newest entries that fit are shown with a note."""
    if not rows:
        return ""
    limit = LIMITS[target]
    total = used(rows)
    shown, size = [], 0
    for m in reversed(rows):
        if size + len(m.content) > limit and shown:
            break
        shown.append(m)
        size += len(m.content)
    shown.reverse()
    lines = [f"## {TITLES[target]} [{meter(total, limit)}]"]
    lines += [f"- [{m.id}] {m.content}" for m in shown]
    if len(shown) < len(rows):
        lines.append(f"({len(rows) - len(shown)} older entries are not shown: this memory is over its budget. "
                     "Consolidate it with the `memory` tool: merge, shorten or remove entries.)")
    return "\n".join(lines)


def _find(rows: list[Memory], op: dict) -> tuple[Memory | None, str]:
    if op.get("id") not in (None, ""):
        try:
            mid = int(op["id"])
        except (TypeError, ValueError):
            return None, "id must be a number"
        hit = next((m for m in rows if m.id == mid), None)
        return (hit, "") if hit else (None, f"no entry with id {mid} in this memory")
    old = (op.get("old_text") or "").strip()
    if not old:
        return None, "pass id or old_text to say which entry"
    hits = [m for m in rows if old in m.content]
    if not hits:
        return None, f"no entry contains {old!r}"
    if len(hits) > 1:
        return None, f"{old!r} matches {len(hits)} entries; use a longer old_text or the id"
    return hits[0], ""


def _listing(rows: list[Memory], target: str) -> str:
    if not rows:
        return "(empty)"
    return "\n".join(f"- [{m.id}] {m.content}" for m in rows) + f"\n[{meter(used(rows), LIMITS[target])}]"


def apply(s: Session, bot: Bot, user_id: int, target: str, operations: list[dict]) -> str:
    """Apply a batch atomically. Returns a short result for the model (never raises for bad input)."""
    if target not in LIMITS:
        return "error: target must be 'team' or 'user'"
    if not isinstance(operations, list) or not operations:
        return "error: operations must be a non-empty list of {action, content?, id?, old_text?}"
    rows = entries(s, bot, target, user_id)
    # Work on a copy: (id or None, content, original row or None)
    work: list[list] = [[m.id, m.content, m] for m in rows]
    added = changed = removed = 0
    for i, op in enumerate(operations, 1):
        if not isinstance(op, dict):
            return f"error: operation {i} is not an object"
        action = (op.get("action") or "").strip().lower()
        content = " ".join(str(op.get("content") or "").split())
        if action == "add":
            if not content:
                return f"error: operation {i}: add needs content"
            if any(w[1] == content for w in work):
                continue
            work.append([None, content, None])
            added += 1
            continue
        if action not in ("patch", "remove", "replace", "delete"):
            return f"error: operation {i}: action must be add, patch or remove"
        live = [w[2] for w in work if w[2] is not None]
        hit, err = _find(live, op)
        if hit is None:
            # also allow old_text to match an entry added earlier in this batch
            old = (op.get("old_text") or "").strip()
            fresh = [w for w in work if w[2] is None and old and old in w[1]]
            if len(fresh) == 1:
                slot = fresh[0]
            else:
                return f"error: operation {i}: {err}\nCurrent entries:\n{_listing(rows, target)}"
        else:
            slot = next(w for w in work if w[2] is hit)
        if action in ("remove", "delete"):
            work.remove(slot)
            removed += 1
            continue
        if not content:
            return f"error: operation {i}: patch needs content"
        old = (op.get("old_text") or "").strip()
        slot[1] = slot[1].replace(old, content, 1) if old and old in slot[1] and op.get("id") in (None, "") \
            else content
        slot[1] = " ".join(slot[1].split())
        changed += 1
    limit = LIMITS[target]
    total = sum(len(w[1]) for w in work)
    if total > limit and total > used(rows):
        return (f"error: memory would be {total:,}/{limit:,} chars, over its budget. Nothing was saved. "
                "Send one batch that removes or shortens stale entries and adds the new one.\n"
                f"Current entries:\n{_listing(rows, target)}")
    keep = {id(w[2]) for w in work if w[2] is not None}
    for m in rows:
        if id(m) not in keep:
            s.delete(m)
    for w in work:
        if w[2] is None:
            s.add(Memory(bot_id=bot.id, team_id=bot.team_id or 0, scope=scope_of(target),
                         user_id=user_id if target == "user" else None, content=w[1], proposed_by=user_id))
        elif w[2].content != w[1]:
            w[2].content = w[1]
            s.add(w[2])
    s.commit()
    parts = [f"{n} {what}" for n, what in ((added, "added"), (changed, "changed"), (removed, "removed")) if n]
    return (f"{TITLES[target].split(' (')[0].lower()} updated: {', '.join(parts) or 'nothing changed'}. "
            f"Now {meter(total, limit)}. The change is visible from your next turn.")
