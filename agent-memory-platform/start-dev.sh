#!/usr/bin/env bash
# 启动运维中心前端 (server/, vite :5173) + 平台后端 (platform/, Spring Boot :9000)
# 跨平台：自动检测 OS，Windows 用 netstat+taskkill，Unix 用 lsof+kill。
set -uo pipefail

# ---------- 可配置项（可用环境变量覆盖） ----------
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
PLATFORM_PORT="${PLATFORM_PORT:-9000}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$REPO_ROOT/server"
PLATFORM_DIR="$REPO_ROOT/platform"
export LOG_DIR="$REPO_ROOT/logs"
FRONTEND_LOG="$LOG_DIR/frontend.log"
PLATFORM_LOG="$LOG_DIR/platform.log"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; NC=$'\033[0m'
info()  { printf "${CYAN}[INFO]${NC}  %s\n"  "$*"; }
ok()    { printf "${GREEN}[OK]${NC}    %s\n"  "$*"; }
warn()  { printf "${YELLOW}[WARN]${NC}  %s\n"  "$*"; }
die()   { printf "${RED}[ERR]${NC}   %s\n" "$*"; exit 1; }

mkdir -p "$LOG_DIR"

# =====================================================================
# 0) OS 检测
# =====================================================================
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

# =====================================================================
# 1) 环境检查：node / npm / java(>=17) / mvn
# =====================================================================
check_env() {
    info "检测到 OS: $OS"
    info "检查运行环境..."

    command -v node >/dev/null 2>&1 || die "未找到 node，请先安装 Node.js (建议 18+)"
    command -v npm  >/dev/null 2>&1 || die "未找到 npm，请随 Node.js 一并安装"
    command -v java >/dev/null 2>&1 || die "未找到 java，平台后端需 JDK 17+"
    command -v mvn  >/dev/null 2>&1 || die "未找到 mvn，platform/ 无 maven-wrapper，需系统已装 Maven"

    local node_v npm_v java_v mvn_v
    node_v="$(node -v 2>/dev/null)"
    npm_v="$(npm -v 2>/dev/null)"
    java_v="$(java -version 2>&1 | head -1)"
    mvn_v="$(mvn -v 2>/dev/null | head -1)"
    ok "node $node_v / npm $npm_v"
    ok "$java_v"
    ok "$mvn_v"

    # 校验 Java 主版本 >= 17（兼容 "21.0.11" 与老式 "1.8.0_432"）
    local jver jmajor
    jver=$(java -version 2>&1 | head -1 | sed -nE 's/.*"([0-9][0-9._A-Za-z+-]*)".*/\1/p' | head -1)
    if [[ -z "$jver" ]]; then
        die "无法识别 Java 版本，请检查 java -version 输出"
    fi
    if [[ "$jver" == 1.* ]]; then
        # 老式 1.8.0_xxx：主版本取小数点后第一段
        jmajor="${jver#1.}"; jmajor="${jmajor%%.*}"
    else
        jmajor="${jver%%.*}"
    fi
    if [[ -z "$jmajor" ]] || ! [[ "$jmajor" =~ ^[0-9]+$ ]] || (( jmajor < 17 )); then
        die "Java 主版本需 >= 17，当前识别为 '${jmajor:-未知}'（原始: $jver）"
    fi
    ok "Java 主版本 $jmajor >= 17"

    # 前端依赖：node_modules 缺失则自动安装
    if [[ ! -d "$SERVER_DIR/node_modules" ]]; then
        warn "server/node_modules 不存在，执行 npm install（首次较慢）..."
        ( cd "$SERVER_DIR" && npm install ) || die "前端依赖安装失败"
        ok "前端依赖安装完成"
    else
        ok "前端依赖已就绪 (server/node_modules)"
    fi
}

# =====================================================================
# 2) 按端口杀已存在进程（OS 分支）
# =====================================================================
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

kill_port() {
    local port="$1" pids=""
    case "$OS" in
        windows) pids="$(port_pids_windows "$port")" ;;
        *)       pids="$(port_pids_unix "$port")" ;;
    esac

    if [[ -z "$pids" ]]; then
        ok "端口 ${port} 无占用进程"
        return 0
    fi
    warn "端口 ${port} 已被占用，PID: $(echo $pids | tr '\n' ' ')，先杀掉"

    local pid
    for pid in $pids; do
        [[ -z "$pid" ]] && continue
        if [[ "$OS" == "windows" ]]; then
            # Git Bash 下 // 转义为单 /；/T 连带子进程一起杀
            taskkill //F //T //PID "$pid" >/dev/null 2>&1 || true
        else
            kill -9 "$pid" 2>/dev/null || true
        fi
    done

    sleep 1
    local still=""
    case "$OS" in
        windows) still="$(port_pids_windows "$port")" ;;
        *)       still="$(port_pids_unix "$port")" ;;
    esac
    if [[ -n "$still" ]]; then
        warn "端口 ${port} 仍被 PID: $(echo $still | tr '\n' ' ') 占用，可能是系统进程，请手动处理"
        return 1
    fi
    ok "端口 ${port} 已释放"
}

kill_existing() {
    info "检查并清理已存在的进程..."
    kill_port "$FRONTEND_PORT"
    kill_port "$PLATFORM_PORT"
}

# =====================================================================
# 3) 启动平台后端与前端
# =====================================================================
start_platform() {
    info "启动平台后端 (platform, Spring Boot, :${PLATFORM_PORT})..."
    echo -e "\n========== platform 启动 @ $(date '+%Y-%m-%d %H:%M:%S') ==========\n" >> "$PLATFORM_LOG"
    ( cd "$PLATFORM_DIR" && exec nohup mvn spring-boot:run >>"$PLATFORM_LOG" 2>&1 ) &
    echo $! > "$LOG_DIR/platform.pid"
    ok "平台后端已后台启动，日志: $PLATFORM_LOG (pid=$(cat "$LOG_DIR/platform.pid"))"
}

start_frontend() {
    info "启动前端 (server, Vite, :${FRONTEND_PORT})..."
    echo -e "\n========== frontend 启动 @ $(date '+%Y-%m-%d %H:%M:%S') ==========\n" >> "$FRONTEND_LOG"
    ( cd "$SERVER_DIR" && exec nohup npm run dev >>"$FRONTEND_LOG" 2>&1 ) &
    echo $! > "$LOG_DIR/frontend.pid"
    ok "前端已后台启动，日志: $FRONTEND_LOG (pid=$(cat "$LOG_DIR/frontend.pid"))"
}

# =====================================================================
# 4) 等待端口就绪
# =====================================================================
port_listening() {
    case "$OS" in
        windows) netstat -ano 2>/dev/null | grep -E "LISTENING" | grep -qE "[: ]${1}[^0-9]" ;;
        *)       command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1 ;;
    esac
}
wait_port() {
    local port="$1" name="$2" tries=0 max="${3:-120}"
    info "等待 ${name} (端口 ${port}) 就绪..."
    while (( tries < max )); do
        if port_listening "$port"; then
            ok "${name} 已在 ${port} 监听"
            return 0
        fi
        sleep 2; tries=$((tries+1))
    done
    warn "${name} 在 $((max*2))s 内未监听 ${port}，请查日志"
    return 1
}

print_summary() {
    printf "\n${GREEN}================ 启动完成 ================${NC}\n"
    printf "前端 (Vite)        : http://localhost:${FRONTEND_PORT}\n"
    printf "  -> 前端日志     : %s\n" "$FRONTEND_LOG"
    printf "平台后端 (Spring) : http://localhost:${PLATFORM_PORT}\n"
    printf "  -> 平台日志     : %s\n" "$PLATFORM_LOG"
    printf "停止               : 重新运行本脚本会先杀掉旧进程；或按 logs/*.pid 手动 kill\n"
    printf "============================================${NC}\n\n"
}

main() {
    info "仓库根目录: $REPO_ROOT"
    detect_os
    check_env
    kill_existing
    start_platform
    start_frontend
    wait_port "$PLATFORM_PORT" "平台后端" 150 || true
    wait_port "$FRONTEND_PORT" "前端"     60  || true
    print_summary
}

main "$@"
