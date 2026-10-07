FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PORT=10000
ENV PATH="/root/.local/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    ca-certificates \
    jq \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Claude Code using Anthropic's native installer
RUN curl -fsSL https://claude.ai/install.sh | bash

RUN claude --version

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

RUN mkdir -p /workspace

WORKDIR /workspace

EXPOSE 10000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000}"]pace

EXPOSE 10000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000}"]
