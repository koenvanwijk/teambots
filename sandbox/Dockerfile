# The project computer. One container per project; every bot gets its own virtual screen in it.
# Add the tools your bots need here.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git jq ca-certificates nodejs npm \
    xvfb xdotool imagemagick chromium fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 bot && mkdir /workspace && chown bot:bot /workspace
USER bot
WORKDIR /workspace
