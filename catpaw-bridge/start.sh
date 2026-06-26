#!/usr/bin/env bash
# =============================================================================
# CatPawAI Bridge - 一键启动脚本
# =============================================================================
# 启动 CatPawAI 反向代理 + CLIProxyAPI，将 CatPawAI 的 glm-5.2 模型
# 暴露给 Claude Code 使用。
#
# 用法:
#   ./start.sh          # 启动所有服务
#   ./start.sh --stop   # 停止所有服务
#   ./start.sh --status # 查看状态
# =============================================================================

set -euo pipefail

# ---- 配置：从 bridge.conf.yaml 读取（单一数据源）----
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$SCRIPT_DIR/bridge.conf.yaml"

# YAML 解析函数（使用 python3，已确保可用）
read_yaml() {
    local key="$1"
    python3 -c "
import yaml, os, sys
with open('$CONFIG_FILE') as f:
    cfg = yaml.safe_load(f) or {}
keys = '$key'.split('.')
val = cfg
for k in keys:
    val = val.get(k, {}) if isinstance(val, dict) else None
if val is None:
    sys.exit(1)
print(val if not isinstance(val, bool) else ('1' if val else '0'))
" 2>/dev/null
}

# 从配置文件读取，环境变量可覆盖
CLIPROXY_PORT="${CLIPROXY_PORT:-$(read_yaml cliproxy.port || echo 8317)}"
CATPAW_PROXY_PORT="${CATPAW_PROXY_PORT:-$(read_yaml catpaw_proxy.port || echo 9000)}"
CLIPROXY_API_KEY="${CLIPROXY_API_KEY:-$(read_yaml cliproxy.api_key || echo sk-catpaw-bridge-key)}"
MODEL_NAME="${MODEL_NAME:-$(read_yaml model.name || echo glm-5.2)}"
PID_DIR="$SCRIPT_DIR/.pids"
LOG_DIR="$SCRIPT_DIR/.logs"

mkdir -p "$PID_DIR" "$LOG_DIR"

# ---- 颜色输出 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ---- 检查依赖 ----
check_deps() {
    if ! command -v python3 &>/dev/null; then
        error "需要 python3，请先安装: brew install python3"
        exit 1
    fi
    # 检查 aiohttp
    if ! python3 -c "import aiohttp" 2>/dev/null; then
        info "安装 aiohttp..."
        pip3 install --break-system-packages aiohttp
    fi
    # 检查 pycryptodome
    if ! python3 -c "from Crypto.Cipher import AES" 2>/dev/null; then
        info "安装 pycryptodome (加密依赖)..."
        pip3 install --break-system-packages pycryptodome
    fi
    # 检查 pyyaml
    if ! python3 -c "import yaml" 2>/dev/null; then
        info "安装 pyyaml (配置解析依赖)..."
        pip3 install --break-system-packages pyyaml
    fi
    # 检查配置文件
    if [[ ! -f "$CONFIG_FILE" ]]; then
        warn "配置文件 $CONFIG_FILE 不存在，使用默认值"
    fi
}

# ---- 编译 CLIProxyAPI ----
build_cliproxy() {
    if [[ -f "$PROJECT_DIR/cli-proxy-api" ]]; then
        info "CLIProxyAPI 二进制已存在，跳过编译"
        return
    fi
    info "编译 CLIProxyAPI..."
    cd "$PROJECT_DIR"
    go build -o cli-proxy-api ./cmd/server/
}

# ---- 启动 CatPawAI 反向代理 ----
start_catpaw_proxy() {
    local pid_file="$PID_DIR/catpaw-proxy.pid"
    if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        warn "CatPawAI 反向代理已在运行 (PID: $(cat "$pid_file"))"
        return
    fi
    info "启动 CatPawAI 反向代理 (端口 $CATPAW_PROXY_PORT)..."
    python3 "$SCRIPT_DIR/catpaw_reverse_proxy.py" > "$LOG_DIR/catpaw-proxy.log" 2>&1 &
    echo $! > "$pid_file"
    sleep 2
    if kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        info "CatPawAI 反向代理已启动 (PID: $(cat "$pid_file"))"
        # 检查健康状态
        local health
        health=$(curl -s http://127.0.0.1:$CATPAW_PROXY_PORT/health 2>/dev/null || echo "failed")
        if echo "$health" | grep -q '"ok"'; then
            info "CatPawAI 认证状态: OK"
        else
            warn "CatPawAI 认证可能失败，查看日志: $LOG_DIR/catpaw-proxy.log"
        fi
    else
        error "CatPawAI 反向代理启动失败，查看日志: $LOG_DIR/catpaw-proxy.log"
        cat "$LOG_DIR/catpaw-proxy.log"
        exit 1
    fi
}

# ---- 启动 CLIProxyAPI ----
start_cliproxy() {
    local pid_file="$PID_DIR/cliproxy.pid"
    if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        warn "CLIProxyAPI 已在运行 (PID: $(cat "$pid_file"))"
        return
    fi
    info "启动 CLIProxyAPI (端口 $CLIPROXY_PORT)..."
    cd "$PROJECT_DIR"
    ./cli-proxy-api -config "$SCRIPT_DIR/config.yaml" > "$LOG_DIR/cliproxy.log" 2>&1 &
    echo $! > "$pid_file"
    sleep 2
    if kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        info "CLIProxyAPI 已启动 (PID: $(cat "$pid_file"))"
    else
        error "CLIProxyAPI 启动失败，查看日志: $LOG_DIR/cliproxy.log"
        cat "$LOG_DIR/cliproxy.log"
        exit 1
    fi
}

# ---- 停止所有服务 ----
stop_all() {
    for name in cliproxy catpaw-proxy; do
        local pid_file="$PID_DIR/$name.pid"
        if [[ -f "$pid_file" ]]; then
            local pid
            pid="$(cat "$pid_file")"
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid"
                info "已停止 $name (PID: $pid)"
            fi
            rm -f "$pid_file"
        fi
    done
}

# ---- 显示状态 ----
show_status() {
    echo ""
    echo -e "${CYAN}========================================${NC}"
    echo -e "${CYAN}  CatPawAI Bridge 状态${NC}"
    echo -e "${CYAN}========================================${NC}"
    echo ""
    for name in catpaw-proxy cliproxy; do
        local pid_file="$PID_DIR/$name.pid"
        local port
        case $name in
            catpaw-proxy) port=$CATPAW_PROXY_PORT ;;
            cliproxy) port=$CLIPROXY_PORT ;;
        esac
        if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
            echo -e "  ${GREEN}●${NC} $name  (PID: $(cat "$pid_file"), :$port)"
        else
            echo -e "  ${RED}○${NC} $name  (未运行, :$port)"
        fi
    done
    echo ""
}

# ---- 显示启动完成信息 ----
show_ready() {
    echo ""
    echo -e "${CYAN}========================================${NC}"
    echo -e "${CYAN}  CatPawAI Bridge 已启动${NC}"
    echo -e "${CYAN}========================================${NC}"
    echo ""
    echo -e "  CatPawAI 反向代理:  http://127.0.0.1:${CATPAW_PROXY_PORT}"
    echo -e "  CLIProxyAPI:        http://127.0.0.1:${CLIPROXY_PORT}"
    echo -e "  API Key:            ${CLIPROXY_API_KEY}"
    echo -e "  模型:               ${MODEL_NAME}"
    echo ""
    echo -e "${YELLOW}  在 Claude Code 中使用 (二选一):${NC}"
    echo ""
    echo -e "  ${GREEN}# 方式1: 环境变量直接设置${NC}"
    echo -e "  export ANTHROPIC_BASE_URL=http://127.0.0.1:${CLIPROXY_PORT}"
    echo -e "  export ANTHROPIC_AUTH_TOKEN=${CLIPROXY_API_KEY}"
    echo -e "  export ANTHROPIC_MODEL=${MODEL_NAME}"
    echo -e "  claude"
    echo ""
    echo -e "  ${GREEN}# 方式2: 使用 cc-switch 脚本${NC}"
    echo -e "  source $SCRIPT_DIR/cc-switch.sh"
    echo -e "  cc_switch catpaw"
    echo -e "  claude"
    echo ""
    echo -e "  ${GREEN}# 方式3: 使用 cc-switch 桌面应用${NC}"
    echo -e "  导入配置: $SCRIPT_DIR/cc-switch-profiles.json"
    echo ""
    echo -e "  日志目录: $LOG_DIR"
    echo -e "  停止服务: $0 --stop"
    echo -e "  查看状态: $1 --status"
    echo ""
}

# ---- 主逻辑 ----
main() {
    case "${1:-}" in
        --stop)
            stop_all
            exit 0
            ;;
        --status)
            show_status
            exit 0
            ;;
        "" )
            check_deps
            build_cliproxy
            start_catpaw_proxy
            start_cliproxy
            show_ready "$0"
            ;;
        *)
            echo "用法: $0 [--stop|--status]"
            exit 1
            ;;
    esac
}

main "$@"
