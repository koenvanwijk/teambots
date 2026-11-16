# TeamBots op een Hetzner-VPS

Deze gids beschrijft hoe je TeamBots op een VPS draait, naast de laptop als
terugval. De app luistert op poort 18080, alleen op localhost en op het
Tailscale-adres van de **host**. Er gaat geen publieke tunnel open en poort
18080 wordt niet op het publieke IP gezet.

## Architectuur in het kort

- **Tailscale draait op de host**, aangemeld met een interactieve
  `tailscale up` (een login-URL in de browser). Er is geen Tailscale-sidecar
  in Compose: een tweede node is overbodig, en het officiële image stopt
  wanneer `TS_AUTHKEY` leeg is — dat past niet bij een interactieve login.
- De **app-container** hangt aan het Docker-netwerk `teambots`. Uitgaand
  verkeer naar de tailnet (Spark `100.69.221.57` of `100.125.230.10`, Home
  Assistant `100.70.66.80:8123`) loopt via de Docker-gateway naar Tailscale
  op de host. We gebruiken niet `network_mode: host`: dan zou de app op
  `0.0.0.0` ook op het publieke NIC luisteren.
- **Sandboxes** (één container per project) hangen aan hetzelfde netwerk
  `teambots`. Zo blijft het interactieve scherm (noVNC) bereikbaar. Zet
  `TB_SANDBOX_NETWORK` niet terug naar `bridge`.
- Poort 18080 wordt gepubliceerd op `127.0.0.1` en op `TB_TAILSCALE_IP`
  (het IPv4-adres van `tailscale ip -4` op de VPS).
- Alleen de container **sandboxd** heeft de Docker-socket. Hij heeft geen
  netwerk en praat met de app via een Unix-socket op het volume
  `sandboxd-run`. De app zelf kan Docker niet aansturen.
- De database staat in `./data` (op de VPS: `/opt/teambots/data`), niet in
  een anonieme Docker-volume, zodat een proefrun en de overstap een gewone
  bestandskopie zijn.

## 1. Basisinstallatie van de server

Op een verse Ubuntu-server (x86, bij voorkeur 4 vCPU / 8 GB / 80 GB schijf):

1. Installeer Docker Engine met de compose-plugin (officiële Docker-repo),
   git, de GitHub CLI (`gh`) en Tailscale. Start de daemon, maar meld de
   machine nog niet aan tot stap 3.
2. Zet automatische beveiligingsupdates aan (`unattended-upgrades`).
3. Maak een gebruiker `teambots` in de groep `docker` en zet je SSH-sleutel
   ook op die gebruiker.
4. Zet de tijdzone op `Europe/Amsterdam` (`timedatectl set-timezone Europe/Amsterdam`).
5. Clone deze repo naar `/opt/teambots` (HTTPS is genoeg, het is een publieke
   repo):

   ```bash
   git clone https://github.com/koenvanwijk/teambots.git /opt/teambots
   ```

6. Bouw het sandbox-image (zonder Android-variant):

   ```bash
   cd /opt/teambots
   docker build -t teambots-sandbox:latest ./sandbox
   ```

Een schijf van 19 GB is krap voor Docker plus het sandbox-image (Chrome).
Plan een rootschijf van 80 GB of meer. Een extra Hetzner-volume dat al
aangekoppeld is hoef je niet te formatteren; gebruik het alleen als je
bewust Docker-data of backups daarheen verplaatst.

### gVisor voor de sandboxes

gVisor (`runsc`) geeft elke sandbox een eigen kernel in user space. Het werkt
op een Hetzner Cloud-VM (geen geneste virtualisatie nodig).

```bash
# Officiële apt-repo van gVisor
curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  > /etc/apt/sources.list.d/gvisor.list
apt-get update && apt-get install -y runsc
runsc install                      # voegt de runtime toe aan /etc/docker/daemon.json
systemctl restart docker           # herstart ook alle containers
docker run --rm --runtime=runsc hello-world   # controle
```

Zet daarna `TB_SANDBOX_RUNTIME=runsc` in `.env`. Bestaande sandboxes worden
bij het eerstvolgende gebruik onder gVisor opnieuw aangemaakt; de
werkruimte (het volume) blijft bewaard, lopende processen zoals een open
browser niet.

## 2. Compose-stack starten

```bash
cd /opt/teambots
cp .env.example .env
chmod 600 .env
# Vul TB_SECRET_KEY in (openssl rand -hex 32). Bij een latere overstap van de
# laptop gebruik je dezelfde sleutel als daar, anders zijn versleutelde
# geheimen niet meer te lezen.
# Zet TB_TAILSCALE_IP op het IPv4-adres van `sudo tailscale ip -4` (stap 3).
# Voor een rooktest zonder Tailscale mag je tijdelijk 127.0.0.1 invullen
# en de tweede publicatie weglaten, of eerst stap 3 doen.
mkdir -p data backups
docker compose up -d --build
```

Controleer dat de UI lokaal antwoordt:

```bash
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18080/
```

Zet de stack daarna weer uit tot de proefrun of de overstap als dit alleen
een rooktest was:

```bash
docker compose down
```

Laptop en lokale Docker Desktop gebruiken `docker-compose.local.yml`
(`./start.sh` of `.\start.ps1`). Daar is geen Tailscale nodig en blijft
de app op poort 8080 staan.

## 3. Tailscale op de host (interactieve login)

Een auth key is niet nodig. Op de VPS, als root of met sudo:

```bash
sudo tailscale up --hostname=teambots-vps --accept-dns=false
```

De opdracht geeft een login-URL. Open die in je eigen browser, meld je aan
bij Tailscale en keur het apparaat goed. Controleer daarna:

```bash
tailscale status
tailscale ip -4
```

Zet dat IPv4-adres in `/opt/teambots/.env` als `TB_TAILSCALE_IP=...` en
start of herstart de stack: `docker compose up -d`.

De UI is daarna bereikbaar op `http://<tailscale-ip>:18080` vanaf een
apparaat in dezelfde tailnet, en op `http://127.0.0.1:18080` op de server
zelf (of via SSH-tunnel). Poort 18080 staat niet open op het publieke IP.

Zodra SSH via Tailscale werkt, kun je poort 22 in de Hetzner-firewall
sluiten voor internet. Doe dat pas als die route is getest.

`TB_HTTP_ALLOW_PRIVATE=1` moet aan staan op de VPS. Anders weigert
`http_request` de Tailscale-adressen van de Spark en Home Assistant
(100.x telt als een privaat adres).

## 4. Proefrun met een kopie van de database

De laptop blijft hier gewoon draaien. Dit is nog geen overstap.

1. Op de laptop, met de app aan, maak je een consistente kopie:

   ```bash
   scripts/backup-db.sh
   ```

   Of kopieer `data/teambots.db` (en `-wal`/`-shm` als die er zijn) nadat je
   de app kort hebt stilgezet. Met `backup-db.sh` hoeft de app niet uit.
2. Zet de kopie op de VPS in `/opt/teambots/data/`.
3. Zet in de `.env` op de VPS **dezelfde** `TB_SECRET_KEY` als op de laptop.
4. Start de stack op de VPS en log in. Controleer of projecten, bots en
   kaarten kloppen. De Spark en Home Assistant werken als de host in de
   tailnet zit (stap 3) en het verkeer naar 100.x vanaf de VPS aankomt.
5. `docker compose down` op de VPS als de proef klaar is, zodat je niet per
   ongeluk twee actieve servers met dezelfde database houdt.

## 5. Overstap (ongeveer een kwartier)

1. Zet de app op de laptop stil (`./stop-local.sh` of het proces op poort
   18080). Laat andere diensten (Home Assistant, homey-live-server, de
   Tibber-emulator) met rust.
2. Maak een laatste backup op de laptop (`scripts/backup.sh` of
   `scripts/backup-db.sh`) en kopieer de database plus `.env` naar
   `/opt/teambots/` op de VPS. Hergebruik `TB_SECRET_KEY`. Zet
   `TB_TAILSCALE_IP` op het VPS-adres.
3. Start op de VPS: `docker compose up -d`.
4. Log in de app-container in bij Claude:

   ```bash
   docker compose exec app claude login
   ```

   De sessie blijft bewaard onder `/opt/teambots/data/claude-home`.
5. Zet `TB_PUBLIC_URL` op de URL die je in de browser opent en herstart de
   app als je OAuth (Google, GitHub, MCP) gebruikt; werk desnoods de
   redirect-URI's bij de providers bij.
6. Controleer vanaf je laptop via Tailscale of de UI, een sandbox en een
   model-aanroep werken.

Laat de laptop een week als terugval staan, maar houd de app daar uit zolang
de VPS de actieve database heeft.

## 6. Nachtelijke backup

Een systemd-timer zet elke nacht om 03:15 (Amsterdamse tijd, met een kleine
willekeurige vertraging) een online SQLite-kopie in `/opt/teambots/backups`
en bewaart de laatste 14 snapshots:

```bash
sudo cp /opt/teambots/deploy/systemd/teambots-backup.service \
        /opt/teambots/deploy/systemd/teambots-backup.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now teambots-backup.timer
```

Voor een complete snapshot (database, `.env` en projectvolumes) blijft
`scripts/backup.sh` beschikbaar. Kopieer backups ook van de server af.

## 7. Terug naar de laptop

1. Zet de VPS-stack stil: `docker compose down`.
2. Kopieer `/opt/teambots/data/teambots.db` (en een eventuele backup van
   `.env`) terug naar `~/teambots/data` op de laptop. Houd dezelfde
   `TB_SECRET_KEY`.
3. Start de laptop-app weer met `./start-local.sh`.
4. De VPS mag aan blijven staan; start de compose-stack daar niet opnieuw
   met dezelfde database.

## 8. Publieke HTTPS-tunnel (alleen met expliciet akkoord)

Zet geen poorten open naar internet en start geen publieke tunnel voordat
dat is afgesproken. Als je de UI via cloudflared (of Caddy) op het internet
zet, gelden deze instellingen:

- **Registratie is dicht.** Zodra het eerste account bestaat, weigert
  `POST /api/register` verzoeken zonder geldige project-invite. Deel de
  invite-link (`https://…/?invite=…`) met nieuwe mensen; bij het aanmaken
  van het account worden ze meteen lid van dat project. Zet
  `TB_OPEN_REGISTRATION=1` alleen als je bewust open inschrijving wilt.
- **`/docs`, `/redoc` en `/openapi.json` zijn uit**, tenzij je
  `TB_ENABLE_DOCS=1` zet.
- **Rate-limit** op `/api/login` en `/api/register`: standaard 10 pogingen
  per IP per minuut (`TB_AUTH_RATE_LIMIT`). De app gebruikt
  `CF-Connecting-IP` of `X-Forwarded-For` alleen als de directe peer een
  lokale of private proxy is (cloudflared of Caddy op de host).
- Zet `TB_PUBLIC_URL` op de https-URL van de tunnel en herstart de app,
  zodat OAuth-redirects kloppen.

Ook later, alleen met akkoord:

- De publieke SSH-poort sluiten zodra inloggen via Tailscale werkt.
- Eventueel de rootschijf vergroten of Docker-data naar een extra volume
  verplaatsen als de schijf vol raakt.
