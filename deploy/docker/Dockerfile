# syntax=docker/dockerfile:1
# agent-memory 应用镜像：HTTP 在线服务 + 进程内 bge 嵌入/精排。
#
# 注意：含 CPU 版 torch + FlagEmbedding（纯 CPU 推理，不装 CUDA 库，镜像约 5–6GB）。
# 若改用「独立 TEI 推理服务」，可去掉下方 pip 安装里的 [embed]，并在 config.yml 把
# embedder 改 openai、reranker 改 api。
FROM python:3.11-slim

# 不设 PIP_NO_CACHE_DIR：配合下方 BuildKit 缓存挂载复用已下的 wheel，重建不再联网重下。
ENV PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

WORKDIR /app

# 构建期系统依赖（部分科学计算包需要编译工具链）
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# 依赖 + 本地包安装：用 BuildKit pip 缓存挂载。项目用 editable 安装，site-packages
# 只保留指向 /app/src 的链接；compose 挂载 src 后会被同一个链接读取。
COPY pyproject.toml ./
COPY src ./src
# 先从 PyTorch CPU 索引装 CPU 版 torch（x86_64/aarch64 均有 +cpu wheel），避免默认拉 CUDA
# 版 torch 连带 nvidia(~2.9G)+triton(~652M) 等 GPU 库——本服务为纯 CPU 推理，这些用不上。
# torch 满足后，下面 .[embed,deploy] 不会再装 CUDA 变体。CPU 约束只落在部署镜像：pyproject
# 仍是通用 torch>=2.0，GPU/Mac/其它平台按各自情况安装，不受影响。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip \
    && pip install torch==2.12.1+cpu --extra-index-url https://download.pytorch.org/whl/cpu \
    && pip install -e ".[embed,deploy]"

ENV HF_HOME=/models \
    HF_ENDPOINT=https://huggingface.co

# 调用层（HTTP surface）与脚本
COPY bootstrap ./bootstrap
COPY scripts ./scripts

EXPOSE 8137

# 配置经挂载卷提供（compose 把 ./config.yml 挂到 /config/config.yml）
CMD ["python", "bootstrap/http_server/__main__.py", \
     "--host", "0.0.0.0", "--port", "8137", "/config/config.yml"]
