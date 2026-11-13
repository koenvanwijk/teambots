# The project computer. One container per project; every bot gets its own virtual screen in it.
# People can also open that same screen interactively (x11vnc + noVNC) through the TeamBots UI.
# Add the tools your bots need here.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git jq ca-certificates nodejs npm \
    xvfb xdotool imagemagick chromium fonts-liberation fonts-dejavu-core \
    x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/* \
    && (command -v import >/dev/null || ln -s /usr/bin/import-im6.q16 /usr/local/bin/import) \
    && command -v chromium >/dev/null \
    && command -v x11vnc >/dev/null \
    && command -v websockify >/dev/null
RUN useradd -m -u 1000 bot && mkdir /workspace && chown bot:bot /workspace
USER bot
WORKDIR /workspace
