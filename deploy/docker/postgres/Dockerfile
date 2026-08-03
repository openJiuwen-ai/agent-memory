# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip \
    && pip install -e ".[deploy]" "httpx>=0.27,<1"

COPY bootstrap ./bootstrap
COPY scripts ./scripts

RUN groupadd --gid 10001 agentmemory \
    && useradd --uid 10001 --gid agentmemory --create-home \
        --home-dir /home/agentmemory --shell /usr/sbin/nologin agentmemory

USER 10001:10001

EXPOSE 8137

CMD ["python", "bootstrap/http_server/__main__.py", \
     "--host", "0.0.0.0", "--port", "8137", "/config/config.yml"]
