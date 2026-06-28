"""Configuration: load from bridge.conf.yaml (single source of truth).

Priority: env var > bridge.conf.yaml > _DEFAULTS
"""

import os
from pathlib import Path

import yaml


_SCRIPT_DIR = Path(__file__).resolve().parent.parent  # catpaw-bridge/
_CONFIG_PATH = os.environ.get(
    "BRIDGE_CONFIG",
    str(_SCRIPT_DIR / "bridge.conf.yaml"),
)

# Default config (used when bridge.conf.yaml is missing or incomplete)
_DEFAULTS = {
    "cliproxy": {"port": 8317, "api_key": "sk-catpaw-bridge-key"},
    "catpaw_proxy": {"port": 9000, "verbose": True, "strip_tool_definitions": False},
    "catpaw": {
        "api_host": "catpaw.meituan.com",
        "data_dir": "~/Library/Application Support/CatPawAI",
        "sso_client_id": "1d47d6ff96",
        "sso_client_id_2": "f32a546874",
        "tenant_id": "5282fa6645",
        "need_passport_id": True,
    },
    "model": {"name": "glm-5.2", "type_code": 2},
    "limits": {"max_encrypted_body": 180_000, "max_message_content": 15_000, "max_system_content": 8_000},
}


def _parse_bool(env_val, config_val) -> bool:
    """Parse boolean from env var (string) or config (bool/string).

    Priority: env_val (if not None) > config_val > False
    """
    if env_val is not None:
        return env_val.lower() in ("1", "true", "yes", "on")
    if isinstance(config_val, bool):
        return config_val
    if isinstance(config_val, str):
        return config_val.lower() in ("1", "true", "yes", "on")
    return False


def _load_config() -> dict:
    """Load config from bridge.conf.yaml, merge with defaults."""
    cfg = {}
    try:
        with open(_CONFIG_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
        print(f"[CatPawProxy] Config loaded from {_CONFIG_PATH}", flush=True)
    except FileNotFoundError:
        print(f"[CatPawProxy] WARNING: {_CONFIG_PATH} not found, using defaults", flush=True)
    except Exception as e:
        print(f"[CatPawProxy] WARNING: config load error: {e}, using defaults", flush=True)

    def deep_get(d, *keys, default=None):
        for k in keys:
            if isinstance(d, dict):
                d = d.get(k, {})
            else:
                return default
        return d if d != {} else default

    cp = deep_get(cfg, "catpaw_proxy", default={})
    catpaw = deep_get(cfg, "catpaw", default={})
    model = deep_get(cfg, "model", default={})
    limits = deep_get(cfg, "limits", default={})
    ccg = deep_get(cfg, "ccg", default={})

    return {
        "listen_host": os.environ.get("CATPAW_PROXY_HOST", "127.0.0.1"),
        "listen_port": int(os.environ.get("CATPAW_PROXY_PORT", cp.get("port", 9000))),
        "strip_tool_definitions": _parse_bool(
            os.environ.get("STRIP_TOOL_DEFINITIONS"),
            cp.get("strip_tool_definitions", False),
        ),
        "api_host": os.environ.get("CATPAW_API_HOST", catpaw.get("api_host", "catpaw.meituan.com")),
        "data_dir": os.path.expanduser(
            os.environ.get("CATPAW_DATA_DIR", catpaw.get("data_dir", _DEFAULTS["catpaw"]["data_dir"]))
        ),
        "sso_client_id": os.environ.get("SSO_CLIENT_ID", catpaw.get("sso_client_id", "1d47d6ff96")),
        "sso_client_id_2": os.environ.get("SSO_CLIENT_ID_2", catpaw.get("sso_client_id_2", "f32a546874")),
        "tenant_id": os.environ.get("TENANT_ID", catpaw.get("tenant_id", "5282fa6645")),
        "need_passport_id": _parse_bool(os.environ.get("NEED_PASSPORT_ID"), catpaw.get("need_passport_id", True)),
        "verbose": _parse_bool(os.environ.get("CATPAW_PROXY_VERBOSE"), cp.get("verbose", True)),
        "model_name": model.get("name", "glm-5.2"),
        "model_type_code": model.get("type_code", 2),
        "max_encrypted_body": int(limits.get("max_encrypted_body", 180_000)),
        "max_message_content": int(limits.get("max_message_content", 50_000)),
        "max_system_content": int(limits.get("max_system_content", 8_000)),
        "ccg_enabled": _parse_bool(os.environ.get("CCG_ENABLED"), ccg.get("enabled", True)),
    }


_CFG = _load_config()

# ---- Module-level constants (imported by other modules) ----

LISTEN_HOST = _CFG["listen_host"]
LISTEN_PORT = _CFG["listen_port"]
CATPAW_API_HOST = _CFG["api_host"]
CATPAW_API_BASE = f"https://{CATPAW_API_HOST}"
CATPAW_DATA_DIR = _CFG["data_dir"]
SSO_CLIENT_ID = _CFG["sso_client_id"]
SSO_CLIENT_ID_2 = _CFG["sso_client_id_2"]
TENANT_ID = _CFG["tenant_id"]
NEED_PASSPORT_ID = _CFG["need_passport_id"]
VERBOSE = _CFG["verbose"]
MODEL_NAME = _CFG["model_name"]
MODEL_TYPE_CODE = _CFG["model_type_code"]
MAX_ENCRYPTED_BODY = _CFG["max_encrypted_body"]
MAX_MESSAGE_CONTENT = _CFG["max_message_content"]
MAX_SYSTEM_CONTENT = _CFG["max_system_content"]
STRIP_TOOL_DEFINITIONS = _CFG["strip_tool_definitions"]
CCG_ENABLED = _CFG["ccg_enabled"]
