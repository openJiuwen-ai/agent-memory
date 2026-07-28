#!/usr/bin/env bash
# 停止运维中心前端 (:5173) + 平台后端 (:9000)
# 双保险：先按 PID 文件杀，再按端口扫一遍兜底。跨平台：Windows 用 netstat+taskkill，Unix 用 lsof+kill。
set -uo pipefail

FRONTEND_PORT="${FRONTEND_PORT:-5173}"
PLATFORM_PORT="${PLATFORM_PORT:-9000}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$REPO_ROOT/logs"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; NC=$'\033[0m'
info()  { printf "${CYAN}[INFO]${NC}  %s\n"  "$*"; }
ok()    { printf "${GREEN}[OK]${NC}    %s\n"  "$*"; }
warn()  { printf "${YELLOW}[WARN]${NC}  %s\n"  "$*"; }

# ---------- OS 检测 ----------
detect_os() {
    case "$OSTYPE" in
        msys*|cygwin*|win32) OS=windows ;;
        linux*)              OS=linux ;;
        darwin*)             OS=mac ;;
        *)
            local u; u="$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')"
            case "$u" in
                *mingw*|*msys*|*cygwin*) OS=windows ;;
                *) OS="${u:-unknown}" ;;
            esac
            ;;
    esac
}

# ---------- 按端口查 PID ----------
port_pids_windows() {
    netstat -ano 2>/dev/null \
        | grep -E "LISTENING" \
        | grep -E "[: ]${1}[^0-9]" \
        | awk '{print $NF}' | sort -u
}
port_pids_unix() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -ti tcp:"$1" 2>/dev/null | sort -u
    elif command -v fuser >/dev/null 2>&1; then
        fuser "$1/tcp" 2>/dev/null | tr -s ' ' '\n' | sort -u
    fi
}

# ---------- 杀单个 PID ----------
kill_pid() {
    local pid="$1"
    [[ -z "$pid" ]] && return 0
    if [[ "$OS" == "windows" ]]; then
        taskkill //F //T //PID "$pid" >/dev/null 2>&1 || true
    else
        kill -9 "$pid" 2>/dev/null || true
    fi
}

# ---------- 1) 按 PID 文件杀 ----------
kill_by_pidfile() {
    local name="$1" pidfile="$2"
    if [[ ! -f "$pidfile" ]]; then
        ok "$name：无 PID 文件，跳过"
        return 0
    fi
    local pid; pid="$(tr -dc '0-9' < "$pidfile")"
    if [[ -z "$pid" ]]; then
        warn "$name：PID 文件为空，删除文件"
        rm -f "$pidfile"; return 0
    fi
    info "$name：PID 文件指向 $pid，杀掉"
    kill_pid "$pid"
    rm -f "$pidfile"
    ok "$name：PID 文件已处理"
}

# ---------- 2) 按端口兜底杀 ----------
kill_by_port() {
    local name="$1" port="$2" pids=""
    case "$OS" in
        windows) pids="$(port_pids_windows "$port")" ;;
        *)       pids="$(port_pids_unix "$port")" ;;
    esac
    if [[ -z "$pids" ]]; then
        ok "$name：端口 $port 无占用进程"
        return 0
    fi
    warn "$name：端口 $port 仍被 PID: $(echo $pids | tr '\n' ' ') 占用，兜底杀掉"
    local pid
    for pid in $pids; do kill_pid "$pid"; done
    sleep 1
    # 复核
    local still=""
    case "$OS" in
        windows) still="$(port_pids_windows "$port")" ;;
        *)       still="$(port_pids_unix "$port")" ;;
    esac
    if [[ -n "$still" ]]; then
        warn "$name：端口 $port 仍被 PID: $(echo $still | tr '\n' ' ') 占用，可能是系统进程，请手动处理"
    else
        ok "$name：端口 $port 已释放"
    fi
}

main() {
    info "检测到 OS: $OS"
    info "停止前端 (:${FRONTEND_PORT}) + 平台后端 (:${PLATFORM_PORT})..."
    # 平台先停（后端），前端后停，顺序无强约束
    kill_by_pidfile "平台后端" "$LOG_DIR/platform.pid"
    kill_by_port     "平台后端" "$PLATFORM_PORT"
    kill_by_pidfile "前端"     "$LOG_DIR/frontend.pid"
    kill_by_port     "前端"     "$FRONTEND_PORT"
    printf "${GREEN}[OK]${NC}    停止流程结束\n"
}

detect_os
main
