#!/usr/bin/env python3
"""
从 bridge.conf.yaml 生成 cc-switch-profiles.json。

cc-switch 桌面应用只能导入 JSON 格式的配置文件，
此脚本从统一配置文件 bridge.conf.yaml 同步生成。

支持两种 Profile 类型:
- Claude Code (anthropic): 环境变量方式
- Codex CLI (codex): auth + config.toml 方式 (cc-switch v3.x 格式)

用法: python3 generate-profiles.py
"""

import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed. Run: pip3 install pyyaml", file=sys.stderr)
    sys.exit(1)

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "bridge.conf.yaml"
OUTPUT_PATH = SCRIPT_DIR / "cc-switch-profiles.json"


def _build_codex_config_toml(
    model: str,
    reasoning_effort: str,
    wire_api: str,
    base_url: str,
    provider_name: str = "CatPawAI Bridge",
    api_key: str = "",
) -> str:
    """Build a Codex config.toml string for cc-switch.

    cc-switch stores Codex provider config as a TOML string in the "config"
    field of settings_config. The "auth" field holds OPENAI_API_KEY separately.
    """
    # Determine whether to embed api_key in the provider section
    api_key_line = f'api_key = "{api_key}"\n' if api_key else ""

    return (
        f'model_provider = "custom"\n'
        f'model = "{model}"\n'
        f'model_reasoning_effort = "{reasoning_effort}"\n'
        f'disable_response_storage = true\n'
        f"\n"
        f"[model_providers.custom]\n"
        f'name = "{provider_name}"\n'
        f'base_url = "{base_url}"\n'
        f'wire_api = "{wire_api}"\n'
        f"requires_openai_auth = true\n"
        f"{api_key_line}"
    )


def main():
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    cliproxy = cfg.get("cliproxy", {})
    cc = cfg.get("claude_code", {})
    codex = cfg.get("codex", {})

    port = cliproxy.get("port", 8317)
    api_key = cliproxy.get("api_key", "sk-catpaw-bridge-key")

    base_url = f"http://127.0.0.1:{port}"

    # ---- Claude Code profiles ----
    model = cc.get("model", "glm-5.2")
    small_model = cc.get("small_model", model)
    masked_model = cc.get("masked_model", "claude-sonnet-4-20250514")
    masked_small_model = cc.get("masked_small_model", "claude-sonnet-4-20250514")
    official_base = cc.get("official_base_url", "https://api.anthropic.com")
    official_model = cc.get("official_model", "claude-sonnet-4-20250514")
    official_small = cc.get("official_small_model", "claude-3-5-haiku-20241022")

    # ---- Codex CLI profiles ----
    codex_model = codex.get("model", "glm-5.2")
    codex_reasoning = codex.get("reasoning_effort", "high")
    codex_wire_api = codex.get("wire_api", "responses")
    codex_official_model = codex.get("official_model", "gpt-4.1")
    codex_official_base = codex.get("official_base_url", "https://api.openai.com/v1")

    # Build Codex config.toml for cc-switch
    codex_catpaw_toml = _build_codex_config_toml(
        model=codex_model,
        reasoning_effort=codex_reasoning,
        wire_api=codex_wire_api,
        base_url=base_url,
        provider_name="CatPawAI Bridge",
    )

    codex_official_toml = _build_codex_config_toml(
        model=codex_official_model,
        reasoning_effort=codex_reasoning,
        wire_api="responses",
        base_url=codex_official_base,
        provider_name="OpenAI",
    )

    profiles = {
        "profiles": {
            # ---- Claude Code profiles ----
            "catpaw-glm52": {
                "name": f"CatPaw {model}",
                "anthropic": {
                    "ANTHROPIC_BASE_URL": base_url,
                    "ANTHROPIC_AUTH_TOKEN": api_key,
                    "ANTHROPIC_MODEL": model,
                    "ANTHROPIC_SMALL_FAST_MODEL": small_model,
                },
            },
            "catpaw-glm52-as-claude": {
                "name": f"CatPaw {model} (伪装 {masked_model})",
                "anthropic": {
                    "ANTHROPIC_BASE_URL": base_url,
                    "ANTHROPIC_AUTH_TOKEN": api_key,
                    "ANTHROPIC_MODEL": masked_model,
                    "ANTHROPIC_SMALL_FAST_MODEL": masked_small_model,
                },
            },
            "official-claude": {
                "name": "Claude 官方",
                "anthropic": {
                    "ANTHROPIC_BASE_URL": official_base,
                    "ANTHROPIC_AUTH_TOKEN": "sk-ant-your-official-key",
                    "ANTHROPIC_MODEL": official_model,
                    "ANTHROPIC_SMALL_FAST_MODEL": official_small,
                },
            },
            # ---- Codex CLI profiles (cc-switch v3.x format) ----
            # settings_config = {"auth": {"OPENAI_API_KEY": "..."}, "config": "<TOML string>"}
            "catpaw-codex": {
                "name": f"CatPaw Codex ({codex_model}, {codex_reasoning})",
                "codex": {
                    "auth": {
                        "OPENAI_API_KEY": api_key,
                    },
                    "config": codex_catpaw_toml,
                },
            },
            "official-codex": {
                "name": f"OpenAI 官方 ({codex_official_model})",
                "codex": {
                    "auth": {
                        "OPENAI_API_KEY": "sk-your-openai-key",
                    },
                    "config": codex_official_toml,
                },
            },
        },
        "_comment": "此文件由 bridge.conf.yaml 同步生成。修改配置请编辑 bridge.conf.yaml 后重新运行 generate-profiles.py",
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(profiles, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"✅ 已生成 {OUTPUT_PATH}")
    print(f"   配置来源: {CONFIG_PATH}")
    print(f"   包含 Profile:")
    print(f"     Claude Code: catpaw-glm52, catpaw-glm52-as-claude, official-claude")
    print(f"     Codex CLI:   catpaw-codex (model={codex_model}, reasoning={codex_reasoning}), official-codex")


if __name__ == "__main__":
    main()
