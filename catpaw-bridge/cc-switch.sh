#!/usr/bin/env bash
# =============================================================================
# Claude Code 配置切换脚本 (cc-switch 兼容)
# =============================================================================
# 从 bridge.conf.yaml 读取统一配置，无需手动维护多份配置。
#
# 用法:
#   source cc-switch.sh catpaw        # 切换到 CatPaw (直接模型名)
#   source cc-switch.sh catpaw-claude # 切换到 CatPaw (伪装 Claude Sonnet)
#   source cc-switch.sh official      # 切换到 Claude 官方
#   source cc-switch.sh status        # 查看当前状态
# =============================================================================

# ---- 从 bridge.conf.yaml 读取配置 ----
_CC_SWITCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
_CC_CONFIG_FILE="$_CC_SWITCH_DIR/bridge.conf.yaml"

read_yaml() {
    local key="$1"
    local default="${2:-}"
    local val
    val=$(python3 -c "
import yaml, sys
try:
    with open('$_CC_CONFIG_FILE') as f:
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
" 2>/dev/null) || echo "$default"
    echo "$val"
}

CLIPROXY_HOST="${CLIPROXY_HOST:-127.0.0.1}"
CLIPROXY_PORT="${CLIPROXY_PORT:-$(read_yaml cliproxy.port 8317)}"
CLIPROXY_API_KEY="${CLIPROXY_API_KEY:-$(read_yaml cliproxy.api_key sk-catpaw-bridge-key)}"

# 模型配置
_CATPAW_MODEL="$(read_yaml claude_code.model glm-5.2)"
_CATPAW_SMALL_MODEL="$(read_yaml claude_code.small_model glm-5.2)"
_CATPAW_MASKED_MODEL="$(read_yaml claude_code.masked_model claude-sonnet-4-20250514)"
_CATPAW_MASKED_SMALL_MODEL="$(read_yaml claude_code.masked_small_model claude-sonnet-4-20250514)"
_OFFICIAL_BASE_URL="$(read_yaml claude_code.official_base_url https://api.anthropic.com)"
_OFFICIAL_MODEL="$(read_yaml claude_code.official_model claude-sonnet-4-20250514)"
_OFFICIAL_SMALL_MODEL="$(read_yaml claude_code.official_small_model claude-3-5-haiku-20241022)"

cc_switch() {
    local profile="${1:-status}"
    local base_url=""
    local model=""
    local small_model=""
    local name=""

    case "$profile" in
        catpaw)
            name="CatPaw ($_CATPAW_MODEL)"
            base_url="http://${CLIPROXY_HOST}:${CLIPROXY_PORT}"
            model="$_CATPAW_MODEL"
            small_model="$_CATPAW_SMALL_MODEL"
            ;;
        catpaw-claude)
            name="CatPaw (伪装 $_CATPAW_MASKED_MODEL)"
            base_url="http://${CLIPROXY_HOST}:${CLIPROXY_PORT}"
            model="$_CATPAW_MASKED_MODEL"
            small_model="$_CATPAW_MASKED_SMALL_MODEL"
            ;;
        official)
            name="Claude 官方"
            base_url="$_OFFICIAL_BASE_URL"
            model="$_OFFICIAL_MODEL"
            small_model="$_OFFICIAL_SMALL_MODEL"
            echo "⚠️  请设置 ANTHROPIC_AUTH_TOKEN 为你的官方 API Key"
            export ANTHROPIC_BASE_URL="$base_url"
            export ANTHROPIC_MODEL="$model"
            export ANTHROPIC_SMALL_FAST_MODEL="$small_model"
            echo "✅ 已切换到: $name"
            echo "   BASE_URL: $base_url"
            echo "   MODEL: $model"
            return 0
            ;;
        status)
            echo "当前 Claude Code 配置:"
            echo "  ANTHROPIC_BASE_URL: ${ANTHROPIC_BASE_URL:-未设置}"
            echo "  ANTHROPIC_MODEL: ${ANTHROPIC_MODEL:-未设置}"
            echo "  ANTHROPIC_SMALL_FAST_MODEL: ${ANTHROPIC_SMALL_FAST_MODEL:-未设置}"
            echo ""
            echo "可用配置:"
            echo "  catpaw         - CatPaw ($_CATPAW_MODEL)"
            echo "  catpaw-claude  - CatPaw (伪装 $_CATPAW_MASKED_MODEL)"
            echo "  official       - Claude 官方 ($_OFFICIAL_MODEL)"
            return 0
            ;;
        *)
            echo "用法: cc_switch <catpaw|catpaw-claude|official|status>"
            return 1
            ;;
    esac

    export ANTHROPIC_BASE_URL="$base_url"
    export ANTHROPIC_AUTH_TOKEN="$CLIPROXY_API_KEY"
    export ANTHROPIC_MODEL="$model"
    export ANTHROPIC_SMALL_FAST_MODEL="$small_model"

    echo "✅ 已切换到: $name"
    echo "   BASE_URL: $base_url"
    echo "   MODEL: $model"
    echo "   API_KEY: $CLIPROXY_API_KEY"
}

# 如果直接执行而非 source，则调用函数
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    cc_switch "$@"
else
    # source 模式下创建别名
    alias ccs='cc_switch'
fi
