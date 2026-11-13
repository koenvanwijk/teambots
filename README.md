# TeamBots

A self-hosted bots platform in the style of Grok Bot, where every bot runs on your own
OpenAI-compatible endpoint. The centre of it is Team Bots: one bot with shared knowledge and
capabilities that every teammate or client talks to in private.

## What is in it

| Area | What works |
| --- | --- |
| Endpoints | Register any `/v1/chat/completions` endpoint (vLLM, Ollama, LiteLLM, ...), key stored encrypted and write-only, connection test that checks tool calling, model override per bot |
| Projects | A project is the workspace; an optional client name groups projects. The Overview page shows all your projects per client with their latest shared activity |
| Bots | Several bots per project; every project member can make one. Personal bots and Team Bots |
| Team Bots | Draft, publish, share with the whole project or selected people; roles owner / manager / collaborator |
| Privacy | A private chat is readable only by its user, also not by the bot's owner |
| Activity feed | Per Team Bot a shared feed: after a task in a private chat the bot posts a summary of at most three sentences. Only turns that used a tool count; a person can switch it off per chat |
| Memory | Personal memory per user, shared team memory, learnings from collaborators wait for review |
| Skills, routines | Saved by owner or manager; cron-scheduled prompts per user and conversation |
| Computer | One sandbox per project, shared by its people and bots: shell and files |
| Screen | With "computer use" on, a bot has its own virtual screen with a browser, screenshot / click / type / key / scroll tools, and a live view in the UI. Needs a model with image input |
| Secrets | Never in the sandbox or the model's context. The bot writes `{{secret:NAME}}` in an `http_request`; the app fills it in outside the sandbox, only for the hosts the secret is bound to, never follows redirects, and masks secret values in everything returned to the model |
| Approvals | Risky tools pause for an approval card (Allow once / Always allow / Deny); only the requester can answer |
| Channels | Several people and bots, bots answer on `@handle`, no personal memory in channels |
| Handoff, MCP | `ask_bot` delegates to another bot; streamable-HTTP MCP servers per bot |

Not built: Slack / Discord / Microsoft Teams, meeting bots, voice, SSO, token streaming,
event-triggered routines.

What has and has not been tested: `pytest -q` runs 14 end-to-end tests against a scripted model,
including the screen tools with a real Xvfb and Chromium on the local backend. Not tested: the
Docker sandbox backend, a real model, and anything on Windows or Hetzner.

## Run it on Linux

Needs Docker Engine with the compose plugin. `./start.sh`, then open http://localhost:8080. The
script creates `.env` with a random `TB_SECRET_KEY` on first run. Logs: `docker compose logs -f app`.
To run the tests instead: `pip install -r requirements.txt && pytest -q` (the screen test also
needs Xvfb, xdotool, ImageMagick and a Chromium, and is skipped without them).

## Run it on a Windows laptop

Needs Docker Desktop (WSL 2 backend). In PowerShell, in this folder: `.\start.ps1`, then open
http://localhost:8080. The script creates `.env` with a random `TB_SECRET_KEY` on first run.
A model server on the laptop itself is reachable as `http://host.docker.internal:<port>/v1`.
The local (non-Docker) backend needs a Unix shell and does not work on plain Windows.

For Hetzner see `deploy/hetzner.md`.

## Run it without Docker (Linux, macOS, WSL)

```bash
pip install -r requirements.txt
TB_SANDBOX=local uvicorn app.main:app --port 8080      # http://localhost:8080
```

`TB_SANDBOX=local` runs commands as a plain subprocess on your machine. It is for development
only and gives no isolation.

To try it without a real model: `uvicorn tests.mock_llm:app --port 8791`, then add the endpoint
`http://localhost:8791/v1` with key `sk-test-key`. The mock answers `TOOL:{"name": "run_command",
"arguments": {"command": "ls"}}` with that tool call, which is enough to see the approval flow.

## Run it on a VPS or DGX Spark

```bash
echo "TB_SECRET_KEY=$(openssl rand -hex 32)" > .env
docker compose up -d --build
```

Put a TLS reverse proxy (Caddy, Traefik, nginx) in front of port 8080. Keep `TB_SECRET_KEY`
safe: it encrypts the stored API keys and secrets, and losing it makes them unreadable.

## First steps in the UI

1. Create an account, then a team. Share the invite code with teammates.
2. Add an endpoint and click it to run the connection test. "tool calling: true" is required.
3. Create a bot. For a Team Bot: write instructions, add secrets / MCP servers / skills, then
   tick Published under Sharing.
4. Chat. Create a channel and add people and bots to work together.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TB_SECRET_KEY` | insecure dev value | Encrypts API keys and secrets at rest |
| `TB_DATA_DIR` | `./data` | SQLite database and local workspaces |
| `TB_DB_URL` | SQLite in data dir | Any SQLAlchemy URL, e.g. Postgres |
| `TB_SANDBOX` | `local` | `docker` or `local` |
| `TB_SANDBOX_IMAGE` | `teambots-sandbox:latest` | Image for the bots' computers |
| `TB_SANDBOX_NETWORK` | `bridge` | Docker network for sandboxes (`none` = offline) |
| `TB_BROWSER` | `chromium` | Browser binary inside the sandbox |
| `TB_HTTP_ALLOW_PRIVATE` | `0` | `1` lets `http_request` reach private addresses and send secrets over http (development) |
| `TB_BIND`, `TB_DOMAIN` | `127.0.0.1`, `localhost` | Compose: bind address of the app, domain for Caddy |
| `TB_TIMEZONE` | `Europe/Amsterdam` | Time zone for cron expressions |
| `TB_MAX_STEPS` | `12` | Tool-call rounds per turn |

## Layout

```
app/db.py          data model (SQLModel)
app/access.py      every permission rule
app/agent.py       agent loop, tools, approval gate, handoff
app/llm.py         OpenAI-compatible client
app/sandbox.py     the project computer (docker / local)
app/screen.py      a virtual screen per bot: screenshot, mouse, keyboard
app/secrets_http.py  secrets filled in outside the sandbox, masking
app/mcp_client.py  MCP tool discovery and calls
app/routines.py    cron scheduler
app/api.py         REST API + SSE
app/static/        single-file web UI
tests/             end-to-end tests against a scripted model (pytest -q)
```

## Security

- The app container mounts the Docker socket to start sandboxes. That is root-equivalent on the
  host. Run it on a dedicated VM, or replace the socket with a restricted proxy or rootless Docker.
- Sandboxes drop all capabilities, are limited to 2 CPU / 2 GB / 512 processes, and have network
  access by default. Set `TB_SANDBOX_NETWORK=none` or a filtered network if bots should not reach
  your LAN.
- Secrets never enter the sandbox or the prompt. Two limits remain: a server that echoes a secret
  back transformed (encoded, split) is not masked, so bind secrets only to hosts you trust; and
  whoever can use the bot can make requests with its secrets to the bound hosts.
- `http_request` runs in the app, outside the sandbox. It refuses private and link-local addresses
  unless `TB_HTTP_ALLOW_PRIVATE=1`.
- One sandbox per project means every project member and bot can read and change the same files,
  and can see every bot's screen. Do not put two clients in one project.
- The activity feed publishes a model-written summary of private-chat tasks to everyone with
  access to the bot. The summary can still reveal what someone worked on; people can switch it
  off per chat.
- A model can be steered by text in files and tool results (prompt injection). Keep approval mode
  on "ask" for bots that read untrusted content and can act.
- Passwords are PBKDF2-hashed; sessions are bearer tokens without expiry. Add SSO/OIDC and token
  expiry before exposing this to the internet.
