# syntax=docker/dockerfile:1
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# agent-memory application image for remote embedding/rerank services.
#
# This image intentionally does not install torch / FlagEmbedding / transformers
# or the project [embed] extra. Embedding and rerank are provided by external HTTP
# services configured in config.yml.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY jiuwen_memory ./jiuwen_memory

# [deploy] provides Milvus / Elasticsearch / Redis / YAML clients.
# httpx is required by common.reranker.reranker_impl.api_reranker.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip \
    && pip install -e ".[deploy]" "httpx>=0.27,<1"

COPY bootstrap ./bootstrap
COPY scripts ./scripts

EXPOSE 8137

CMD ["python", "bootstrap/http_server/__main__.py", \
     "--host", "0.0.0.0", "--port", "8137", "/config/config.yml"]
