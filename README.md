# TeamBots

A self-hosted bots platform in the style of Grok Bot, where every bot runs on your own
OpenAI-compatible endpoint. The centre of it is Team Bots: one bot with shared knowledge and
capabilities that every teammate or client talks to in private.

## What is in it

| Area | What works |
| --- | --- |
| Endpoints | Register any `/v1/chat/completions` endpoint (vLLM, Ollama, LiteLLM, ...) or GitHub Copilot (github.com or a data-residency tenant such as `org.ghe.com`). The key or GitHub token is stored encrypted and write-only; a connection test checks tool calling; model override per bot |
| Projects | A project is the workspace; an optional client name groups projects. The Overview page shows all your projects per client with their latest shared activity |
| Bots | Several bots per project; every project member can make one. Personal bots and Team Bots |
| Team Bots | Draft, publish, share with the whole project or selected people; roles owner / manager / collaborator |
| Privacy | A private chat is readable only by its user, also not by the bot's owner |
| Activity feed | Per Team Bot a shared feed: after a task in a private chat the bot posts a summary of at most three sentences. Only turns that used a tool count; a person can switch it off per chat |
| Memory | Personal memory per user, shared team memory, learnings from collaborators wait for review |
| Skills, routines | Saved by owner or manager; cron-scheduled prompts per user and conversation |
| Computer | One sandbox per project, shared by its people and bots: shell and files. Each person also has an Overview bot with its own private chat and sandbox (`tb-home-u{user id}`), not tied to a project. The image includes git and the official GitHub CLI (`gh`); no credentials are baked in, so sign in per project with `gh auth login` |
| Screen | With "computer use" on, a bot has its own virtual screen with a browser, screenshot / click / type / key / scroll tools, and a live view in the UI. "Open scherm" attaches noVNC to that same Xvfb so you can use mouse and keyboard (e.g. to sign in to Chromium). Needs a model with image input |
| Secrets | Never in the sandbox or the model's context. The bot writes `{{secret:NAME}}` in an `http_request`; the app fills it in outside the sandbox, only for the hosts the secret is bound to, never follows redirects, and masks secret values in everything returned to the model. A message that seems to contain a credential (a known token format, `password is ...`, or the value of a stored secret) is held back before it is stored: the person saves it as a secret and sends the placeholder, edits the message, or sends it anyway |
| Approvals | Risky tools pause for an approval card (Allow once / Always allow / Deny); only the requester can answer |
| Channels | Several people and bots; every project bot is added when a channel or a bot is created and replies to every message by default (or only on `@handle` if set to mention-only). Removing a bot sticks. No personal memory in channels |
| Handoff, MCP | `ask_bot` delegates to another bot; streamable-HTTP MCP servers per bot |
| While a bot works | You can keep typing. A new message steers the running turn (the bot reads it at its next step), waits in the queue behind it, or interrupts it. "Auto" decides: a short stop word interrupts, otherwise the bot's model classifies the message, and steering is the fallback. A Stop button ends the turn. Only the person the bot works for can steer or interrupt; messages from others queue |
| Web UI | Dark and light theme. `@` in the message box lists what you can add: bots (in a channel also bots and people you can add on the spot), skills (`@skill:name`), files on the project computer (`@file:path`), people and secrets. What a bot does is folded into one line per turn; commands and file contents open on click, approval cards always show. Search with Ctrl K. Bots, channels, projects and endpoints can be renamed |
| Install as an app | The web UI is a PWA: on https or localhost the browser offers to install it (also under the user menu), and it opens in its own window with the TeamBots icon. The service worker caches only the app shell, never anything under `/api` |

Not built: Slack / Discord / Microsoft Teams, meeting bots, voice, SSO, token streaming,
event-triggered routines.

What has and has not been tested: `pytest -q` runs 36 tests (24 end-to-end, 4 unit tests for thinking-model replies, and 8 GitHub Copilot tests against a mock — no real token) against a scripted model,
including the screen tools with a real Xvfb and Chromium on the local backend (skipped when those are missing). The test MCP listens on port 18792 so it does not collide with other local services. Not tested: the
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
A message with `SLOW 20` in it makes the mock wait 20 seconds, so you can try typing while the bot
is busy.

## Run it on a VPS or DGX Spark

```bash
echo "TB_SECRET_KEY=$(openssl rand -hex 32)" > .env
docker compose up -d --build
```

Put a TLS reverse proxy (Caddy, Traefik, nginx) in front of port 8080. Keep `TB_SECRET_KEY`
safe: it encrypts the stored API keys and secrets, and losing it makes them unreadable.

## First steps in the UI

1. Create an account, then a team. Share the invite code with teammates.
2. Add an endpoint and click it to run the connection test. "tool calling: true" is required. For GitHub Copilot, pick that provider type and enter the GitHub domain (for example `ictgroup.ghe.com`) plus a GitHub OAuth token; TeamBots exchanges it for a short-lived Copilot session and does not store that session token.
3. Create a bot. For a Team Bot: write instructions, add secrets / MCP servers / skills, then
   tick Published under Sharing.
4. Chat. Create a channel and all project bots join it automatically and reply to every message; remove a bot or set it to mention-only if you do not want that. On a bot with computer use, **Open scherm** opens the interactive screen (sign in to Chromium there; the profile is kept in the project's Docker volume). To use `gh` in the sandbox, run `gh auth login` there (config is stored on the project volume); TeamBots secrets never enter the sandbox, so a `GH_TOKEN` secret cannot be used as an environment variable inside it.

5. **Overview** at the top of the sidebar is your personal bot: it sees every project, bot and channel you can access, can ask a project bot a question and relay the answer, and has its own computer (**Open scherm**). It uses the first model endpoint it finds in your projects; change that under the Overview bot's settings.
6. **Routines** in the sidebar lists every routine you own, across projects: name, bot, project, conversation, the schedule in words and as cron, the next and last run in Europe/Amsterdam, and whether it is enabled. Pause, resume, edit (name, cron, prompt), delete, or run one now. A bot's settings still have a Routines tab to create one in the conversation you have open. The Overview bot can list and manage the same routines.

## Voice and external access

The Overview bot is meant to be reachable from a voice assistant (Grok Bot voice, a Tesla voice agent, or anything that can call an HTTP API) after you have signed in once and created a token.

In the UI: click your name at the bottom of the sidebar → **API tokens for voice** → **New token**. The secret is shown once (it starts with `tbh_`). Store it in the assistant. You can revoke it there later. The token is stored hashed and is scoped to the overview bot; it cannot be used as a session token for the rest of the API.

Send a message (it lands in the same Overview chat you see in the UI):

```bash
curl -sS http://127.0.0.1:18080/api/v1/home/message \
  -H "Authorization: Bearer tbh_YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"What is going on in my projects?","mode":"voice"}'
```

`mode` is `voice` (short spoken reply, no markdown) or `chat`. The JSON response is `{ "reply", "conversation_id", "approval_pending" }`. Risky tools still pause for the normal approval card in the UI; the API never auto-approves.

Voice stacks that speak OpenAI can point at TeamBots as a `/v1` endpoint, model `teambots-home` (or `teambots-home-voice`):

```bash
curl -sS http://127.0.0.1:18080/v1/chat/completions \
  -H "Authorization: Bearer tbh_YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"teambots-home","messages":[{"role":"user","content":"What are my bots doing?"}]}'
```

Also: `GET /api/v1/home/status` (a text overview), `GET /api/v1/home/openapi` (tool schema for other agents), `GET /v1/models`. All of these require the Bearer token.

The Python hook a later adapter should call is `run_home_turn` in `app/home.py` (message in, final reply out, no UI). The HTTP routes above are thin wrappers around it.

**Reachability.** On the LAN the app is at `http://192.168.86.25:18080` (and on Tailscale at this machine's Tailscale IP, same port). A Tesla or Grok Bot voice running outside the house cannot use a plain HTTP LAN address. For that you need HTTPS reachability, for example [Tailscale Funnel](https://tailscale.com/kb/1223/funnel) or a [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) tunnel in front of port 18080. Do not open a public tunnel until you have chosen one of those and are happy with it being on the internet.

There is also an HTTPS front door at
<https://teamsbot-extnode-3ez6v0lng26hyupqse5n0kb61n9.gobrev.dev>
(NVIDIA Brev). That URL is behind a Pomerium SSO login: a browser that has signed in can use the UI, but a voice assistant that only sends a TeamBots API token gets a 302 to `auth.apps.run.brev.nvidia.com` and never reaches `/api/v1/home` or `/v1/chat/completions`. For voice, put those paths on a listener that does not require the Brev SSO (a Brev public port, or a cloudflared / Tailscale Funnel tunnel to port 18080). Do not open that tunnel until you have chosen one.

TeamBots is meant to sit behind a TLS reverse proxy. It honours `X-Forwarded-Proto` and `X-Forwarded-Host` (`ProxyHeadersMiddleware`, and `uvicorn --proxy-headers` in Docker and `start-local.sh`). Redirects, the Secure cookie used by the interactive screen, WebSockets for noVNC (`wss`) and the event stream, and the PWA manifest and service worker (relative URLs, `start_url` `/`) then use https and the public host. Keep the app itself on a private port; only the proxy should be public.


## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TB_SECRET_KEY` | insecure dev value | Encrypts API keys and secrets at rest |
| `TB_DATA_DIR` | `./data` | SQLite database and local workspaces |
| `TB_DB_URL` | SQLite in data dir | Any SQLAlchemy URL, e.g. Postgres |
| `TB_SANDBOX` | `local` | `docker` or `local` |
| `TB_SANDBOX_IMAGE` | `teambots-sandbox:latest` | Image for the bots' computers |
| `TB_SANDBOX_NETWORK` | `bridge` | Docker network for sandboxes (`none` = offline) |
| `TB_BROWSER` | `google-chrome` | Browser binary inside the sandbox (`google-chrome` by default; set `chromium` to use Debian Chromium). Runs as user `bot` with `--no-sandbox` because the container drops capabilities. The profile is stored on the project volume under `.screens/bot<id>` |
| `TB_HTTP_ALLOW_PRIVATE` | `0` | `1` lets `http_request` reach private addresses and send secrets over http (development) |
| `TB_BIND`, `TB_DOMAIN` | `127.0.0.1`, `localhost` | Compose: bind address of the app, domain for Caddy |
| `TB_TIMEZONE` | `Europe/Amsterdam` | Time zone for cron expressions |
| `TB_MAX_STEPS` | `12` | Tool-call rounds per turn |
| `TB_MAX_TOKENS` | `8192` | Completion budget sent to the model (thinking models need headroom so `reasoning_content` does not eat the whole reply) |

## Layout

```
app/db.py          data model (SQLModel)
app/access.py      every permission rule
app/agent.py       agent loop, tools, approval gate, handoff
app/llm.py         OpenAI-compatible client
app/copilot.py     GitHub Copilot provider (token exchange, cache, completions)
app/sandbox.py     the project computer (docker / local)
app/screen.py      a virtual screen per bot: screenshot, mouse, keyboard
app/secrets_http.py  secrets filled in outside the sandbox, masking
app/mcp_client.py  MCP tool discovery and calls
app/routines.py    cron scheduler
app/api.py         REST API + SSE
app/static/        web UI without a build step: index.html, app.css, app.js
tests/             end-to-end tests against a scripted model (pytest -q)
scripts/           local helpers (e.g. import the OpenClaw ICT Copilot token)
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
