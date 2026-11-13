#!/usr/bin/env sh
# Start TeamBots on a Linux machine with Docker Engine and the compose plugin:  ./start.sh
set -e
command -v docker >/dev/null || { echo "Docker is not installed." >&2; exit 1; }
if [ ! -f .env ]; then
  echo "TB_SECRET_KEY=$(openssl rand -hex 32)" > .env
  chmod 600 .env
  echo "Created .env with a new TB_SECRET_KEY. Keep this file: it decrypts your stored keys and secrets."
fi
docker compose up -d --build
echo "TeamBots runs at http://localhost:8080"
echo "A model server on this machine is reachable from the app as http://host.docker.internal:<port>/v1"
