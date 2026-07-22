# JiuwenMemory Dockerfile — Python FastAPI long-term memory engine.
# Called by spring-ai-ascend (Java) over HTTP.
# Entry: jiuwen_memory/server/memory_server.py (:8000).
# Data: SQLite + ChromaDB under /app/memory_data (mount a volume).
#
# Build:  docker build -t agent-memory:latest -f Dockerfile .
# Run:    docker run -d --name agent-memory -p 8001:8000 \
#           --env-file deploy/.env -v agent_memory_data:/app/memory_data \
#           agent-memory:latest

FROM python:3.11-slim AS base

# China apt mirror (Debian) for faster installs.
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
    || sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list 2>/dev/null \
    || true

# build-essential: C-extension wheels for chromadb/hnswlib; curl: healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# China pip mirror.
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple \
 && pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn

# Copy metadata first (layer cache).
COPY pyproject.toml README.md /app/

# Install deps + package (cached unless pyproject.toml changes).
RUN pip install --no-cache-dir ".[chromadb,sqlite,gaussdb,elasticsearch,server]"

# Copy source.
COPY jiuwen_memory/ /app/jiuwen_memory/

# Reinstall package only (deps already satisfied) to register entry points.
RUN pip install --no-cache-dir --no-deps ".[chromadb,sqlite,gaussdb,elasticsearch,server]"

# Non-root user with writable home (memory_server._load_env() creates ~/.jiuwenmemory/).
RUN groupadd -r app && useradd -r -g app -m -d /home/app app \
    && mkdir -p /home/app/.jiuwenmemory /app/memory_data \
    && chown -R app:app /home/app /app

ENV MEMORY_DATA_DIR=/app/memory_data \
    IP=0.0.0.0 \
    PORT=8000

EXPOSE 8000

USER app

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

WORKDIR /app

ENTRYPOINT ["memory-server"]
