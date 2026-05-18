FROM python:3.12-slim

ARG CLAUDE_CODE_VERSION=latest

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/opt/data \
    XDG_CONFIG_HOME=/opt/data/.config \
    ORCHESTRATOR_DB=/opt/data/orchestrator.db \
    PATH=/usr/local/bin:/usr/bin:/bin:/opt/data/.npm-global/bin \
    OLLAMA_LOCAL_URL=http://127.0.0.1:11434 \
    TMPDIR=/opt/data/tmp

# system deps + node (for claude CLI) + curl (healthchecks) + kubectl
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        dnsutils \
        file \
        git \
        gnupg \
        gzip \
        iproute2 \
        iputils-ping \
        jq \
        less \
        netcat-openbsd \
        openssh-client \
        postgresql-client \
        procps \
        ripgrep \
        rsync \
        sqlite3 \
        tar \
        tini \
        tree \
        unzip \
        yq \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && KUBECTL_VERSION="$(curl -fsSL https://dl.k8s.io/release/stable.txt)" \
    && echo "Installing kubectl ${KUBECTL_VERSION}" \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && kubectl version --client --output=yaml | head -3 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# python deps
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app code
COPY server.py bot.py db.py sessions.py orchestrator_jobs.py ./
RUN chmod +x /app/orchestrator_jobs.py \
    && ln -s /app/orchestrator_jobs.py /usr/local/bin/orchestrator-jobs
COPY static/ ./static/

# data dir + user for uid 1000 (set via k8s securityContext)
RUN echo 'orchestrator:x:1000:0:Orchestrator:/opt/data:/bin/bash' >> /etc/passwd \
    && mkdir -p /opt/data \
    && chown -R 1000:0 /opt/data /app \
    && chmod -R g=u /opt/data /app

USER orchestrator
EXPOSE 8765

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8765"]
