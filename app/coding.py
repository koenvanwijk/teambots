"""Personal coding bots: Claude Code running in the user's own sandbox (`home-u{owner_id}`).

This module holds the bot helpers. app.agent imports this module at its top, so anything here that
needs app.agent must import it lazily inside the function.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone

from . import config, sandbox, secrets_http, usage
from .db import Bot, CodingRun, Conversation, Message, Session, User, select, session

RUNNER = "claude_code"
DEFAULT_MODEL = "opus"
MODEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
TOOL_RESULT_MAX = 6000
CHUNK_MAX = 262144
NOT_LOGGED_IN = "Je bent nog niet ingelogd bij Claude. Gebruik Log in bij Claude bij de instellingen van deze bot."

# Runs `claude -p` detached in the sandbox. Every input is a positional argument; prompt and
# instructions come from files, so nothing the user typed is ever parsed by the shell.
RUN_SCRIPT = '''root="$PWD"; base="$root/$1"
export CLAUDE_CONFIG_DIR="$root/.config/claude"
cd "$root/${2:-.}" || { echo "workdir not found: $2" > "$base.err"; echo 127 > "$base.exit"; exit 0; }
"$5" -p --output-format stream-json --verbose --model "$3" --permission-mode bypassPermissions \\
  --append-system-prompt "$(cat "$base.system")" ${4:+--resume "$4"} \\
  < "$base.prompt" > "$base.jsonl" 2> "$base.err" &
echo $! > "$base.pid"
wait $!
echo $? > "$base.exit"
'''


def clean_workdir(raw: str) -> str:
    """Empty stays empty; otherwise a path relative to the workspace. Raises ValueError."""
    raw = (raw or "").strip()
    return sandbox._check_path(raw) if raw else ""


def _tool_result_text(content) -> str:
    """Tool output as plain text: list content joins its text parts, long output is cut short."""
    if isinstance(content, list):
        content = "\n".join(p["text"] for p in content
                            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
    text = content if isinstance(content, str) else ""
    if len(text) > TOOL_RESULT_MAX:
        text = text[:TOOL_RESULT_MAX] + "\n[... truncated ...]"
    return text


def parse_event(line: str) -> list[dict]:
    """One stream-json line from `claude -p --output-format stream-json` as display items. Unknown lines give []."""
    try:
        ev = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(ev, dict):
        return []
    kind = ev.get("type")
    if kind == "system" and ev.get("subtype") == "init":
        sid = ev.get("session_id")
        return [{"kind": "session", "session_id": sid if isinstance(sid, str) else ""}]
    msg = ev.get("message")
    blocks = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(blocks, list):
        blocks = []
    out = []
    if kind == "assistant":
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                text = b.get("text")
                out.append({"kind": "text", "text": text if isinstance(text, str) else ""})
            elif b.get("type") == "tool_use":
                out.append({"kind": "tool_call", "id": b.get("id", ""), "name": b.get("name", ""),
                            "arguments": json.dumps(b.get("input", {}))})
    elif kind == "user":
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                out.append({"kind": "tool_result", "id": b.get("tool_use_id", ""),
                            "content": _tool_result_text(b.get("content"))})
    elif kind == "result":
        try:
            cost = float(ev.get("total_cost_usd"))
        except (TypeError, ValueError):
            cost = None
        text = ev.get("result")
        usage = ev.get("usage")
        out.append({"kind": "result", "text": text if isinstance(text, str) else "",
                    "is_error": bool(ev.get("is_error")), "cost_usd": cost,
                    "usage": usage if isinstance(usage, dict) else {}})
    return out


def create_bot(s: Session, user: User, name: str, model: str = DEFAULT_MODEL, workdir: str = "",
               instructions: str = "") -> tuple[Bot, Conversation]:
    """Create a personal coding bot with its private chat. The caller commits."""
    bot = Bot(team_id=0, owner_id=user.id, name=name.strip(), handle="", instructions=instructions,
              endpoint_id=0, model=model, is_team=False, published=False, is_home=False,
              computer_use=True, runner=RUNNER, workdir=clean_workdir(workdir))
    s.add(bot)
    s.flush()
    bot.handle = f"code-{bot.id}"
    s.add(bot)
    conv = Conversation(team_id=0, kind="dm", name=bot.name, share_activity=False,
                        user_id=user.id, bot_id=bot.id)
    s.add(conv)
    s.flush()
    return bot, conv


def bot_conv(s: Session, bot: Bot) -> Conversation:
    """The owner's private chat with this coding bot (created when missing)."""
    conv = s.exec(select(Conversation).where(
        Conversation.bot_id == bot.id, Conversation.kind == "dm", Conversation.user_id == bot.owner_id)).first()
    if not conv:
        conv = Conversation(team_id=0, kind="dm", name=bot.name, share_activity=False,
                            user_id=bot.owner_id, bot_id=bot.id)
        s.add(conv)
        s.flush()
    return conv


def bot_out(bot: Bot, conv: Conversation) -> dict:
    return {"id": bot.id, "name": bot.name, "handle": bot.handle, "model": bot.model, "workdir": bot.workdir,
            "instructions": bot.instructions, "runner": bot.runner,
            "conversation": {"id": conv.id, "kind": conv.kind, "team_id": conv.team_id, "name": conv.name}}


# ---------------------------------------------------------------- running a turn

def _complete(data: bytes) -> tuple[bytes, int]:
    """The part of `data` up to and including the last newline, and its length in bytes."""
    n = data.rfind(b"\n") + 1
    return data[:n], n


async def logged_in(bot: Bot) -> bool:
    from .home import sandbox_key
    code, _ = await sandbox.sb_exec(sandbox_key(bot), ["test", "-s", ".config/claude/.credentials.json"])
    return code == 0


def _post_msg(conv_id: int, **fields):
    from . import agent
    return agent._post(conv_id, **fields)


async def run_turn(conv_id: int, bot_id: int, requester_id: int) -> None:
    """Follow the running run of this bot and chat, or start one for the messages said since the last run."""
    with session() as s:
        bot = s.get(Bot, bot_id)
        run = s.exec(select(CodingRun).where(CodingRun.bot_id == bot_id, CodingRun.conversation_id == conv_id,
                                             CodingRun.status == "running").order_by(CodingRun.id.desc())).first()
        if run is None:
            last = s.exec(select(CodingRun).where(CodingRun.bot_id == bot_id, CodingRun.conversation_id == conv_id)
                          .order_by(CodingRun.id.desc())).first()
            after = last.after_message_id if last else 0
            msgs = s.exec(select(Message).where(Message.conversation_id == conv_id, Message.role == "user",
                                                Message.id > after).order_by(Message.id)).all()
            run_id = None
        else:
            run_id = run.id
    if run_id is None:
        if not msgs:
            return
        prompt = "\n\n".join(m.content.strip() for m in msgs if m.content.strip())
        run = await _start(bot, conv_id, requester_id, prompt, msgs[-1].id)
        run_id = run.id if run else None
    if run_id is not None:
        await _follow(run_id)


async def _start(bot: Bot, conv_id: int, requester_id: int, prompt: str, after_message_id: int):
    from .home import sandbox_key
    key = sandbox_key(bot)
    if not await logged_in(bot):
        _post_msg(conv_id, role="assistant", content=NOT_LOGGED_IN, author_bot_id=bot.id, requester_id=requester_id)
        return None
    with session() as s:
        last = s.exec(select(CodingRun).where(CodingRun.bot_id == bot.id, CodingRun.conversation_id == conv_id,
                                              CodingRun.session_id != "").order_by(CodingRun.id.desc())).first()
        resume = last.session_id if last else ""
    model, workdir = bot.model, clean_workdir(bot.workdir)
    if not MODEL_RE.match(model or "") or (resume and not MODEL_RE.match(resume)):
        _post_msg(conv_id, role="assistant", content="De run kon niet starten: ongeldig model of sessie.",
                  author_bot_id=bot.id, requester_id=requester_id)
        return None
    with session() as s:
        run = CodingRun(bot_id=bot.id, conversation_id=conv_id, requester_id=requester_id,
                        after_message_id=after_message_id)
        s.add(run)
        s.commit()
        s.refresh(run)
        run.base = f".coding/run-{run.id}"
        s.add(run)
        s.commit()
        s.refresh(run)
    base = run.base
    try:
        await sandbox.put_bytes(key, base + ".prompt", prompt.encode())
        await sandbox.put_bytes(key, base + ".system", (bot.instructions or "").encode())
        await sandbox.sb_spawn(key, ["sh", "-c", RUN_SCRIPT, "sh", base, workdir, model, resume,
                                     config.CODING_CLAUDE_BIN])
    except Exception as e:  # the sandbox is down or the files could not be stored
        _end(run.id, "failed")
        _post_msg(conv_id, role="assistant", content=f"De run kon niet starten: {e}"[:500],
                  author_bot_id=bot.id, requester_id=requester_id)
        return None
    pid = 0
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        code, out = await sandbox.sb_exec(key, ["cat", base + ".pid"])
        if code == 0 and out.strip().isdigit():
            pid = int(out.strip())
            break
        await asyncio.sleep(min(0.1, config.CODING_POLL_S))
    with session() as s:
        run = s.get(CodingRun, run.id)
        run.pid = pid
        s.add(run)
        s.commit()
        s.refresh(run)
    return run


def _end(run_id: int, status: str) -> None:
    with session() as s:
        run = s.get(CodingRun, run_id)
        run.status, run.ended_at = status, datetime.now(timezone.utc)
        s.add(run)
        s.commit()


def _update(run_id: int, **fields) -> None:
    with session() as s:
        run = s.get(CodingRun, run_id)
        for k, v in fields.items():
            setattr(run, k, v)
        s.add(run)
        s.commit()


async def _follow(run_id: int) -> None:
    """Read the run's log until it has a result, or its process is gone without one."""
    from .home import sandbox_key
    with session() as s:
        run = s.get(CodingRun, run_id)
        bot = s.get(Bot, run.bot_id)
        conv_id, requester_id, base, pid = run.conversation_id, run.requester_id, run.base, run.pid
    key = sandbox_key(bot)
    common = dict(author_bot_id=bot.id, requester_id=requester_id)
    while True:
        with session() as s:
            run = s.get(CodingRun, run_id)
            offset, text_posted = run.offset, run.text_posted
        # asked before reading, so output written just before the process ended is still read below
        code, _ = await sandbox.sb_exec(key, ["sh", "-c", 'test -e "$1.exit" || ! { [ "$2" -gt 0 ] && kill -0 "$2"; } 2>/dev/null',
                                              "sh", base, str(pid)])
        dead = code == 0
        _, chunk = await sandbox.sb_exec(key, ["sh", "-c", 'tail -c +"$1" "$2" 2>/dev/null | head -c 262144',
                                               "sh", str(offset + 1), base + ".jsonl"], raw=True)
        data, n = _complete(chunk)
        if n == 0 and len(chunk) >= CHUNK_MAX:
            n = len(chunk)  # one line longer than a batch: skip it rather than stall
        if n:
            state = {"text_posted": text_posted}
            finished = False
            for line in data.decode("utf-8", "replace").splitlines():
                if _handle(line, run_id, bot, conv_id, requester_id, common, state):
                    finished = True
                    break
            _update(run_id, offset=offset + n, text_posted=state["text_posted"])
            if finished:
                return
        elif dead:
            _, err = await sandbox.sb_exec(key, ["sh", "-c", 'tail -n 20 "$1" 2>/dev/null', "sh", base + ".err"])
            err = secrets_http.mask(err.strip(), bot.id)
            _end(run_id, "failed")
            _post_msg(conv_id, role="assistant", content="De run stopte zonder resultaat."
                      + (f"\n\n{err}" if err else ""), **common)
            return
        await asyncio.sleep(config.CODING_POLL_S)


def _handle(line: str, run_id: int, bot: Bot, conv_id: int, requester_id: int, common: dict, state: dict) -> bool:
    """Store one log line. Returns True when it held the result, which ends the run."""
    from . import agent
    for item in parse_event(line):
        kind = item["kind"]
        if kind == "session":
            if MODEL_RE.match(item["session_id"]):
                _update(run_id, session_id=item["session_id"])
        elif kind == "text":
            if item["text"].strip():
                _post_msg(conv_id, role="assistant", content=secrets_http.mask(item["text"], bot.id), **common)
                state["text_posted"] = True
        elif kind == "tool_call":
            call = {"id": item["id"], "type": "function", "function": {
                "name": item["name"], "arguments": secrets_http.mask(item["arguments"], bot.id)}}
            _post_msg(conv_id, role="assistant", content="", tool_calls=json.dumps([call]), **common)
        elif kind == "tool_result":
            agent._post_tool(conv_id, item["id"], secrets_http.mask(item["content"], bot.id), **common)
        elif kind == "result":
            with usage.bind(origin="coding", user_id=requester_id, bot_id=bot.id, conversation_id=conv_id):
                usage.log_call(endpoint=None, model=bot.model, raw=item["usage"],
                               reported_cost_usd=item["cost_usd"], notes="claude_code")
            if item["text"].strip() and (not state["text_posted"] or item["is_error"]):
                _post_msg(conv_id, role="assistant", content=secrets_http.mask(item["text"], bot.id), **common)
            _end(run_id, "failed" if item["is_error"] else "done")
            return True
    return False
