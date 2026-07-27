#!/usr/bin/env bash
# Docker 一键启动 platform + server
# 用法：
#   ./start-docker.sh            构建并后台启动
#   ./start-docker.sh --no-build 用已构建镜像直接启动（改代码后不重 build 时用）
#   ./start-docker.sh --rebuild  强制 --no-cache 重新构建
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT" || exit 1

GREEN=$'\033[32m'; CYAN=$'\033[36m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; NC=$'\033[0m'
info()  { printf "${CYAN}[INFO]${NC}  %s\n" "$*"; }
ok()    { printf "${GREEN}[OK]${NC}    %s\n" "$*"; }
warn()  { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
die()   { printf "${RED}[ERR]${NC}   %s\n" "$*"; exit 1; }

# 前置检查
command -v docker >/dev/null 2>&1 || die "未找到 docker，请先安装 Docker"
if ! docker info >/dev/null 2>&1; then
    die "docker daemon 未运行（Linux 需 sudo systemctl start docker；Windows 需启动 Docker Desktop）"
fi

BUILD_ARGS=()
NO_CACHE=0
case "${1:-}" in
    --no-build) BUILD_ARGS+=(--no-build) ;;
    --rebuild)  BUILD_ARGS+=(--build); NO_CACHE=1 ;;
    "")         BUILD_ARGS+=(--build) ;;
    *) die "未知参数: $1（可用: --no-build / --rebuild）" ;;
esac

# 确保 data 目录存在（否则 docker 挂载会创建一个匿名目录，权限还可能不对）
mkdir -p platform/data logs

info "在 $REPO_ROOT 启动 docker compose ..."

BUILD_FLAGS=()
(( NO_CACHE == 1 )) && BUILD_FLAGS+=(--no-cache)

if [[ " ${BUILD_ARGS[*]} " == *" --no-build "* ]]; then
    info "跳过构建，直接启动现有镜像"
    docker compose up -d || die "docker compose up 失败"
else
    info "构建镜像并启动（首次较慢，Maven 拉依赖可能几分钟）..."
    docker compose build "${BUILD_FLAGS[@]}" || die "docker compose build 失败"
    docker compose up -d || die "docker compose up 失败"
fi

# 等待端口就绪
wait_http() {
    local url="$1" name="$2" tries=0 max="${3:-90}"
    info "等待 ${name} 就绪 (${url}) ..."
    while (( tries < max )); do
        # 只要有 HTTP 响应（任何状态码，包括 401/404）就认为服务已起来
        local code
        code="$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo 000)"
        if [[ "$code" != "000" ]]; then
            ok "${name} 已就绪 (HTTP ${code})"
            return 0
        fi
        sleep 2; tries=$((tries+1))
    done
    warn "${name} 在 $((max*2))s 内未就绪，请查日志: docker compose logs ${name}"
    return 1
}

wait_http "http://localhost:9000/api/v1/auth/login" "platform" 90 || true
wait_http "http://localhost:5173/"                 "server"   30 || true

printf "\n${GREEN}================ Docker 启动完成 ================${NC}\n"
printf "前端 (nginx)      : http://localhost:5173\n"
printf "  -> /api/* 反代到 platform:9000\n"
printf "平台后端 (Spring) : http://localhost:9000\n"
printf "  -> SQLite:   ./platform/data/platform.db\n"
printf "  -> 记忆服务: \${MEMORY_SERVICE_BASE_URL:-http://host.docker.internal:8000}\n"
printf "查看日志          : docker compose logs -f\n"
printf "停止              : ./stop-docker.sh\n"
printf "=================================================${NC}\n\n"
