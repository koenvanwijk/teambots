# Claude Code coding bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Een persoonlijke coding bot die Claude Code (`claude -p`) als los proces in de persoonlijke sandbox draait, de run live in de chat toont en een herstart van de app overleeft.

**Architecture:** Een nieuwe module `app/coding.py` start Claude met `sandbox.sb_spawn` via een vast shell-script, laat stream-json naar `.coding/run-<id>.jsonl` op het volume schrijven en leest dat bestand elke `CODING_POLL_S` seconden bij met `sandbox.sb_exec`. Events worden gewone `Message`-rijen. De bestaande run-machinerie van `app/agent.py` (`_runs`, `_held`, `stop`) blijft de baas; `_run_task` stuurt coding bots door naar `coding.run_turn`. Inloggen gebeurt met `/login` van Claude Code in een `xfce4-terminal` op het scherm van de bot.

**Tech Stack:** Python 3.12, FastAPI, SQLModel/SQLite, pytest, vanilla JS (`app/static/app.js`), Docker sandbox-image.

**Spec:** `docs/superpowers/specs/2026-11-17-claude-coding-bot-design.md`

## Global Constraints

- Functie alleen actief met `TB_CODING_BOTS=1` (standaard uit); anders geven de coding-routes 404.
- Geen wijziging aan `app/sandboxd.py`; alleen `sb_spawn`, `sb_exec`, `put_bytes`.
- TeamBots slaat geen Claude-token op en geeft er geen door; Claude leest zijn login uit `CLAUDE_CONFIG_DIR=<workspace>/.config/claude`.
- Coding bot: `team_id = 0`, `is_home = False`, `is_team = False`, `published = False`, `computer_use = True`, `endpoint_id = 0`, `runner = "claude_code"`; sandbox-sleutel `home-u{owner_id}`.
- Gesprek van een coding bot: `Conversation(kind="dm", team_id=0, user_id=owner_id, bot_id=bot.id, share_activity=False)`.
- Claude-aanroep: `-p --output-format stream-json --verbose --model <model> --permission-mode bypassPermissions --append-system-prompt <instructies> [--resume <session_id>]`, prompt via stdin.
- Standaardmodel `opus`; standaardwerkmap `""` (= `/workspace`).
- Model en session-id moeten voldoen aan `^[A-Za-z0-9._:-]{1,100}$`; werkmap gaat door `sandbox._check_path` (of is leeg).
- Tekst en tool-resultaten gaan door `secrets_http.mask(text, bot_id)` voor ze worden opgeslagen; tool-resultaten worden ingekort tot 6000 tekens.
- Berichten tijdens een run gaan in de wachtrij (`agent._held`), nooit naar `_route` (geen steer of interrupt).
- Schrijfstijl in UI-teksten en docs: geen em dashes.

## Review Focus

- **Prompt met shell-tekens** (`"`, `$(...)`, backticks, nieuwe regels): moet letterlijk bij Claude aankomen, niets uitgevoerd. Test in Task 3.
- **Halve regel aan het eind van het logbestand** (Claude schrijft nog): niet parsen, offset niet voorbij de laatste `\n`. Test in Task 3.
- **App herstart midden in een run**: geannuleerde volgtaak zonder stop-verzoek mag het proces niet killen en de status niet wijzigen. Test in Task 4.
- **Container opnieuw aangemaakt / proces weg zonder `result`**: run wordt `failed` met de laatste regels van `.err`, niet eeuwig `running`. Test in Task 4.
- **Niet ingelogd**: een duidelijke melding in de chat, geen run gestart. Test in Task 3.

---

### Task 1: Coding bot aanmaken, instellen en afschermen

**Files:**
- Modify: `app/config.py` (vlaggen)
- Modify: `app/db.py` (`Bot.runner`, `Bot.workdir`, `CodingRun`, migratie)
- Modify: `app/home.py:45` (`sandbox_key`)
- Create: `app/coding.py` (alleen bot-helpers in deze task)
- Modify: `app/api.py` (routes, weigeren van delen)
- Test: `tests/test_coding.py`

**Interfaces:**
- Produces:
  - `config.CODING_BOTS: bool` (`TB_CODING_BOTS == "1"`), `config.CODING_CLAUDE_BIN: str` (`TB_CODING_CLAUDE_BIN`, standaard `"claude"`), `config.CODING_POLL_S: float` (`TB_CODING_POLL_S`, standaard `1.0`).
  - `Bot.runner: str = ""`, `Bot.workdir: str = ""`.
  - `CodingRun(id, bot_id, conversation_id, requester_id, after_message_id: int, session_id: str = "", base: str = "", pid: int = 0, offset: int = 0, status: str = "running", text_posted: bool = False, started_at, ended_at: Optional[datetime])`.
  - `coding.create_bot(s, user, name: str, model: str = "opus", workdir: str = "", instructions: str = "") -> tuple[Bot, Conversation]`
  - `coding.bot_conv(s, bot: Bot) -> Conversation`
  - `coding.bot_out(bot: Bot, conv: Conversation) -> dict` met `id, name, handle, model, workdir, instructions, runner, conversation`.
  - Routes: `GET /api/coding-bots`, `POST /api/coding-bots` `{name, model?, workdir?, instructions?}`, `PATCH /api/coding-bots/{id}` (zelfde velden), alle 404 als `TB_CODING_BOTS` uit staat.

- [ ] **Step 1: Write the failing tests** in `tests/test_coding.py` (zelfde opzet als `tests/test_copilot.py`: env vóór imports, `TestClient(app)`, `_register` helper; zet `config.CODING_BOTS = True` in een fixture en herstel na afloop)

```python
def test_coding_routes_hidden_when_disabled(client_off):
    assert client_off.get("/api/coding-bots", headers=h).status_code == 404

def test_create_coding_bot(client):
    r = client.post("/api/coding-bots", headers=h, json={"name": "Coder"})
    assert r.status_code == 200
    b = r.json()
    assert b["runner"] == "claude_code" and b["model"] == "opus" and b["workdir"] == ""
    assert b["conversation"]["kind"] == "dm" and b["conversation"]["team_id"] == 0
    with session() as s:
        bot = s.get(Bot, b["id"])
        assert home.sandbox_key(bot) == f"home-u{bot.owner_id}"
        assert (bot.is_team, bot.published, bot.computer_use, bot.endpoint_id) == (False, False, True, 0)
    assert [x["id"] for x in client.get("/api/coding-bots", headers=h).json()] == [b["id"]]

def test_coding_bot_rejects_bad_settings(client):
    assert client.post("/api/coding-bots", headers=h, json={"name": "x", "model": "opus; rm -rf /"}).status_code == 400
    assert client.post("/api/coding-bots", headers=h, json={"name": "x", "workdir": "../etc"}).status_code == 400

def test_coding_bot_cannot_be_shared_or_used_by_others(client):
    bid = client.post("/api/coding-bots", headers=h, json={"name": "Coder"}).json()["id"]
    assert client.put(f"/api/bots/{bid}/share", headers=h, json={"published": True, "access": "team"}).status_code == 400
    other = _register(client, "coding_other")
    assert client.patch(f"/api/coding-bots/{bid}", headers=other, json={"name": "mine"}).status_code == 404
    assert client.get("/api/coding-bots", headers=other).json() == []
```

Het exacte body-formaat van `PUT /bots/{id}/share` staat in `app/api.py:1145`; pas de JSON in de test daarop aan.

- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_coding.py` . Expected: FAIL (404 op `/api/coding-bots` of ImportError).

- [ ] **Step 3: Implement**
  - Config-vlaggen; `Bot.runner`, `Bot.workdir` en tabel `CodingRun` in `app/db.py`; kolommen toevoegen in `_ensure_router_columns` (`runner VARCHAR DEFAULT ''`, `workdir VARCHAR DEFAULT ''`).
  - `home.sandbox_key`: `if bot.is_home or bot.runner: return f"home-u{bot.owner_id}"`.
  - `coding.create_bot`, `bot_conv`, `bot_out`, plus `coding.MODEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")` en `coding.clean_workdir(raw: str) -> str` (leeg blijft leeg, anders `sandbox._check_path`, `ValueError` wordt 400). Handle: `code-<bot id>` na flush.
  - Routes in `app/api.py`. `PUT /bots/{id}/share` en `PATCH /bots/{id}` met `endpoint_id`: `_need(not bot.runner, 400, "a coding bot is personal and cannot be shared")`.

- [ ] **Step 4: Run** `.venv/bin/python -m pytest -q tests/test_coding.py` . Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/db.py app/home.py app/coding.py app/api.py tests/test_coding.py
git commit -m "Add personal coding bots behind TB_CODING_BOTS"
```

---

### Task 2: Stream-json omzetten naar berichten

**Files:**
- Modify: `app/coding.py`
- Test: `tests/test_coding.py`

**Interfaces:**
- Produces: `coding.parse_event(line: str) -> list[dict]`. Elk item is een van:
  - `{"kind": "session", "session_id": str}`
  - `{"kind": "text", "text": str}`
  - `{"kind": "tool_call", "id": str, "name": str, "arguments": str}` (arguments = `json.dumps(input)`)
  - `{"kind": "tool_result", "id": str, "content": str}` (lijst-content: de `text`-delen samengevoegd; ingekort tot 6000 tekens met `"\n[... truncated ...]"`)
  - `{"kind": "result", "text": str, "is_error": bool, "cost_usd": float | None, "usage": dict}`
  Een regel die geen geldige JSON is of een onbekend type heeft, geeft `[]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_init():
    assert coding.parse_event('{"type":"system","subtype":"init","session_id":"s1"}') == [{"kind": "session", "session_id": "s1"}]

def test_parse_assistant_text_and_tool_use():
    line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Ik kijk even"},
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}]}})
    assert coding.parse_event(line) == [
        {"kind": "text", "text": "Ik kijk even"},
        {"kind": "tool_call", "id": "toolu_1", "name": "Bash", "arguments": '{"command": "ls"}'}]

def test_parse_tool_result_list_and_truncate():
    line = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "x" * 7000}]}]}})
    [item] = coding.parse_event(line)
    assert item["id"] == "toolu_1" and item["content"].endswith("[... truncated ...]") and len(item["content"]) < 6100

def test_parse_result():
    line = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Klaar",
                       "total_cost_usd": 0.42, "usage": {"input_tokens": 10, "output_tokens": 3}})
    assert coding.parse_event(line) == [{"kind": "result", "text": "Klaar", "is_error": False,
                                         "cost_usd": 0.42, "usage": {"input_tokens": 10, "output_tokens": 3}}]

def test_parse_garbage():
    assert coding.parse_event("not json") == [] and coding.parse_event('{"type":"other"}') == []
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_coding.py -k parse` . Expected: FAIL (`parse_event` bestaat niet).
- [ ] **Step 3: Implement `parse_event`** in `app/coding.py`.
- [ ] **Step 4: Run** dezelfde opdracht. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -am "Parse Claude Code stream-json events"`

---

### Task 3: Een run starten en volgen

**Files:**
- Modify: `app/coding.py`
- Modify: `app/agent.py:696` (`_run_task`), `app/agent.py:1108` (`handle_user_message`)
- Create: `tests/fake_claude_code.py`
- Test: `tests/test_coding.py`

**Interfaces:**
- Consumes: Task 1 (`CodingRun`, config), Task 2 (`parse_event`).
- Produces:
  - `coding.RUN_SCRIPT: str` (hieronder, letterlijk)
  - `async def coding.logged_in(bot: Bot) -> bool` (`test -s .config/claude/.credentials.json` via `sb_exec`)
  - `async def coding.run_turn(conv_id: int, bot_id: int, requester_id: int) -> None`: volgt een bestaande `running` run voor deze bot en dit gesprek, anders start een nieuwe met de user-berichten met `id > vorige run.after_message_id`; keert terug als de run klaar is.
  - `async def coding._start(bot, conv_id, requester_id, prompt: str, after_message_id: int) -> CodingRun`
  - `async def coding._follow(run_id: int) -> None`

`RUN_SCRIPT` (aangeroepen als `["sh", "-c", RUN_SCRIPT, "sh", base, workdir, model, session_id, claude_bin]`; alle invoer als positionele argumenten, prompt en instructies uit bestanden):

```sh
root="$PWD"; base="$root/$1"
export CLAUDE_CONFIG_DIR="$root/.config/claude"
cd "$root/${2:-.}" || { echo "workdir not found: $2" > "$base.err"; echo 127 > "$base.exit"; exit 0; }
"$5" -p --output-format stream-json --verbose --model "$3" --permission-mode bypassPermissions \
  --append-system-prompt "$(cat "$base.system")" ${4:+--resume "$4"} \
  < "$base.prompt" > "$base.jsonl" 2> "$base.err" &
echo $! > "$base.pid"
wait $!
echo $? > "$base.exit"
```

Gedrag van `_start`: zonder login een assistant-bericht "Je bent nog niet ingelogd bij Claude. Gebruik Log in bij Claude bij de instellingen van deze bot." en geen run. Anders: `CodingRun` aanmaken, `base = f".coding/run-{id}"`, met `sandbox.put_bytes` de bestanden `<base>.prompt` en `<base>.system` (bot.instructions) schrijven, `sb_spawn` met het script, daarna tot 10 s wachten op `<base>.pid` en het PID opslaan.

Gedrag van `_follow`, per tik (`CODING_POLL_S`):
- `sb_exec(key, ["sh", "-c", 'tail -c +"$1" "$2" 2>/dev/null | head -c 262144', "sh", str(offset + 1), base + ".jsonl"], raw=True)`.
- Alleen bytes tot en met de laatste `b"\n"` verwerken; `offset` met dat aantal bytes verhogen en na elke batch opslaan.
- Items uit `parse_event`: `session` zet `session_id`; `text` wordt `_post(role="assistant", content=mask(text))` en zet `text_posted`; `tool_call` wordt een assistant-bericht met `tool_calls=json.dumps([{"id", "type": "function", "function": {"name", "arguments"}}])`; `tool_result` wordt `_post_tool`-achtig tool-bericht met `tool_call_id`; `result` logt `usage.log_call(endpoint=None, model=bot.model, raw=usage, reported_cost_usd=cost_usd, notes="claude_code")` binnen `usage.bind(origin="coding", user_id, bot_id, conversation_id)`, post `text` alleen als `not text_posted or is_error`, zet status `done` of `failed` en `ended_at`, en stopt.
- Geen nieuwe bytes: als `<base>.exit` bestaat of `kill -0 <pid>` faalt, status `failed`, bericht "De run stopte zonder resultaat." plus de laatste 20 regels van `<base>.err`.
- `agent._post` en `agent._post_tool` hergebruiken (geen eigen opslaglogica).

Aansluiting in `app/agent.py`:
- `_run_task`: binnen de lock, als `bot.runner == "claude_code"`: `await coding.run_turn(conv_id, bot_id, requester_id)` in plaats van `_run_locked`.
- `handle_user_message`: voor een bot met `runner` die al loopt alleen `_held.setdefault(key, []).append((msg.id, user_id))`, geen `_route`.

`tests/fake_claude_code.py`: leest `--model`, `--resume`, `--append-system-prompt` uit argv en de prompt van stdin. Schrijft regels (met `flush`) : init met `session_id` = `--resume` of `"sess-1"`; assistant met tool_use `Bash`; user met tool_result `"ok"`; assistant-tekst `f"echo:{prompt}|resume:{resume}|system:{system}"`; result met `total_cost_usd 0.01`. Prompt bevat `"sleep"`: 0,2 s pauze tussen regels en 30 s voor het result. Prompt bevat `"die"`: alleen init, dan `sys.stderr.write("boom")` en exit 2. In tests: `config.CODING_CLAUDE_BIN = <pad naar een uitvoerbaar wrapper-script dat fake_claude_code.py met sys.executable start>`, `config.CODING_POLL_S = 0.05`, en een helper die `.config/claude/.credentials.json` in de lokale workspace (`data/workspaces/home-u<id>/`) aanmaakt.

- [ ] **Step 1: Write the failing tests**

```python
def test_run_posts_steps_and_reply(client, logged_in_bot):
    bid, conv = logged_in_bot
    client.post(f"/api/conversations/{conv}/messages", headers=h, json={"content": "maak een test"})
    msgs = wait_for(lambda: _finished_run(bid))  # helper: wacht tot CodingRun.status != running
    texts = [m.content for m in _messages(conv) if m.role == "assistant" and m.content]
    assert texts[-1] == "echo:maak een test|resume:|system:"
    calls = [json.loads(m.tool_calls)[0]["function"]["name"] for m in _messages(conv) if m.tool_calls]
    assert calls == ["Bash"]
    assert any(m.role == "tool" and m.tool_call_id == "toolu_1" and m.content == "ok" for m in _messages(conv))
    run = _last_run(bid)
    assert run.status == "done" and run.session_id == "sess-1"
    assert _usage_rows(bid)[-1].notes == "claude_code"

def test_prompt_with_shell_characters_arrives_literally(client, logged_in_bot):
    bid, conv = logged_in_bot
    nasty = 'a "b" $(touch /tmp/pwned) `id` \n c'
    client.post(f"/api/conversations/{conv}/messages", headers=h, json={"content": nasty})
    wait_for(lambda: _finished_run(bid))
    assert _assistant_texts(conv)[-1].startswith("echo:" + nasty.strip())
    assert not os.path.exists("/tmp/pwned")

def test_partial_line_is_not_parsed():
    assert coding._complete(b'{"a":1}\n{"b"') == (b'{"a":1}\n', 8)  # helper used by _follow

def test_not_logged_in_posts_hint(client, bot_without_login):
    bid, conv = bot_without_login
    client.post(f"/api/conversations/{conv}/messages", headers=h, json={"content": "hoi"})
    wait_for(lambda: any("niet ingelogd" in t for t in _assistant_texts(conv)))
    assert _last_run(bid) is None
```

`coding._complete(data: bytes) -> tuple[bytes, int]` geeft het deel tot en met de laatste `\n` en het aantal bytes.

- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_coding.py -k "run or prompt or partial or logged"` . Expected: FAIL.
- [ ] **Step 3: Implement** `RUN_SCRIPT`, `logged_in`, `_complete`, `_start`, `_follow`, `run_turn` en de twee aanpassingen in `app/agent.py`.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest -q tests/test_coding.py` . Expected: PASS.
- [ ] **Step 5: Commit** `git commit -am "Run Claude Code as a detached process and follow its log"`

---

### Task 4: Stoppen, wachtrij, resume en herstart

**Files:**
- Modify: `app/coding.py`, `app/agent.py:728` (`interrupt`), `app/main.py:25` (`lifespan`)
- Test: `tests/test_coding.py`

**Interfaces:**
- Consumes: Task 3.
- Produces:
  - `coding.request_stop(conv_id: int, bot_id: int) -> None` (zet een vlag in het geheugen)
  - `coding.resume_all() -> int` (start voor elke `CodingRun` met `status == "running"` `agent.start_run(conv, bot, requester)`; geeft het aantal terug)

Gedrag:
- `agent.interrupt` roept `coding.request_stop(conv_id, bot_id)` aan voor het annuleren.
- `run_turn` vangt `asyncio.CancelledError`: met stop-vlag `kill <pid>` via `sb_exec`, status `stopped`, bericht "Gestopt."; zonder vlag (afsluiten van de app) niets aan proces of status veranderen en opnieuw raisen.
- Een nieuwe run na een vorige met `session_id` geeft `--resume <session_id>` mee.
- `lifespan` roept `coding.resume_all()` aan na `routines.start()` (alleen als `config.CODING_BOTS`).

- [ ] **Step 1: Write the failing tests**

```python
def test_second_message_resumes_session(client, logged_in_bot):
    bid, conv = logged_in_bot
    send(conv, "eerste"); wait_for(lambda: _finished_run(bid))
    send(conv, "tweede"); wait_for(lambda: _runs_done(bid) == 2)
    assert _assistant_texts(conv)[-1] == "echo:tweede|resume:sess-1|system:"

def test_message_during_run_is_queued(client, logged_in_bot):
    bid, conv = logged_in_bot
    send(conv, "sleep lang"); wait_for(lambda: _last_run(bid) and _last_run(bid).pid)
    send(conv, "daarna")
    st = client.get(f"/api/conversations/{conv}/state", headers=h).json()
    assert len(st["queued"]) == 1

def test_stop_kills_process(client, logged_in_bot):
    bid, conv = logged_in_bot
    send(conv, "sleep lang"); wait_for(lambda: _last_run(bid) and _last_run(bid).pid)
    client.post(f"/api/conversations/{conv}/stop", headers=h)
    wait_for(lambda: _last_run(bid).status == "stopped")
    assert not _pid_alive(_last_run(bid).pid)

def test_cancel_without_stop_leaves_run_running_and_resume_all_picks_it_up(client, logged_in_bot):
    bid, conv = logged_in_bot
    send(conv, "sleep lang"); wait_for(lambda: _last_run(bid) and _last_run(bid).pid)
    agent._runs[(conv, bid)].task.cancel()      # simuleer afsluiten van de app
    wait_for(lambda: (conv, bid) not in agent._runs)
    assert _last_run(bid).status == "running" and _pid_alive(_last_run(bid).pid)
    assert coding.resume_all() == 1
    assert (conv, bid) in agent._runs
    client.post(f"/api/conversations/{conv}/stop", headers=h)

def test_process_dies_without_result(client, logged_in_bot):
    bid, conv = logged_in_bot
    send(conv, "die"); wait_for(lambda: _finished_run(bid))
    run = _last_run(bid)
    assert run.status == "failed"
    assert "boom" in _assistant_texts(conv)[-1]
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_coding.py -k "resume or queued or stop or cancel or dies"` . Expected: FAIL.
- [ ] **Step 3: Implement** `request_stop`, de afhandeling in `run_turn`, `--resume`, `resume_all` en de aanroep in `lifespan`.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest -q` (hele suite). Expected: alles PASS (2 skipped zoals nu).
- [ ] **Step 5: Commit** `git commit -am "Stop, queue, resume and restart handling for coding runs"`

---

### Task 5: Inloggen bij Claude en het sandbox-image

**Files:**
- Modify: `app/coding.py`, `app/api.py`, `sandbox/Dockerfile`
- Test: `tests/test_coding.py`

**Interfaces:**
- Consumes: Task 1, `screen._env(key, bot_id)` (start Xvfb en geeft `DISPLAY`-env).
- Produces:
  - `async def coding.open_login(bot: Bot) -> None`: `sb_spawn(key, ["sh", "-c", 'export CLAUDE_CONFIG_DIR="$PWD/.config/claude"; mkdir -p "$CLAUDE_CONFIG_DIR"; exec xfce4-terminal --geometry 160x45 -x "$1"', "sh", config.CODING_CLAUDE_BIN], env=await screen._env(key, bot.id))`
  - `GET /api/coding-bots/{id}/login` geeft `{"logged_in": bool}`; `POST /api/coding-bots/{id}/login` start de terminal en geeft `{"screen": f"/api/bots/{id}/screen/vnc"}`. Alleen de eigenaar, anders 404.

- [ ] **Step 1: Write the failing tests**

```python
def test_login_status(client, coding_bot):
    bid = coding_bot
    assert client.get(f"/api/coding-bots/{bid}/login", headers=h).json() == {"logged_in": False}
    _make_credentials(bid)
    assert client.get(f"/api/coding-bots/{bid}/login", headers=h).json() == {"logged_in": True}

def test_open_login_spawns_terminal(client, coding_bot, monkeypatch):
    calls = []
    async def fake_spawn(key, argv, env=None): calls.append((key, argv, env))
    async def fake_env(key, bot_id, **kw): return {"DISPLAY": f":{100 + bot_id}"}
    monkeypatch.setattr(coding.sandbox, "sb_spawn", fake_spawn)
    monkeypatch.setattr(coding.screen, "_env", fake_env)
    r = client.post(f"/api/coding-bots/{coding_bot}/login", headers=h)
    assert r.json() == {"screen": f"/api/bots/{coding_bot}/screen/vnc"}
    key, argv, env = calls[0]
    assert key.startswith("home-u") and "xfce4-terminal" in argv[2] and env["DISPLAY"] == f":{100 + coding_bot}"
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_coding.py -k login` . Expected: FAIL.
- [ ] **Step 3: Implement** `open_login` en de twee routes. In `sandbox/Dockerfile` (heeft al `xfce4-terminal` sinds de XFCE-desktop op main): Claude Code installeren (npm-pakket `@anthropic-ai/claude-code` of de officiele installer, met een Node-versie die het pakket ondersteunt); `ENV CLAUDE_CONFIG_DIR=/workspace/.config/claude`; een `command -v claude` controle zoals de bestaande.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest -q tests/test_coding.py` en `docker build -t teambots-sandbox:test ./sandbox` . Expected: PASS en een geslaagde build.
- [ ] **Step 5: Commit** `git commit -am "Sign in to Claude Code from the sandbox screen"`

---

### Task 6: Web-UI en documentatie

**Files:**
- Modify: `app/static/app.js` (zijbalk rond regel 352, nieuwe functies)
- Modify: `app/api.py` (`GET /api/me` of het bestaande config-antwoord krijgt `coding_bots: bool`)
- Modify: `README.md`, `.env.example`
- Test: handmatig in de browser (lokale run), plus `node --check app/static/app.js`

**Interfaces:**
- Consumes: alle routes uit Task 1 en 5.
- Produces (JS): `loadCodingBots()`, `newCodingBot()`, `editCodingBot(b)`, `claudeLogin(b)`.

- [ ] **Step 1: Implement** in `app/static/app.js`:
  - Als `coding_bots` aan staat: onder de Overview-knop een lijst coding bots (`GET /api/coding-bots`), elk opent `openConv(b.conversation, b)`, plus een knop **New coding bot** (`form()` met naam, model `opus`, werkmap, instructies).
  - In de bovenbalk van een coding-gesprek: **Settings** (`editCodingBot`, `PATCH`) en **Log in bij Claude** (`claudeLogin`: `POST .../login`, opent het `screen`-pad in een nieuw tabblad, en toont daarna de status van `GET .../login`).
  - Tool-stappen van Claude (`Bash`, `Edit`, `Read`, `Write`, `Grep`, `Glob`) krijgen een icoon in de bestaande tabel rond regel 1022.
- [ ] **Step 2: Docs**: README-rij "Coding bots" in de feature-tabel, `TB_CODING_BOTS`, `TB_CODING_CLAUDE_BIN`, `TB_CODING_POLL_S` in de variabelentabel en `.env.example`, met de waarschuwing dat de Overview-bot in dezelfde sandbox de Claude-login kan lezen.
- [ ] **Step 3: Verify**: `node --check app/static/app.js`; lokale run met `TB_CODING_BOTS=1 TB_SANDBOX=local TB_CODING_CLAUDE_BIN=<pad naar echte claude>`; een coding bot maken, inloggen (lokaal: `claude` in de workspace met `CLAUDE_CONFIG_DIR`), een opdracht geven en zien dat de stappen verschijnen, stoppen werkt en een tweede bericht verdergaat. `.venv/bin/python -m pytest -q` blijft groen.
- [ ] **Step 4: Commit** `git commit -am "Coding bots in the web UI and docs"`
