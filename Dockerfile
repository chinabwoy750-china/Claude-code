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

# Install Claude Code as root
RUN curl -fsSL https://claude.ai/install.sh | bash

# Verify installation
RUN /root/.local/bin/claude --version

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Create non-root user and workspace
RUN useradd -m -s /bin/bash claude \
    && mkdir -p /workspace /home/claude/.local/bin \
    && cp /root/.local/bin/claude /home/claude/.local/bin/claude \
    && chown -R claude:claude /app /workspace /home/claude

# Run Claude Code as the non-root user
ENV PATH="/home/claude/.local/bin:${PATH}"

USER claude

WORKDIR /workspace

EXPOSE 10000

CMD ["sh", "-c", "uvicorn server:app --app-dir /app --host 0.0.0.0 --port ${PORT:-10000}"]