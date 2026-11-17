# The project computer. One container per project; every bot gets its own lightweight XFCE desktop.
# People can also open that desktop interactively (x11vnc + noVNC) through the TeamBots UI.
# Default browser is official Google Chrome stable; Debian Chromium stays installed as a fallback
# (set TB_BROWSER=chromium). Chrome runs as the unprivileged user `bot` and needs --no-sandbox
# in this image (capabilities are dropped). The profile lives on the project volume:
# /workspace/.screens/bot<id>
# Official gh (GitHub CLI) is installed from cli.github.com/packages; git is included too.
# No GitHub credentials are baked in — auth is per project (`gh auth login`).
# Add the tools your bots need here.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git jq ca-certificates gnupg \
    nodejs npm \
    xvfb xdotool imagemagick chromium fonts-liberation fonts-dejavu-core \
    x11vnc novnc websockify \
    xfce4-session xfce4-panel xfdesktop4 xfwm4 xfce4-terminal xfce4-appfinder \
    thunar mousepad dbus-x11 x11-utils \
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
# Official GitHub CLI from cli.github.com/packages. No credentials are baked in;
# `gh auth login` is per project. Config lives on the project volume so it survives
# container recreate (TeamBots secrets never enter the sandbox).
RUN mkdir -p -m 755 /etc/apt/keyrings /etc/apt/sources.list.d \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
         > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* \
    && command -v gh >/dev/null \
    && command -v git >/dev/null
ENV GH_CONFIG_DIR=/workspace/.config/gh
RUN useradd -m -u 1000 bot && mkdir /workspace && chown bot:bot /workspace
USER bot
WORKDIR /workspace
