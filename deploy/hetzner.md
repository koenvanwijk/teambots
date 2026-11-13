# Running on a Hetzner server

Not executed or tested yet; this is the intended procedure.

1. Create a server (Ubuntu 24.04; CX32 or larger: each project sandbox may use 2 CPU / 2 GB when a
   browser is open). Use it only for TeamBots: the app controls the Docker daemon.
2. Point a DNS A record (e.g. `bots.example.com`) at the server.
3. In the Hetzner Cloud firewall allow inbound 22 (your IP only), 80 and 443. Nothing else.
4. Install Docker Engine with the compose plugin, then copy this folder to the server.
5. Create `.env`:

   ```
   TB_SECRET_KEY=<the same key as on the laptop if you move the data, otherwise: openssl rand -hex 32>
   TB_DOMAIN=bots.example.com
   ```

6. `docker compose --profile server up -d --build`. Caddy fetches the certificate; the app itself
   stays bound to 127.0.0.1.
7. Model endpoint: the server must be able to reach it. A model on your DGX Spark at home is not
   reachable from Hetzner without a tunnel or VPN (Tailscale/WireGuard), or use a hosted endpoint.

Moving data from the laptop: stop both sides, copy the `tb-data` volume (`teambots.db` and
`screens/`), and keep the same `TB_SECRET_KEY`. Project workspaces live in volumes `tb-p<id>`.

Before real client use: sign-in is username/password with tokens that do not expire. Put SSO or
at least token expiry in front first (see README, Security).
