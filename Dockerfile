FROM python:3.12-slim
# The docker CLI (Debian 13: package docker-cli; docker.io no longer ships it) lets the app start one sandbox container per user/channel on the host daemon.
# curl is used to install the official Claude Code CLI; credentials are not baked in.
# git keeps the history of every project knowledge base (app/knowledge.py).
RUN apt-get update && apt-get install -y --no-install-recommends \
        docker-cli curl ca-certificates sqlite3 git \
    && curl -fsSL https://claude.ai/install.sh | bash \
    && ln -sf /root/.local/bin/claude /usr/local/bin/claude \
    && command -v claude && command -v docker \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV TB_DATA_DIR=/data TB_SANDBOX=docker TB_CLAUDE_CLI=/usr/local/bin/claude
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
