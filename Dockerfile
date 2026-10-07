FROM node:20-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PORT=10000
ENV PATH="/home/node/.local/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    jq \
    ca-certificates \
    less \
    procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Claude Code using Anthropic's current native installer.
USER node
RUN curl -fsSL https://claude.ai/install.sh | bash
RUN claude --version

USER root
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

RUN mkdir -p /workspace && chown -R node:node /workspace /app

USER node
WORKDIR /workspace

EXPOSE 10000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000}"]
