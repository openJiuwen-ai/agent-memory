#!/usr/bin/env bash
# 停止并清理 platform + server 容器
# 用法：
#   ./stop-docker.sh           停止并删除容器、网络（保留数据卷）
#   ./stop-docker.sh --purge   连带删除 ./platform/data 下的 SQLite 文件
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT" || exit 1

GREEN=$'\033[32m'; CYAN=$'\033[36m'; YELLOW=$'\033[33m'; NC=$'\033[0m'
info() { printf "${CYAN}[INFO]${NC}  %s\n" "$*"; }
ok()   { printf "${GREEN}[OK]${NC}    %s\n" "$*"; }
warn() { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }

info "停止 docker compose ..."
docker compose down

if [[ "${1:-}" == "--purge" ]]; then
    warn "--purge: 清空 ./platform/data 与 ./logs"
    # 注意：这是删宿主目录里的文件，不是 docker volume
    rm -rf platform/data/* logs/*
    ok "数据已清空"
fi

ok "完成"
