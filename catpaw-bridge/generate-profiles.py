#!/usr/bin/env python3
"""
从 bridge.conf.yaml 生成 cc-switch-profiles.json。

cc-switch 桌面应用只能导入 JSON 格式的配置文件，
此脚本从统一配置文件 bridge.conf.yaml 同步生成。

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


def main():
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    cliproxy = cfg.get("cliproxy", {})
    cc = cfg.get("claude_code", {})

    port = cliproxy.get("port", 8317)
    api_key = cliproxy.get("api_key", "sk-catpaw-bridge-key")

    base_url = f"http://127.0.0.1:{port}"
    model = cc.get("model", "glm-5.2")
    small_model = cc.get("small_model", model)
    masked_model = cc.get("masked_model", "claude-sonnet-4-20250514")
    masked_small_model = cc.get("masked_small_model", "claude-sonnet-4-20250514")
    official_base = cc.get("official_base_url", "https://api.anthropic.com")
    official_model = cc.get("official_model", "claude-sonnet-4-20250514")
    official_small = cc.get("official_small_model", "claude-3-5-haiku-20241022")

    profiles = {
        "profiles": {
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
        },
        "_comment": "此文件由 bridge.conf.yaml 同步生成。修改配置请编辑 bridge.conf.yaml 后重新运行 generate-profiles.py",
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(profiles, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"✅ 已生成 {OUTPUT_PATH}")
    print(f"   配置来源: {CONFIG_PATH}")


if __name__ == "__main__":
    main()
