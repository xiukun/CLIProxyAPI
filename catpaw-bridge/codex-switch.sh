#!/usr/bin/env bash
# =============================================================================
# Codex CLI 配置切换脚本
# =============================================================================
# 从 bridge.conf.yaml 读取统一配置，生成 ~/.codex/config.toml
#
# 用法:
#   source codex-switch.sh catpaw    # 切换到 CatPaw Bridge
#   source codex-switch.sh official  # 切换到 OpenAI 官方
#   source codex-switch.sh status    # 查看当前配置
# =============================================================================

# ---- 从 bridge.conf.yaml 读取配置 ----
_CODEX_SWITCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
_CODEX_CONFIG_FILE="$_CODEX_SWITCH_DIR/bridge.conf.yaml"

read_yaml() {
    local key="$1"
    local default="${2:-}"
    python3 -c "
import yaml, sys
try:
    with open('$_CODEX_CONFIG_FILE') as f:
        cfg = yaml.safe_load(f) or {}
    keys = '$key'.split('.')
    v = cfg
    for k in keys:
        v = v.get(k, {}) if isinstance(v, dict) else None
    if v is None:
        sys.exit(1)
    print(v if not isinstance(v, bool) else ('1' if v else '0'))
except Exception:
    sys.exit(1)
" 2>/dev/null || echo "$default"
}

CLIPROXY_HOST="${CLIPROXY_HOST:-127.0.0.1}"
CLIPROXY_PORT="${CLIPROXY_PORT:-$(read_yaml cliproxy.port 8317)}"
CLIPROXY_API_KEY="${CLIPROXY_API_KEY:-$(read_yaml cliproxy.api_key sk-catpaw-bridge-key)}"
_CODEX_MODEL="$(read_yaml codex.model glm-5.2)"
_CODEX_REASONING_EFFORT="$(read_yaml codex.reasoning_effort high)"
_CODEX_WIRE_API="$(read_yaml codex.wire_api responses)"
_CODEX_OFFICIAL_MODEL="$(read_yaml codex.official_model gpt-4.1)"
_CODEX_OFFICIAL_BASE_URL="$(read_yaml codex.official_base_url https://api.openai.com/v1)"

# Codex 配置目录
_CODEX_DIR="$HOME/.codex"
_CODEX_CONFIG="$_CODEX_DIR/config.toml"

# 备份当前配置
_backup_config() {
    if [[ -f "$_CODEX_CONFIG" ]]; then
        cp "$_CODEX_CONFIG" "$_CODEX_DIR/config.toml.bak" 2>/dev/null
    fi
}

# 写入 CatPaw Bridge 配置
_write_catpaw_config() {
    mkdir -p "$_CODEX_DIR"
    _backup_config
    cat > "$_CODEX_CONFIG" << TOML
# Codex CLI - CatPawAI Bridge 配置
# 由 codex-switch.sh 自动生成 ($(date '+%Y-%m-%d %H:%M:%S'))

model_provider = "catpaw"
model = "$_CODEX_MODEL"
model_reasoning_effort = "$_CODEX_REASONING_EFFORT"
wire_api = "$_CODEX_WIRE_API"
disable_response_storage = true

[model_providers.catpaw]
name = "CatPawAI Bridge"
base_url = "http://${CLIPROXY_HOST}:${CLIPROXY_PORT}"
wire_api = "$_CODEX_WIRE_API"
api_key = "${CLIPROXY_API_KEY}"
TOML
}

# 写入 OpenAI 官方配置
_write_official_config() {
    mkdir -p "$_CODEX_DIR"
    _backup_config
    cat > "$_CODEX_CONFIG" << TOML
# Codex CLI - OpenAI 官方配置
# 由 codex-switch.sh 自动生成 ($(date '+%Y-%m-%d %H:%M:%S'))

model_provider = "openai"
model = "$_CODEX_OFFICIAL_MODEL"
model_reasoning_effort = "$_CODEX_REASONING_EFFORT"

[model_providers.openai]
name = "OpenAI"
base_url = "$_CODEX_OFFICIAL_BASE_URL"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
TOML
}

codex_switch() {
    local profile="${1:-status}"

    case "$profile" in
        catpaw)
            _write_catpaw_config
            echo "✅ 已切换到: CatPawAI Bridge ($_CODEX_MODEL)"
            echo "   配置文件: $_CODEX_CONFIG"
            echo "   BASE_URL: http://${CLIPROXY_HOST}:${CLIPROXY_PORT}"
            echo "   MODEL:    $_CODEX_MODEL"
            echo "   REASONING: $_CODEX_REASONING_EFFORT"
            echo "   WIRE_API: $_CODEX_WIRE_API"
            echo "   API_KEY:  $CLIPROXY_API_KEY"
            echo ""
            echo "   直接运行: codex"
            ;;
        official)
            _write_official_config
            echo "✅ 已切换到: OpenAI 官方 ($_CODEX_OFFICIAL_MODEL)"
            echo "   配置文件: $_CODEX_CONFIG"
            echo "   ⚠️  请设置 OPENAI_API_KEY 为你的官方 API Key"
            echo ""
            echo "   export OPENAI_API_KEY=sk-your-key"
            echo "   codex"
            ;;
        status)
            echo "Codex CLI 配置状态:"
            echo "  配置文件: $_CODEX_CONFIG"
            if [[ -f "$_CODEX_CONFIG" ]]; then
                echo "  --- 当前配置 ---"
                cat "$_CODEX_CONFIG" | head -15
            else
                echo "  (未配置)"
            fi
            echo ""
            echo "  OPENAI_API_KEY: ${OPENAI_API_KEY:-未设置}"
            echo ""
            echo "可用配置:"
            echo "  catpaw    - CatPawAI Bridge ($_CODEX_MODEL)"
            echo "  official  - OpenAI 官方 ($_CODEX_OFFICIAL_MODEL)"
            ;;
        *)
            echo "用法: codex_switch <catpaw|official|status>"
            return 1
            ;;
    esac
}

# 如果直接执行而非 source，则调用函数
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    codex_switch "$@"
else
    alias cxs='codex_switch'
fi
