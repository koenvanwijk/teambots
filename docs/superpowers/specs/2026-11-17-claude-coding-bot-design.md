# Coding bot met Claude Code in de persoonlijke sandbox

Datum: 2026-11-17. Status: goedgekeurd door Ralf.

## Waarom

Ralf wil lange coding-taken (minuten tot een uur) kunnen starten, volgen en bijsturen vanuit
TeamBots, ook als de browser dicht is of hij onderweg is. Er is nu geen coding harness in
TeamBots: de bestaande provider `claude_cli` gebruikt Claude alleen als model (een beurt, geen
eigen tools, de gedeelde login in de app-container). Hier wordt Claude Code zelf de agent: hij
leest en bewerkt bestanden, voert commando's uit en houdt een sessie vast.

## Doelen

- Een persoonlijke **Coding bot** die Claude Code (`claude -p`) draait in de persoonlijke sandbox
  van de eigenaar (`tb-home-u<id>`).
- Elke gebruiker koppelt zijn **eigen Claude-login** (bij Ralf een ICT Group-account). Een bot
  gebruikt altijd de login van de eigenaar; niemand anders kan de bot of de login gebruiken.
- Een run **loopt door** als de browser dicht gaat en als de app herstart; na een herstart pakt
  de app de run weer op.
- Je **ziet live** wat Claude doet (tekst, tool-aanroepen, resultaten) als gewone stappen in de
  chat, met ongeveer een seconde vertraging.
- Je kunt een run **stoppen**, en met een volgend bericht **verdergaan** in dezelfde sessie.
- Kosten en tokens uit de CLI komen in het Costs-overzicht.

## Niet-doelen (v1)

- Copilot CLI als tweede runner (komt later; het ontwerp houdt er rekening mee met een veld
  `runner`).
- Coding bots in een project-sandbox of gedeeld met anderen.
- Goedkeuringskaarten per tool-aanroep van Claude (`--permission-prompt-tool`).
- Bijsturen midden in een lopende run (een bericht tijdens een run komt in de wachtrij).
- De Overview-bot runs laten starten of opvragen (een logische vervolgstap voor spraak).

## Anti-doelen

- De Claude-login van de een mag nooit door een ander gebruikt kunnen worden.
- Het token mag niet in de chat, de database of de logs van de app terechtkomen (TeamBots slaat
  het helemaal niet op).
  Andere gebruikers mogen er nooit bij kunnen. (De eigen Overview-bot kan er wel bij, zie
  Veiligheid.)
- Geen wijziging aan de beveiligingsgrens van `sandboxd` (alleen bestaande `sb_spawn`/`sb_exec`).
- Een run mag de app niet blokkeren of andere sandboxes vertragen.

## Randvoorwaarden

- Werkt met beide sandbox-backends (`docker` via `sandboxd`, en `local` voor ontwikkeling en
  tests).
- De sandbox draait als gebruiker `bot`, met `--cap-drop ALL`, onder gVisor op de VPS.
- `sb_exec` en `sb_spawn` gebruiken een lock per sandbox. Polling moet kort zijn (een `tail`),
  zodat de Overview-bot in dezelfde sandbox niet merkbaar trager wordt.
- De Claude-login van Ralf is van ICT Group; ICT moet akkoord zijn dat dat token op deze VPS
  staat. Daarom staat de functie achter `TB_CODING_BOTS=1` (standaard uit), zodat de code
  gemerged en gedeployed kan worden zonder dat iemand inlogt.

## Aanpakken

- **A. Streamen via `sandboxd`**: nieuwe streaming-exec, events direct naar de chat. Nadeel: de
  run hangt aan de verbinding van de app en `sandboxd` moet aangepast worden.
- **B. Los proces in de sandbox, logbestand volgen** (gekozen): de app start Claude met
  `sb_spawn` op de achtergrond, Claude schrijft stream-json naar een bestand op het volume, de app
  leest dat bestand stap voor stap bij met `sb_exec`. Overleeft browser en app-herstart, geen
  wijziging aan `sandboxd`.
- **C. Een container per run**: beste isolatie, maar veel meer werk aan `sandboxd` en Docker, en
  weinig winst in een sandbox die al persoonlijk is.

Ralf koos B.

## Ontwerp

### Login per gebruiker

- De gebruiker logt in met de gewone login van Claude Code, in zijn eigen persoonlijke sandbox.
  TeamBots slaat geen token op en geeft er ook geen door.
- Bij de coding bot staat de knop **Log in bij Claude**. Die start op het scherm van de
  persoonlijke sandbox een `xterm` met `claude` (de gebruiker typt daar `/login`), en opent het
  interactieve scherm (noVNC, bestaat al). De gebruiker keurt de login goed in Chrome op hetzelfde
  scherm; de terugkoppeling naar `localhost` komt in dezelfde container aan. Lukt dat niet, dan
  plakt de gebruiker de code die Claude toont in de terminal.
- Claude Code bewaart de login zelf in `CLAUDE_CONFIG_DIR` op het volume
  (`/workspace/.config/claude`) en ververst zijn tokens zelf. Runs gebruiken die login
  automatisch omdat ze dezelfde `CLAUDE_CONFIG_DIR` hebben.
- Het scherm hoort in de huidige code bij een bot (display `:100+bot_id`). De knop gebruikt het
  scherm van de coding bot zelf; daarvoor krijgt de coding bot `computer_use`-schermen zonder dat
  het model screenshot-tools krijgt (de builder kiest hoe).
- Status: `GET /api/bots/{id}/claude-login` zegt of er een login is (bijvoorbeeld door in de
  sandbox `claude auth status` of het bestaan van het credentials-bestand te controleren; de
  builder kiest wat betrouwbaar is). Zonder login meldt een run dat in de chat, met een verwijzing
  naar de knop.

### De bot

- Nieuw veld `Bot.runner: str = ""` (`""` = gewone bot, `"claude_code"` = coding bot), met een
  migratie zoals de bestaande `_ensure_*_columns`.
- Een coding bot is persoonlijk: `team_id = 0`, `is_home = False`, `is_team = False` en kan niet
  gepubliceerd of gedeeld worden. `access.bot_role` geeft voor zo'n bot alleen de eigenaar
  toegang; de API moet weigeren `is_team`, `published` of `BotAccess` voor een coding bot te
  zetten. Hij krijgt een eigen gesprek per bot (zoals de Overview-bot).
- `home.sandbox_key(bot)` geeft `home-u{owner_id}` voor een coding bot, zodat hij dezelfde
  persoonlijke sandbox gebruikt als de Overview-bot.
- Instellingen van de bot: naam, model (`--model`, standaard `opus`), werkmap (relatief aan
  `/workspace`, standaard `/workspace`), en eigen instructies (meegegeven met
  `--append-system-prompt`).
- `endpoint_id` is voor een coding bot niet van toepassing; het veld is verplicht in het model,
  dus de builder kiest een nette oplossing (bijvoorbeeld 0 en overal overslaan waar een endpoint
  gelezen wordt).
- In de zijbalk staan coding bots onder Overview. Aanmaken met **New coding bot**, alleen als
  `TB_CODING_BOTS=1`.

### Een run

Nieuwe tabel `CodingRun(id, bot_id, conversation_id, requester_id, session_id, log_path, pid,
offset, status, started_at, ended_at)`. `status`: `running | done | failed | stopped`.

Starten (bij een nieuw bericht aan een coding bot, als er geen run loopt):

1. Controleer of er een Claude-login is; zonder login een duidelijke melding in de chat.
2. `log_path = .coding/run-<id>.jsonl` op het volume.
3. `sb_spawn` met een klein shell-script dat:
   - het PID wegschrijft naar `.coding/run-<id>.pid`,
   - `claude -p "<bericht>" --output-format stream-json --verbose --model <model>
     --permission-mode bypassPermissions [--resume <session_id>] [--append-system-prompt ...]`
     draait in de werkmap, met stdout naar het logbestand en stderr naar `.coding/run-<id>.err`,
   - na afloop de exitcode wegschrijft naar `.coding/run-<id>.exit`.
   Het bericht en de instructies gaan via een bestand of stdin naar het script, niet via de
   shell-regel, om quoting- en injectieproblemen te vermijden.
   `CLAUDE_CONFIG_DIR` gaat mee via `env` (staat ook in het image).
4. De app start een volgtaak (asyncio) voor de run.

Volgen:

- Elke ~1 s: `sb_exec` met `tail -c +<offset+1>` op het logbestand (begrensd in grootte). Alleen
  hele regels verwerken; `offset` bijwerken in de database na elke verwerkte batch.
- Event-mapping naar de bestaande `Message`-rijen, zodat de UI ze zonder grote wijzigingen
  toont:
  - `system/init`: `session_id` opslaan.
  - `assistant` met tekst: assistant-bericht.
  - `assistant` met `tool_use`: assistant-bericht met `tool_calls` (naam + argumenten als JSON).
  - `user` met `tool_result`: tool-bericht met `tool_call_id`, ingekort tot een redelijke lengte.
  - `result`: slotbericht (als de tekst nog niet getoond is), tokens en `total_cost_usd` naar
    `usage` loggen, status `done` of `failed`.
- Als het proces weg is (geen `result`, wel een `.exit`-bestand of geen levend PID): status
  `failed` met de laatste regels uit `.err`.
- Na een herstart van de app: alle runs met `status = running` krijgen weer een volgtaak die
  verdergaat vanaf `offset`.

Herstarts: een run overleeft een herstart van de app (volgtaak gaat verder vanaf `offset`). Als
de sandbox-container opnieuw wordt aangemaakt, sterft het Claude-proces; de run wordt `failed`
met een duidelijke melding, en een nieuw bericht gaat verder met `--resume`. De wachtrij voor
berichten tijdens een run blijft in het geheugen (`agent._held`); gaat die bij een herstart van de
app verloren, dan meldt de chat dat en stuurt de gebruiker het bericht opnieuw.

Stoppen: de Stop-knop (bestaand) stuurt voor een coding bot een `kill` naar het PID in de
sandbox; status `stopped`. Een nieuw bericht tijdens een run komt in de wachtrij en start na
afloop een nieuwe run met `--resume <session_id>`.

### Sandbox-image

- Claude Code installeren in `sandbox/Dockerfile` (keuze tussen npm-pakket en de officiele
  installer laat ik aan de builder; de Node-versie in het image moet passen).
- `xterm` installeren voor de login.
- `CLAUDE_CONFIG_DIR` op het volume (`/workspace/.config/claude`), zodat login en sessies
  (`--resume`) een herstart van de container overleven.

### Veiligheid

- Het token staat als bestand van Claude Code in `/workspace/.config/claude` op het volume van de
  persoonlijke sandbox. TeamBots slaat het niet op en geeft het niet door.
- **Bewuste keuze van Ralf:** de coding bot deelt de sandbox met de eigen Overview-bot. Die kan
  altijd het credentials-bestand, `.coding/` en de sessies lezen, en dat aan zijn model geven
  (mogelijk een externe provider). Root op de host kan er ook bij.
  Andere gebruikers kunnen er niet bij: de persoonlijke sandbox en beide bots zijn alleen van de
  eigenaar. De Overview-bot mag het coding-gesprek ook tonen via `read_messages`.
- `bypassPermissions` is aanvaardbaar omdat de sandbox geisoleerd is (gVisor, geen
  Docker-socket, persoonlijk volume). Netwerk is wel open, dus Claude kan alles wat de
  eigenaar met zijn GitHub-login in die sandbox kan.
- Tool-resultaten en tekst uit de run gaan door `secrets_http.mask` voor de opslag, zodat
  opgeslagen TeamBots-secrets niet in de chat komen.
- Alleen de eigenaar mag de bot gebruiken, het gesprek lezen en een run stoppen.

## Wat ik zelf heb besloten (te controleren)

- Login via `/login` van Claude Code in een `xterm` op het scherm van de sandbox (op voorstel van
  Ralf), geen token in TeamBots.
- Een aparte coding bot onder Overview, niet de Overview-bot zelf omzetten.
- `bypassPermissions` als standaard, geen goedkeuringskaarten in v1.
- Een bericht tijdens een run gaat in de wachtrij; geen bijsturen midden in een run.
- Standaardmodel `opus`.
- `TB_CODING_BOTS` als schakelaar, standaard uit.
- Een opnieuw aangemaakte container maakt de run `failed`; de wachtrij blijft in het geheugen.

## Aan de builder

- Precieze UI-details (formulier, weergave van tool-stappen, run-status in de zijbalk).
- Het shell-script en de manier om het bericht veilig mee te geven.
- Grenzen: maximale run-duur, maximale grootte per `tail`, inkorten van tool-resultaten.
- Hoe het `local`-backend het PID en kill afhandelt.
- Tests: een nep-`claude` die stream-json uitspuugt (zoals de bestaande nep-binary in
  `tests/test_claude_cli.py`), voor start, volgen, herstart, stoppen en resume.
