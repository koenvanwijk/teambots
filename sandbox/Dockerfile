# The project computer. One container per project; every bot gets its own virtual screen in it.
# People can also open that same screen interactively (x11vnc + noVNC) through the TeamBots UI.
# Default browser is official Google Chrome stable; Debian Chromium stays installed as a fallback
# (set TB_BROWSER=chromium). Chrome runs as the unprivileged user `bot` and needs --no-sandbox
# in this image (capabilities are dropped). The profile lives on the project volume:
# /workspace/.screens/bot<id>
# Add the tools your bots need here.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git jq ca-certificates gnupg \
    nodejs npm \
    xvfb xdotool imagemagick chromium fonts-liberation fonts-dejavu-core \
    x11vnc novnc websockify \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
         | gpg --dearmor -o /usr/share/keyrings/google-linux.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
         > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/* \
    && (command -v import >/dev/null || ln -s /usr/bin/import-im6.q16 /usr/local/bin/import) \
    && command -v chromium >/dev/null \
    && command -v google-chrome >/dev/null \
    && command -v x11vnc >/dev/null \
    && command -v websockify >/dev/null
RUN useradd -m -u 1000 bot && mkdir /workspace && chown bot:bot /workspace
USER bot
WORKDIR /workspace
