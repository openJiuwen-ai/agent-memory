#!/usr/bin/env bash
# 重新部署 agent-memory 容器（BuildKit 缓存 + 镜像源加速）
set -euo pipefail

# 定位 deploy 目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f "$SCRIPT_DIR/.env" ]; then
  echo "错误: 未找到 .env 配置文件，请先执行 cp .env.example .env 并填写配置" >&2
  exit 1
fi
# shellcheck disable=SC1091
source "$SCRIPT_DIR/.env"

# 配置（优先环境变量，回退到 .env）
AGENT_MEMORY_IMAGE="${AGENT_MEMORY_IMAGE:-agent-memory:latest}"
CONTAINER_NAME="${CONTAINER_NAME:-agent-memory}"
NETWORK_NAME="agent-memory-net"
MEMORY_PORT="${PORT:-8001}"
# 127.0.0.1=仅本机；0.0.0.0=暴露公网，谨慎
MEMORY_BIND_HOST="${HOST_IP:-[::]}"
CONTAINER_PORT="${CONTAINER_PORT:-8000}"
MEMORY_API_KEY="${MEMORY_API_KEY:-}"

export DOCKER_BUILDKIT=1

# 计时
SECONDS=0
timer_start() { SECONDS=0; }
timer_stop()  { local label="$1"; echo "  [计时] ${label}: ${SECONDS}s"; }

echo "=== 步骤1: 停止并移除当前容器 ==="
docker stop "$CONTAINER_NAME" 2>/dev/null || true
docker rm   "$CONTAINER_NAME" 2>/dev/null || true

echo "=== 步骤2: 创建 Docker 网络 ==="
docker network create "$NETWORK_NAME" 2>/dev/null || true

echo "=== 步骤3: 构建 agent-memory 镜像 ==="
timer_start
docker build \
  -f "$SCRIPT_DIR/Dockerfile" \
  -t "$AGENT_MEMORY_IMAGE" \
  "$SCRIPT_DIR/.."
timer_stop "镜像构建"

echo "=== 步骤4: 启动容器（${MEMORY_BIND_HOST}:${MEMORY_PORT}->${CONTAINER_PORT}） ==="
timer_start
docker run -d \
  --name "$CONTAINER_NAME" \
  --network "$NETWORK_NAME" \
  -p "${MEMORY_BIND_HOST}:${MEMORY_PORT}:${CONTAINER_PORT}" \
  --env-file "$SCRIPT_DIR/.env" \
  --restart unless-stopped \
  "$AGENT_MEMORY_IMAGE"
timer_stop "agent-memory 启动"

echo "等待健康检查..."
healthy=false
for _ in $(seq 1 30); do
  if curl -fsS "http://localhost:${MEMORY_PORT}/health" >/dev/null 2>&1; then
    healthy=true
    echo "agent-memory 健康检查通过"
    break
  fi
  sleep 2
done

if [ "$healthy" = false ]; then
  echo "错误: agent-memory 健康检查未通过，部署失败" >&2
  exit 1
fi

echo "=== 步骤5: 验证容器状态 ==="
docker ps --filter "name=$CONTAINER_NAME" \
  --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo "=== 步骤6: 测试 /health ==="
curl -s "http://localhost:${MEMORY_PORT}/health"
echo ""

echo "=== 部署完成 ==="
