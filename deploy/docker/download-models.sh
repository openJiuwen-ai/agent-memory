#!/usr/bin/env bash
# 预下载本地嵌入/精排模型到 ./models，供 compose 挂载到容器（/models-local）离线加载。
#
# 不在容器启动时联网下载：直连 HuggingFace 在部分网络环境下不稳定，且下载失败时索引
# 构建会静默降级（VectorIndexBuilder 捕获嵌入异常后记录日志并继续），导致写入看似成功
# 而召回为空。改为在宿主机通过 ModelScope 预先下载到目录路径，容器直接从磁盘加载，确保可用。
#
#   cd deploy/docker
#   ./download-models.sh            # 下载到 ./models/bge-m3 与 ./models/bge-reranker-v2-m3
#
# 目录已存在则跳过；删除对应目录可强制重新下载。
set -euo pipefail

cd "$(dirname "$0")"
DEST="./models"
EMBED_DIR="$DEST/bge-m3"
RERANK_DIR="$DEST/bge-reranker-v2-m3"

# 未安装 modelscope CLI 时，将其装入临时 venv，避免污染系统 Python。
if ! command -v modelscope >/dev/null 2>&1; then
  echo "[download-models] 未找到 modelscope，安装到 .venv-modelscope ..."
  python3 -m venv .venv-modelscope
  # shellcheck disable=SC1091
  source .venv-modelscope/bin/activate
  pip install -q --upgrade pip
  pip install -q modelscope
fi

download() {
  local repo="$1" dir="$2"
  if [ -d "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
    echo "[download-models] 已存在，跳过：$dir"
    return
  fi
  echo "[download-models] 下载 $repo → $dir"
  modelscope download --model "$repo" --local_dir "$dir"
}

download "BAAI/bge-m3"               "$EMBED_DIR"
download "BAAI/bge-reranker-v2-m3"   "$RERANK_DIR"

echo "[download-models] 完成。.env 中 EMBED_MODEL=/models-local/bge-m3、RERANK_MODEL=/models-local/bge-reranker-v2-m3 即指向此处。"
