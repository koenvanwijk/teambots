# TeamBots

A self-hosted bots platform in the style of Grok Bot, where every bot runs on your own
OpenAI-compatible endpoint. The centre of it is Team Bots: one bot with shared knowledge and
capabilities that every teammate or client talks to in private.

## What is in it

| Area | What works |
| --- | --- |
| Endpoints | Register any `/v1/chat/completions` endpoint (vLLM, Ollama, LiteLLM, ...), GitHub Copilot (github.com or a data-residency tenant such as `org.ghe.com`), the local Claude CLI, or a Router that picks a model per request. The key or GitHub token is stored encrypted and write-only; a connection test checks tool calling; the bot's model is a combobox of the endpoint catalog with free text still allowed |
| Costs | Every LLM call is logged (tokens, model, origin, latency, and the router route when one was used). Official USD prices are seeded from OpenAI, Anthropic and GitHub Copilot docs; unknown models stay 'price unknown'. A Costs view shows totals, charts and the dearest conversations. Optional monthly project budget (warn at 80%/100%, block only if you turn that on). Local endpoints are 0 |
| Router | A Router endpoint applies Option A rules: private or local-only work stays on Spark; short/simple work uses Spark or a cheap Copilot model; tool-heavy, long-context or hard work can use a stronger model; errors and empty replies fall back along a list; at 80% of budget it steers cheaper, and at 100% with blocking only Spark is used. Rules are editable. Existing bots are not switched automatically |
| Projects | A project is the workspace; an optional client name groups projects. The Overview page shows all your projects per client with their latest shared activity |
| Bots | Several bots per project; every project member can make one. Personal bots and Team Bots |
| Team Bots | Draft, publish, share with the whole project or selected people. Within a project everyone who can use a bot may change it (instructions, model, secrets, tools); who may use it (sharing) stays with the owner and managers |
| Privacy | A private chat is readable only by its user, also not by the bot's owner |
| Activity feed | One shared feed per project: after a published Team Bot completes a task in a private chat, it posts a summary of at most three sentences. Only turns that used a tool count; a person can switch it off per chat |
| Prompt | Built Hermes-style once per turn from sections: how the bot works (reply in the person's language, short, act rather than ask), tool guidance, the bot's instructions, the project's `AGENTS.md`, other bots, skills index, team memory, the person's profile, setup and date. Capped at about 40k characters; the current time travels with the newest message so the prompt stays cacheable. Every project member can see it (Knowledge tab, Show prompt) |
| Knowledge base | Per project a directory tree of text files in git (`data/knowledge/p<id>`): `AGENTS.md`, `memory/`, `users/<name>.md`, `skills/` and any other folders. Every change (UI, API, bot, learning review) is a commit with its author, so the Knowledge tab shows who changed what and can restore an earlier version. Reads return a hash; a write must pass the hash it is based on, otherwise it is refused with who changed the file last and a diff, so nobody overwrites someone else's change by accident. Bots use `knowledge_list/read/write/patch/delete/history` |
| Project context, memory | `AGENTS.md` (12k characters in the prompt), team memory from `memory/` (3000) and the person's profile `users/<name>.md` (1500, only in their private chats). Files may hold more: the prompt then lists what it left out with a pointer. The bot curates memory itself with the `memory` tool (add, patch, remove in one batch, budget checked); any member may edit everything. Memory and skills from the old database tables are imported once at startup |
| Learning | After a chat with several turns or tool calls, a background review saves lasting facts to memory and reusable procedures as skills. It runs on the project's Spark (local) endpoint when there is one, otherwise on the bot's own model, never blocks a chat and fails silently. Set `TB_LEARNING=0` to switch it off |
| Long chats | History that no longer fits the prompt is folded into a rolling summary that the bot sees as "Earlier in this conversation". The `search_history` tool finds older messages across the chats the asker may read |
| Skills, routines | Skills are files in `skills/` of the project knowledge base; any member (or a bot, `skill_manage`) can create, fix and delete them. Cron-scheduled prompts per user and conversation |
| Computer | One sandbox per project, shared by its people and bots: shell and files. Each person also has an Overview bot with its own private chat and sandbox (`tb-home-u{user id}`), not tied to a project. The image includes git and the official GitHub CLI (`gh`); no credentials are baked in, so sign in per project with `gh auth login` |
| Files | Per project: people upload files (Files page, the paperclip in a chat, drag and drop, or paste) to `uploads/` on the project computer, where every person and bot of that project can read them; a chat gets an `@file` mention. Download and delete from the Files page. Nothing is overwritten, at most `TB_MAX_UPLOAD_MB` per file, and a text file that seems to hold a credential is held back unless the person uploads it anyway. Downloads are never shown as a page of the app (only png, jpg, gif and webp open inline) |
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

What has and has not been tested: `pytest -q` runs the suite (end-to-end flows, cost tracking, the rule-based router against the mock LLM, GitHub Copilot against a mock — no real token — and the Claude CLI provider against a fake `claude` binary; the screen test is skipped without Xvfb),
including the screen tools with a real Xvfb and Chromium on the local backend (skipped when those are missing). The test MCP listens on port 18792 so it does not collide with other local services. Not tested: the
Docker sandbox backend, a real model, and anything on Windows or Hetzner.

## Run it on Linux

Needs Docker Engine with the compose plugin. `./start.sh`, then open http://localhost:8080. The
script creates `.env` with a random `TB_SECRET_KEY` on first run and uses `docker-compose.local.yml`
(the laptop compose file, without the VPS Tailscale bind). Logs: `docker compose -f docker-compose.local.yml logs -f app`.
To run the tests instead: `pip install -r requirements.txt && pytest -q` (the screen test also
needs Xvfb, xdotool, ImageMagick and a Chromium, and is skipped without them).

## Run it on a Windows laptop

Needs Docker Desktop (WSL 2 backend). In PowerShell, in this folder: `.\start.ps1`, then open
http://localhost:8080. The script creates `.env` with a random `TB_SECRET_KEY` on first run.
A model server on the laptop itself is reachable as `http://host.docker.internal:<port>/v1`.
The local (non-Docker) backend needs a Unix shell and does not work on plain Windows.

For a Hetzner VPS (host Tailscale, port 18080 on loopback and the Tailscale IP only) see `docs/deploy-vps.md`.

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

## Run it on a VPS

The default `docker-compose.yml` is meant for a VPS. The app uses the host's Tailscale (joined
with an interactive `tailscale up`) and listens on port 18080, published on `127.0.0.1` and the
host's Tailscale IPv4 (`TB_TAILSCALE_IP`). It is not published on the public NIC. Full procedure
(interactive login, trial run, cutover, backups, rollback): `docs/deploy-vps.md`.

```bash
cp .env.example .env   # set TB_SECRET_KEY and TB_TAILSCALE_IP (`tailscale ip -4`)
docker compose up -d --build
```

Keep `TB_SECRET_KEY` safe: it encrypts the stored API keys and secrets, and losing it makes them
unreadable. Do not put a public reverse proxy or tunnel in front until that is agreed.

A laptop or Docker Desktop should keep using `./start.sh` / `docker-compose.local.yml` (port 8080).

## First steps in the UI

1. Create the first account, then a team. Share the invite link (`/?invite=…`) with teammates; they use it to create their account and join. Open registration is off unless `TB_OPEN_REGISTRATION=1`.
2. Add an endpoint and click it to run the connection test. "tool calling: true" is required (the Claude CLI uses a JSON tool-call fallback). For GitHub Copilot, pick that provider type and enter the GitHub domain (for example `ictgroup.ghe.com`) plus a GitHub OAuth token; TeamBots exchanges it for a short-lived Copilot session and does not store that session token. For **Claude CLI**, pick that provider and set the default model to an alias (`fable`, `haiku`, `sonnet`, `opus`) or an id such as `claude-fable-5-2`; no base URL or key is needed. It shells out to the local `claude` binary (`-p --output-format json`, one turn, no host MCP tools), reusing that host's Claude Code login. Tokens and `total_cost_usd` from the CLI JSON are logged; if the CLI reports no cost, the call is stored as a subscription with unknown price. Every bot shares that one account and its rate/spend limits; the binary plus a valid login must be present on the host that runs TeamBots. Override the binary with `TB_CLAUDE_CLI` and its working directory with `TB_CLAUDE_CLI_CWD`. Using a Claude Code / subscription login as a shared bot backend is a licensing question for your organization to clear. For a **Router**, pick that provider; default rules use Spark, cheap/normal/heavy Copilot models, and Claude CLI when one exists. Edit the rules from the endpoint dialog. Do not point existing bots at it unless you mean to; a Router endpoint is added for you to choose.
3. Create a bot. For a Team Bot: write instructions, add secrets / MCP servers / skills, then
   tick Published under Sharing. An MCP server can use a static token or OAuth (each member
   signs in). For OAuth, set the authorize and token URLs, a pre-registered Client ID, an
   optional Client secret (stored encrypted, never shown again) and scopes; `TB_PUBLIC_URL`
   must match the redirect URI you register with the provider
   (`{TB_PUBLIC_URL}/api/mcp/oauth/callback`). The bot tool `add_mcp_server` accepts the same
   fields (`client_id`, `client_secret`, `scopes`, `authorize_url`, `token_url`). Dynamic
   client registration is not used.
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
| `TB_MAX_UPLOAD_MB` | `25` | Largest file a person can upload to a project |
| `TB_MAX_TOKENS` | `8192` | Completion budget sent to the model (thinking models need headroom so `reasoning_content` does not eat the whole reply) |
| `TB_OPEN_REGISTRATION` | `0` | `1` lets anyone create an account. When off, only the first user or someone with a valid project invite code (`?invite=`) can register |
| `TB_ENABLE_DOCS` | `0` | `1` serves `/docs`, `/redoc` and `/openapi.json` |
| `TB_AUTH_RATE_LIMIT` | `10` | Max `/api/login` and `/api/register` attempts per IP per minute (`0` disables) |

## Layout

```
app/db.py          data model (SQLModel)
app/access.py      every permission rule
app/agent.py       agent loop, tools, approval gate, handoff
app/llm.py         OpenAI-compatible client
app/copilot.py     GitHub Copilot provider (token exchange, cache, completions)
app/usage.py       LLM usage log, official prices, cost aggregation
app/prices_seed.py official price table (source URL and date on every row)
app/costs_api.py   Costs API
app/claude_cli.py  Claude CLI provider (local `claude` binary)
app/llm_router.py  rule-based model router (Option A)
app/sandbox.py     the project computer (docker / local)
app/screen.py      a virtual screen per bot: screenshot, mouse, keyboard
app/secrets_http.py  secrets filled in outside the sandbox, masking
app/mcp_client.py  MCP tool discovery and calls
app/mcp_oauth.py   MCP OAuth (authorization code + PKCE, per user)
app/routines.py    cron scheduler
app/api.py         REST API + SSE
app/static/        web UI without a build step: index.html, app.css, app.js
tests/             end-to-end tests against a scripted model (pytest -q)
scripts/           local helpers (e.g. import the OpenClaw ICT Copilot token)
docs/router.md     model router (Option A implemented; B/C still described)
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
- Registration is closed once the first account exists, unless `TB_OPEN_REGISTRATION=1` or the
  request includes a valid project invite code. `/docs` and `/openapi.json` are off unless
  `TB_ENABLE_DOCS=1`. Login and register are rate-limited per IP.
- One sandbox per project means every project member and bot can read and change the same files,
  and can see every bot's screen. Do not put two clients in one project.
- The activity feed publishes a model-written summary of private-chat tasks to everyone in the
  project. The summary can still reveal what someone worked on; people can switch it
  off per chat.
- A model can be steered by text in files and tool results (prompt injection). Keep approval mode
  on "ask" for bots that read untrusted content and can act.
- Passwords are PBKDF2-hashed; sessions are bearer tokens without expiry. Add SSO/OIDC and token
  expiry before exposing this to the internet.
