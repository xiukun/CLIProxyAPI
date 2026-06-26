#!/usr/bin/env python3
"""
CatPawAI Reverse Proxy (with encryption support)

将 CatPawAI IDE 的 glm-5.2 模型暴露为标准 OpenAI 兼容 API，
供 CLIProxyAPI 调用，最终让 Claude Code 使用。

工作流程:
    Claude Code -> CLIProxyAPI (:8317)
        -> 本代理 (:9000, 注入 SSO 认证 + 加密)
        -> CatPawAI API (catpaw.meituan.com/api/gpt/openai/stream)
        -> glm-5.2

认证信息自动从 CatPawAI 的 state.vscdb 中读取。
请求体使用 AES-128-ECB + RSA-OAEP-SHA1 加密。
"""

import asyncio
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web
import yaml

# Crypto
from Crypto.Cipher import AES
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA1
from Crypto.Util.Padding import pad, unpad
import base64
import secrets

# ---------------------------------------------------------------------------
# Configuration: load from bridge.conf.yaml (single source of truth)
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).parent
_CONFIG_PATH = os.environ.get(
    "BRIDGE_CONFIG",
    str(_SCRIPT_DIR / "bridge.conf.yaml"),
)

# Default config (used when bridge.conf.yaml is missing or incomplete)
_DEFAULTS = {
    "cliproxy": {"port": 8317, "api_key": "sk-catpaw-bridge-key"},
    "catpaw_proxy": {"port": 9000, "verbose": True},
    "catpaw": {
        "api_host": "catpaw.meituan.com",
        "data_dir": "~/Library/Application Support/CatPawAI",
        "sso_client_id": "1d47d6ff96",
        "sso_client_id_2": "f32a546874",
        "tenant_id": "5282fa6645",
        "need_passport_id": True,
    },
    "model": {"name": "glm-5.2", "type_code": 2},
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
    """Load config from bridge.conf.yaml, merge with defaults.
    
    Priority: env var > bridge.conf.yaml > _DEFAULTS
    """
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

    return {
        "listen_host": os.environ.get("CATPAW_PROXY_HOST", "127.0.0.1"),
        "listen_port": int(os.environ.get("CATPAW_PROXY_PORT", cp.get("port", 9000))),
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
    }


_CFG = _load_config()

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

# Auth cache
_auth_cache = {"access_token": None, "mis_id": None, "user_info_id": None, "ts": 0}
_AUTH_CACHE_TTL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# RSA Keys (extracted from extension.js via XOR decryption)
# ---------------------------------------------------------------------------

def _extract_rsa_keys():
    """Extract RSA public and private keys from CatPawAI extension.js.

    The keys are XOR-encrypted with key "ThisIsMyXorKey" and then base64-encoded.
    """
    xor_key = "ThisIsMyXorKey"

    def xor_decipher(encoded_str):
        decoded = base64.b64decode(encoded_str)
        result = bytearray()
        for i, b in enumerate(decoded):
            result.append(b ^ ord(xor_key[i % len(xor_key)]))
        return result.decode("utf-8")

    try:
        ext_path = "/Applications/CatPawAI.app/Contents/Resources/app/extensions/mt-idekit.mt-idekit-code/out/extension.js"
        with open(ext_path, "r") as f:
            data = f.read()

        import re

        # Extract key1 (public key)
        m1 = re.search(r'this\.key1=this\.xorDecipher\("([^"]+)"', data)
        m2 = re.search(r'this\.key2=this\.xorDecipher\("([^"]+)"', data)

        if not m1 or not m2:
            raise RuntimeError("Could not find RSA keys in extension.js")

        key1_pem = xor_decipher(m1.group(1))
        key2_pem = xor_decipher(m2.group(1))

        # Clean up PEM (remove extra whitespace, fix line breaks)
        def clean_pem(pem):
            # The XOR decryption may produce the PEM with embedded newlines
            # that are actually part of the base64 content. We need to:
            # 1. Find the header and footer
            # 2. Extract the base64 content between them
            # 3. Clean and reformat

            # Try to find header/footer patterns
            import re as _re

            # Match header like "-----BEGIN PUBLIC KEY-----" or "-----BEGINPRIVATEKEY-----"
            pub_match = _re.search(r'-----BEGIN[\s]*PUBLIC[\s]*KEY-----', pem)
            priv_match = _re.search(r'-----BEGIN[\s]*PRIVATE[\s]*KEY-----', pem)

            if pub_match:
                header = "-----BEGIN PUBLIC KEY-----"
                footer = "-----END PUBLIC KEY-----"
                start = pub_match.end()
                end_match = _re.search(r'-----END[\s]*PUBLIC[\s]*KEY-----', pem[start:])
                if end_match:
                    end = start + end_match.start()
                else:
                    end = len(pem)
            elif priv_match:
                header = "-----BEGIN PRIVATE KEY-----"
                footer = "-----END PRIVATE KEY-----"
                start = priv_match.end()
                end_match = _re.search(r'-----END[\s]*PRIVATE[\s]*KEY-----', pem[start:])
                if end_match:
                    end = start + end_match.start()
                else:
                    end = len(pem)
            else:
                # No PEM headers found, try to fix manually
                pem = pem.replace("\n", "").replace("\r", "").replace(" ", "")
                if "BEGINPUBLICKEY" in pem:
                    pem = pem.replace("BEGINPUBLICKEY", "BEGIN PUBLIC KEY")
                    pem = pem.replace("ENDPUBLICKEY", "END PUBLIC KEY")
                elif "BEGINPRIVATEKEY" in pem:
                    pem = pem.replace("BEGINPRIVATEKEY", "BEGIN PRIVATE KEY")
                    pem = pem.replace("ENDPRIVATEKEY", "END PRIVATE KEY")
                # Split into 64-char lines
                lines = []
                for i in range(0, len(pem), 64):
                    lines.append(pem[i:i + 64])
                return "\n".join(lines)

            # Extract base64 content and clean it
            b64_content = pem[start:end]
            b64_content = b64_content.replace("\n", "").replace("\r", "").replace(" ", "")

            # Build proper PEM
            lines = [header]
            for i in range(0, len(b64_content), 64):
                lines.append(b64_content[i:i + 64])
            lines.append(footer)
            return "\n".join(lines)

        key1_pem = clean_pem(key1_pem)
        key2_pem = clean_pem(key2_pem)

        # Verify keys can be imported
        try:
            RSA.importKey(key1_pem)
        except Exception as e:
            print(f"[CatPawProxy] WARNING: key1 import failed: {e}", flush=True, file=sys.stderr)
        try:
            RSA.importKey(key2_pem)
        except Exception as e:
            print(f"[CatPawProxy] WARNING: key2 import failed: {e}", flush=True, file=sys.stderr)

        if VERBOSE:
            print(f"[CatPawProxy] RSA keys extracted successfully", flush=True)
            print(f"[CatPawProxy] key1 (public) length: {len(key1_pem)}", flush=True)
            print(f"[CatPawProxy] key2 (private) length: {len(key2_pem)}", flush=True)

        return key1_pem, key2_pem
    except Exception as e:
        print(f"[CatPawProxy] WARNING: Could not extract RSA keys: {e}", flush=True, file=sys.stderr)
        print("[CatPawProxy] Encryption will be disabled. API calls may fail.", flush=True, file=sys.stderr)
        return None, None


RSA_PUBLIC_KEY_PEM, RSA_PRIVATE_KEY_PEM = _extract_rsa_keys()


# ---------------------------------------------------------------------------
# Encryption / Decryption
# ---------------------------------------------------------------------------

def encrypt_request(body_str: str, headers: dict) -> str:
    """Encrypt request body using AES-128-ECB + RSA-OAEP-SHA1.

    1. Generate random 16-byte AES key
    2. Encrypt body with AES-128-ECB -> base64
    3. AES key -> base64 -> RSA-OAEP-SHA1 encrypt -> base64
    4. Set 'encrypted-key' header
    5. Return encrypted body (base64 string)
    """
    if not RSA_PUBLIC_KEY_PEM:
        return body_str

    try:
        # Generate random AES-128 key (16 bytes)
        aes_key = secrets.token_bytes(16)

        # Encrypt body with AES-128-ECB
        cipher = AES.new(aes_key, AES.MODE_ECB)
        body_bytes = body_str.encode("utf-8")
        encrypted_body = cipher.encrypt(pad(body_bytes, AES.block_size))
        encrypted_body_b64 = base64.b64encode(encrypted_body).decode("utf-8")

        # Encrypt AES key with RSA-OAEP-SHA1
        rsa_key = RSA.importKey(RSA_PUBLIC_KEY_PEM)
        cipher_rsa = PKCS1_OAEP.new(rsa_key, hashAlgo=SHA1)
        # The AES key is first converted to base64, then encrypted
        aes_key_b64 = base64.b64encode(aes_key).decode("utf-8")
        encrypted_aes_key = cipher_rsa.encrypt(aes_key_b64.encode("utf-8"))
        encrypted_aes_key_b64 = base64.b64encode(encrypted_aes_key).decode("utf-8")

        # Set header
        headers["encrypted-key"] = encrypted_aes_key_b64

        return encrypted_body_b64
    except Exception as e:
        print(f"[CatPawProxy] Encryption failed, sending plaintext: {e}", flush=True, file=sys.stderr)
        return body_str


def decrypt_response_data(encrypted_data: str, encrypted_key: str) -> str:
    """Decrypt response data using AES-128-ECB + RSA-OAEP-SHA1.

    1. RSA-OAEP-SHA1 decrypt the encrypted_key -> base64 string -> AES key
    2. AES-128-ECB decrypt the data
    """
    if not RSA_PRIVATE_KEY_PEM or not encrypted_key:
        return encrypted_data

    try:
        # Decrypt AES key with RSA-OAEP-SHA1
        rsa_key = RSA.importKey(RSA_PRIVATE_KEY_PEM)
        cipher_rsa = PKCS1_OAEP.new(rsa_key, hashAlgo=SHA1)
        encrypted_key_bytes = base64.b64decode(encrypted_key)
        decrypted_aes_key_b64 = cipher_rsa.decrypt(encrypted_key_bytes)
        # The decrypted value is base64-encoded AES key
        aes_key = base64.b64decode(decrypted_aes_key_b64)

        # Decrypt data with AES-128-ECB
        cipher = AES.new(aes_key, AES.MODE_ECB)
        encrypted_data_bytes = base64.b64decode(encrypted_data)
        decrypted_data = unpad(cipher.decrypt(encrypted_data_bytes), AES.block_size)

        return decrypted_data.decode("utf-8")
    except Exception as e:
        print(f"[CatPawProxy] Decryption failed: {e}", flush=True, file=sys.stderr)
        return encrypted_data


# ---------------------------------------------------------------------------
# Auth: read SSO token from CatPawAI state.vscdb
# ---------------------------------------------------------------------------

def get_catpaw_auth():
    """Read SSO accessToken and user info from CatPawAI's state.vscdb."""
    now = time.time()
    if _auth_cache["access_token"] and (now - _auth_cache["ts"]) < _AUTH_CACHE_TTL:
        return _auth_cache

    vscdb_path = os.path.join(CATPAW_DATA_DIR, "User", "globalStorage", "state.vscdb")
    if not os.path.exists(vscdb_path):
        raise RuntimeError(f"CatPawAI state.vscdb not found: {vscdb_path}")

    conn = sqlite3.connect(vscdb_path)
    try:
        cursor = conn.execute(
            "SELECT value FROM ItemTable WHERE key = 'catpaw.mt-authentication'"
        )
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("catpaw.mt-authentication not found in state.vscdb")

        auth_data = json.loads(row[0])
        mt_auth = json.loads(auth_data["mt.auth"])

        sessions = mt_auth.get("sessions", [])
        if not sessions:
            raise RuntimeError("No SSO sessions found")

        session = sessions[0]
        access_token = session["accessToken"]
        account = session.get("account", {})

        mis_id = account.get("label") or account.get("id", "")
        user_info_id = account.get("userInfoId", "")

        _auth_cache["access_token"] = access_token
        _auth_cache["mis_id"] = mis_id
        _auth_cache["user_info_id"] = user_info_id
        _auth_cache["ts"] = now

        if VERBOSE:
            print(f"[CatPawProxy] Auth loaded: mis_id={mis_id}, user_info_id={user_info_id}", flush=True)

        return _auth_cache
    finally:
        conn.close()


def build_catpaw_headers(auth, content_type="application/json"):
    """Build the headers CatPawAI API expects.

    Cookie format (from extension.js function Gf):
        Cookie: <client_id1>_passportid=<token>; <client_id2>_ssoid=<token>
        (when needPassportId=true for tenant 5282fa6645)

    Also sets Catpaw-Auth header to the raw token.
    """
    access_token = auth["access_token"]
    mis_id = auth["mis_id"]

    # Build Cookie: two entries with the same token
    if NEED_PASSPORT_ID:
        cookie1 = f"{SSO_CLIENT_ID}_passportid={access_token}"
    else:
        cookie1 = f"{SSO_CLIENT_ID}_ssoid={access_token}"
    cookie2 = f"{SSO_CLIENT_ID_2}_ssoid={access_token}"
    cookie_str = f"{cookie1}; {cookie2}"

    headers = {
        "Content-Type": content_type,
        "ide-type": "CatPaw IDE",
        "client-type": "CatPaw IDE",
        "ide-version": "2026.2.2",
        "plugin-id": "mt-idekit.mt-idekit-code",
        "plugin-version": "2026.2.3",
        "client-env": "production",
        "user-mis-id": mis_id,
        "user-uid": mis_id,
        "mis-id": mis_id,
        "tenant": TENANT_ID,
        "platform-info": "darwin-arm64",
        "Cookie": cookie_str,
        "Catpaw-Auth": access_token,
    }

    return headers


# ---------------------------------------------------------------------------
# Session Manager: conversation tracking for context caching
# ---------------------------------------------------------------------------

import hashlib
import time

# Map: conversation_hash -> {conversationId, message_count, timestamp}
# This allows the server to cache context for ongoing conversations
_session_store = {}
_MAX_SESSIONS = 200  # limit memory usage
_SESSION_TTL = 1800  # 30 minutes TTL


def _hash_user_messages(messages: list, exclude_last: bool = True) -> str:
    """Hash only USER messages to identify conversation threads.
    
    Key insight: Claude Code alternates user/assistant messages.
    Between turns, both a new user msg and the previous assistant reply
    are added. By hashing ONLY user messages (which are stable across
    assistant replies), we can reliably detect continuations.
    
    exclude_last=True: hash user messages excluding the last user msg
        (used for lookup: "have I seen this conversation prefix before?")
    exclude_last=False: hash ALL user messages including the last one
        (used for storage: "remember this conversation state for next time")
    """
    user_msgs = []
    for msg in messages:
        if msg.get("role") == "user":
            content = _extract_text_content(msg.get("content", ""))
            user_msgs.append(content[:300])  # first 300 chars for efficiency
    
    if exclude_last and user_msgs:
        user_msgs = user_msgs[:-1]  # exclude last user message
    
    return hashlib.md5("\n".join(user_msgs).encode()).hexdigest()


def get_or_create_conversation_id(messages: list) -> tuple:
    """Get or create a conversationId for the given message sequence.
    
    Uses a two-phase hashing strategy:
    1. LOOKUP: Hash user messages excluding the last one (the new question).
       If this hash exists in the store, it's a continuation.
    2. STORAGE: After determining the conversationId, store the hash of
       ALL user messages (including the new question) for future lookups.
    
    Returns: (conversationId, is_new_conversation)
    """
    now = time.time()
    
    # Cleanup expired sessions
    expired = [k for k, v in _session_store.items() if now - v["ts"] > _SESSION_TTL]
    for k in expired:
        del _session_store[k]
    
    # Phase 1: LOOKUP - hash user messages excluding the last one
    lookup_hash = _hash_user_messages(messages, exclude_last=True)
    
    if lookup_hash in _session_store:
        # Cache hit: continuation of existing conversation
        session = _session_store[lookup_hash]
        conv_id = session["conversationId"]
        
        # Phase 2: STORAGE - store hash of ALL user messages for future lookups
        storage_hash = _hash_user_messages(messages, exclude_last=False)
        _session_store[storage_hash] = {
            "conversationId": conv_id,
            "count": len(messages),
            "ts": now,
        }
        
        if VERBOSE:
            print(f"[CatPawProxy] Cache HIT: conv_id={conv_id[:8]}..., msgs={len(messages)}", flush=True)
        return conv_id, False
    
    # Cache miss: new conversation
    conv_id = str(uuid.uuid4())
    
    # Phase 2: STORAGE - store hash of ALL user messages for future lookups
    storage_hash = _hash_user_messages(messages, exclude_last=False)
    _session_store[storage_hash] = {
        "conversationId": conv_id,
        "count": len(messages),
        "ts": now,
    }
    
    # Cleanup old sessions if too many
    if len(_session_store) > _MAX_SESSIONS:
        # Sort by timestamp and remove oldest half
        sorted_keys = sorted(_session_store.keys(), key=lambda k: _session_store[k]["ts"])
        for k in sorted_keys[:_MAX_SESSIONS // 2]:
            del _session_store[k]
    
    if VERBOSE:
        print(f"[CatPawProxy] New session: conv_id={conv_id[:8]}..., msgs={len(messages)}", flush=True)
    return conv_id, True


# ---------------------------------------------------------------------------
# Request/Response format conversion
# ---------------------------------------------------------------------------

def _extract_text_content(content) -> str:
    """Extract text from OpenAI message content (string or multimodal array)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(part.get("text", ""))
        return "\n".join(text_parts)
    return str(content) if content else ""


# ---------------------------------------------------------------------------
# Tool Call Support: inject tool definitions, parse model output for tool calls
# ---------------------------------------------------------------------------

def _inject_tools_prompt(tools: list) -> str:
    """Convert OpenAI tools array to a text system prompt.
    
    Since CatPawAI API doesn't support function calling natively,
    we inject tool definitions as text and instruct the model to
    output tool calls in a parseable format.
    """
    if not tools:
        return ""
    
    lines = [
        "You have access to the following tools. Use them when needed to accomplish tasks.",
        "",
    ]
    
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        func = tool.get("function", tool)
        name = func.get("name", "")
        desc = func.get("description", "")
        params = func.get("parameters", {})
        
        lines.append(f"### {name}")
        lines.append(f"{desc}")
        if params and isinstance(params, dict):
            props = params.get("properties", {})
            required = params.get("required", [])
            if props:
                lines.append("Parameters:")
                for pname, pinfo in props.items():
                    ptype = pinfo.get("type", "string")
                    pdesc = pinfo.get("description", "")
                    req = " (required)" if pname in required else ""
                    lines.append(f"  - {pname} ({ptype}){req}: {pdesc}")
        lines.append("")
    
    lines.extend([
        "## How to call tools",
        "When you want to use a tool, you MUST output the tool call in this EXACT format:",
        "",
        "<tool_call>",
        '{"name": "ToolName", "arguments": {"param1": "value1", "param2": "value2"}}',
        "</tool_call>",
        "",
        "IMPORTANT RULES:",
        "1. You can call multiple tools by outputting multiple <tool_call> blocks.",
        "2. After each tool call, you will receive the result in the next message as 'Tool Result: ...'",
        "3. You can output brief text before tool calls to explain your intent.",
        "4. Always use the exact tool names and parameter names as defined above.",
        "5. When you don't need any tools, just respond normally without <tool_call> blocks.",
        "6. DO NOT describe tool calls in natural language - use the <tool_call> format.",
        "7. DO NOT output tool call syntax inside markdown code blocks - use raw <tool_call> tags.",
        "",
    ])
    
    return "\n".join(lines)


def _convert_messages_with_tools(messages: list) -> str:
    """Convert OpenAI messages (including tool_calls and tool role) to text.
    
    Handles:
    - system role: pass through as context
    - user role: prefix with 'Human:'
    - assistant role with tool_calls: convert to text showing what tools were called
    - tool role: convert to 'Tool Result:' text
    """
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        
        if role == "system":
            content = _extract_text_content(msg.get("content", ""))
            if content:
                parts.append(content)
            continue
        
        if role == "tool":
            # Tool result message
            content = _extract_text_content(msg.get("content", ""))
            tool_call_id = msg.get("tool_call_id", "")
            parts.append(f"Tool Result ({tool_call_id}): {content}")
            continue
        
        if role == "assistant":
            content = _extract_text_content(msg.get("content", ""))
            tool_calls = msg.get("tool_calls", [])
            
            if content:
                parts.append(f"Assistant: {content}")
            
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    tc_name = func.get("name", "")
                    tc_args = func.get("arguments", "{}")
                    tc_id = tc.get("id", "")
                    parts.append(f'Assistant called tool: <tool_call>\n{{"name": "{tc_name}", "arguments": {tc_args}}}\n</tool_call> (id: {tc_id})')
            continue
        
        # Default: user or other roles
        content = _extract_text_content(msg.get("content", ""))
        if content:
            parts.append(f"Human: {content}")
    
    return "\n\n".join(parts)


# Regex patterns for parsing tool calls from model output
# Format 3: <function_calls><invoke name="..."><parameter>...</parameter></invoke></function_calls>
_RE_FUNCTION_CALLS = re.compile(r'<function_calls>\s*(.*?)\s*</function_calls>', re.DOTALL)
_RE_INVOKE = re.compile(r'<invoke\s+name="([^"]+)">(.*?)</invoke>', re.DOTALL)
_RE_PARAMETER = re.compile(r'<parameter\s+name="([^"]+)">(.*?)</parameter>', re.DOTALL)


def _find_balanced_json(text: str, start: int) -> tuple:
    """Find a balanced JSON object starting at position 'start' in text.
    
    Assumes text[start] == '{'. Returns (json_str, end_pos) where end_pos
    is the index AFTER the closing '}'. Returns (None, start) if no valid
    JSON object is found.
    
    Handles:
    - Nested objects and arrays
    - String literals (with escape sequences)
    - Whitespace
    """
    if start >= len(text) or text[start] != '{':
        return None, start
    
    depth = 0
    in_string = False
    escape = False
    i = start
    
    while i < len(text):
        ch = text[i]
        
        if escape:
            escape = False
            i += 1
            continue
        
        if ch == '\\' and in_string:
            escape = True
            i += 1
            continue
        
        if ch == '"':
            in_string = not in_string
            i += 1
            continue
        
        if in_string:
            i += 1
            continue
        
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1
        
        i += 1
    
    return None, start


def _extract_tool_call(json_str: str) -> dict | None:
    """Extract a single tool call from JSON string."""
    try:
        data = json.loads(json_str)
        tc_name = data.get("name", "")
        if not tc_name:
            return None
        tc_args = data.get("arguments", {})
        if isinstance(tc_args, dict):
            tc_args = json.dumps(tc_args, ensure_ascii=False)
        elif not isinstance(tc_args, str):
            tc_args = json.dumps(tc_args, ensure_ascii=False)
        return {
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": tc_name,
                "arguments": tc_args,
            }
        }
    except (json.JSONDecodeError, AttributeError) as e:
        if VERBOSE:
            print(f"[CatPawProxy] Tool call parse error: {e}", flush=True)
        return None


def _find_tag_tool_calls(content: str, tag_name: str) -> list:
    """Find all tool calls wrapped in <tag_name>...</tag_name> (or similar).
    
    Uses balanced brace matching to handle nested JSON objects.
    Handles cases where the closing tag is missing or different (e.g. </think>).
    
    Returns list of (start_pos, end_pos, tool_call_dict) tuples.
    start_pos: index of '<' in opening tag
    end_pos: index AFTER the closing tag (or after the JSON if no closing tag)
    """
    results = []
    open_tag = f"<{tag_name}>"
    search_pos = 0
    
    while True:
        tag_pos = content.find(open_tag, search_pos)
        if tag_pos == -1:
            break
        
        # Find the first '{' after the opening tag
        json_start = content.find('{', tag_pos + len(open_tag))
        if json_start == -1:
            search_pos = tag_pos + len(open_tag)
            continue
        
        # Extract balanced JSON
        json_str, json_end = _find_balanced_json(content, json_start)
        if json_str is None:
            search_pos = json_start + 1
            continue
        
        # Try to parse as tool call
        tc = _extract_tool_call(json_str)
        if tc:
            # Look for a closing tag after the JSON
            # Accept </tool_call>, </tool_use>, </think>, or just end of JSON
            remaining = content[json_end:json_end + 50]
            end_pos = json_end
            for close_tag in [f"</{tag_name}>", "</think>", "</tool_call>", "</tool_use>"]:
                ct_pos = remaining.find(close_tag)
                if ct_pos == 0 or (ct_pos != -1 and ct_pos < 10):
                    end_pos = json_end + ct_pos + len(close_tag)
                    break
            
            results.append((tag_pos, end_pos, tc))
            search_pos = end_pos
        else:
            search_pos = json_end
    
    return results


def _parse_tool_calls(content: str) -> tuple:
    """Parse model output for tool calls.
    
    Returns: (text_without_tool_calls, list_of_tool_call_dicts)
    Each tool_call_dict has: {id, type, function: {name, arguments}}
    
    Supports multiple formats (checked in priority order):
    1. <tool_call>{"name": "...", "arguments": {...}}</tool_call> (primary)
       - Also handles </think> as closing tag (model's actual behavior)
       - Also handles missing closing tag
    2. <tool_use>{"name": "...", "arguments": {...}}</tool_use> (legacy)
    3. <function_calls><invoke name="..."><parameter>...</parameter></invoke></function_calls>
    4. Markdown JSON code blocks containing tool call objects
    """
    if not content:
        return content, []
    
    # Format 1 & 2: <tool_call> and <tool_use> tags with balanced JSON extraction
    found = _find_tag_tool_calls(content, "tool_call")
    if not found:
        found = _find_tag_tool_calls(content, "tool_use")
    
    if found:
        tool_calls = []
        text_parts = []
        last_end = 0
        
        for start_pos, end_pos, tc in found:
            text_parts.append(content[last_end:start_pos])
            tool_calls.append(tc)
            last_end = end_pos
        
        text_parts.append(content[last_end:])
        clean_text = "".join(text_parts).strip()
        return clean_text, tool_calls
    
    # Format 3: <function_calls><invoke name="..."><parameter>...</parameter></invoke></function_calls>
    fc_match = _RE_FUNCTION_CALLS.search(content)
    if fc_match:
        tool_calls = []
        text_parts = [content[:fc_match.start()]]
        invoke_text = fc_match.group(1)
        
        for invoke_match in _RE_INVOKE.finditer(invoke_text):
            tool_name = invoke_match.group(1)
            params_text = invoke_match.group(2)
            
            args = {}
            for param_match in _RE_PARAMETER.finditer(params_text):
                pname = param_match.group(1)
                pval = param_match.group(2).strip()
                args[pname] = pval
            
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                }
            })
        
        if tool_calls:
            text_parts.append(content[fc_match.end():])
            clean_text = "".join(text_parts).strip()
            return clean_text, tool_calls
    
    # Format 4: Markdown JSON code blocks with tool call objects
    # Look for ```json {...} ``` patterns and try to parse as tool calls
    md_pattern = re.compile(r'```(?:json)?\s*(\{.*?\})\s*```', re.DOTALL)
    tool_calls = []
    text_parts = []
    last_end = 0
    
    for match in md_pattern.finditer(content):
        json_str = match.group(1)
        # Only treat as tool call if it has "name" and "arguments"
        if '"name"' in json_str and '"arguments"' in json_str:
            tc = _extract_tool_call(json_str)
            if tc:
                text_parts.append(content[last_end:match.start()])
                tool_calls.append(tc)
                last_end = match.end()
    
    if tool_calls:
        text_parts.append(content[last_end:])
        clean_text = "".join(text_parts).strip()
        return clean_text, tool_calls
    
    # No tool calls found
    return content, []


def openai_to_catpaw_request(openai_body: dict) -> dict:
    """Convert OpenAI chat completion request to CatPawAI agent-mode format.

    Agent mode features:
    - planPromptEnabled: false (avoids agent XML injection)
    - chatApplyModeType: "chat"
    - pluginList: [] (no MCP plugins)
    - triggerMode: "TOOLWINDOW_CHAT" (chat panel mode)
    - conversationId: stable per-conversation ID for server-side tracking
    
    Tool calling:
    CatPawAI API doesn't support function calling natively.
    When tools are present in the request, we:
    1. Inject tool definitions as a system prompt
    2. Convert tool-related messages (tool role, assistant tool_calls) to text
    3. Parse model output for tool calls in the response handler
    
    Context handling:
    CatPawAI server does NOT cache context by conversationId.
    The full conversation history must be sent each time as a single
    merged user message (CatPawAI API only accepts one user message).
    """
    messages = openai_body.get("messages", [])
    tools = openai_body.get("tools", [])
    
    # Get or create conversation session (for tracking, not server-side caching)
    conversation_id, is_new = get_or_create_conversation_id(messages)
    
    # Build the merged content
    if len(messages) == 0:
        merged_content = ""
    elif len(messages) == 1 and not tools:
        merged_content = _extract_text_content(messages[0].get("content", ""))
    else:
        # When tools are present, inject tool definitions and use tool-aware merging
        if tools:
            tools_prompt = _inject_tools_prompt(tools)
            merged_content = tools_prompt + "\n" + _convert_messages_with_tools(messages)
        else:
            # No tools: use simple text merging
            parts = []
            for msg in messages:
                role = msg.get("role", "user")
                content = _extract_text_content(msg.get("content", ""))
                if not content:
                    continue
                if role == "system":
                    parts.append(content)
                elif role == "user":
                    parts.append(f"Human: {content}")
                elif role == "assistant":
                    parts.append(f"Assistant: {content}")
                else:
                    parts.append(f"{role}: {content}")
            merged_content = "\n\n".join(parts)
        
        # Append prompt for assistant if last message is from user
        if messages and messages[-1].get("role") == "user":
            merged_content += "\n\nAssistant:"
    
    catpaw_msg = {
        "role": "user",
        "content": merged_content,
        "triggerMode": "TOOLWINDOW_CHAT",
        "chatSelectContextTagList": [],
        "attachedCodeChunks": [],
        "attachedDocChunks": [],
        "attachedWebPages": [],
        "extraContextList": [],
    }

    # Model type code from unified config
    user_model_type_code = MODEL_TYPE_CODE

    # Build agent-mode request with full field set
    # (matches CatPawAI's buildGptChatRequest output)
    return {
        "selectedCode": "",
        "messages": [catpaw_msg],
        "language": "",
        "filePath": "",
        "conversationId": conversation_id,
        "triggerMode": "TOOLWINDOW_CHAT",
        "gitUrl": "",
        "remoteBranch": "",
        "pluginList": [],
        "promptTemplateWithContext": None,
        "call": None,
        "chatSelectContextTagList": [],
        "userModelTypeCode": user_model_type_code,
        "extra": {},
        "planPromptEnabled": False,      # Disable planning prompt - it injects
                                          # agent XML (<function_calls>) that
                                          # breaks Claude Code's own tool calling
        "chatApplyModeType": "chat",     # Chat mode (not edit)
        # Keep legacy fields for backward compat
        "parentSuggestUuid": "",
        "before": "",
        "after": "",
    }


def _strip_agent_xml(content: str) -> str:
    """Strip agent-mode XML artifacts from model output.
    
    When planPromptEnabled was true (or server injects agent prompts),
    the model may output XML-like blocks:
      <function_calls>...</function_calls>
      <invoke name="...">...</invoke>
      <parameter name="...">...</parameter>
      <antThinking>...</antThinking>
    
    These break Claude Code. Strip them out.
    """
    if not content:
        return content
    
    # Remove common agent XML tags and their content
    agent_patterns = [
        r'<function_calls>.*?</function_calls>',
        r'<invoke\s+name="[^"]*">.*?</invoke>',
        r'<parameter\s+name="[^"]*">.*?</parameter>',
        r'<antThinking>.*?</antThinking>',
        r'<plan>.*?</plan>',
    ]
    for pattern in agent_patterns:
        content = re.sub(pattern, '', content, flags=re.DOTALL)
    
    # Also remove orphaned opening/closing tags
    orphan_tags = [
        '<function_calls>', '</function_calls>',
        '<invoke>', '</invoke>',
        '<parameter>', '</parameter>',
        '<antThinking>', '</antThinking>',
        '<plan>', '</plan>',
    ]
    for tag in orphan_tags:
        content = content.replace(tag, '')
    
    return content


def catpaw_sse_to_openai_sse(catpaw_data: dict, model: str, is_last: bool = False) -> dict:
    """Convert a single CatPawAI SSE data object to OpenAI streaming format.

    CatPawAI fields (camelCase):
        - choices[0].finishReason
        - choices[0].delta.content (or content)
        - object: "chat.completion"
        - lastOne: bool
        - suggestUuid: str

    OpenAI fields (snake_case):
        - choices[0].finish_reason
        - choices[0].delta.content
        - object: "chat.completion.chunk"
    """
    choices = catpaw_data.get("choices", [])
    openai_choices = []

    for choice in choices:
        delta = choice.get("delta", {})
        content = delta.get("content", "")

        # Also check direct content field
        if not content and "content" in choice:
            content = choice.get("content", "")

        # Skip [DONE] content (it's an end marker, not actual content)
        if content == "[DONE]":
            content = ""

        # Strip agent-mode XML artifacts
        content = _strip_agent_xml(content)

        finish_reason = choice.get("finishReason", None)
        if is_last and not finish_reason:
            finish_reason = "stop"

        # Fix index type: CatPawAI returns string "0", OpenAI expects int
        raw_index = choice.get("index", 0)
        try:
            idx = int(raw_index)
        except (ValueError, TypeError):
            idx = 0

        if is_last:
            openai_choice = {
                "index": idx,
                "delta": {},
                "finish_reason": finish_reason or "stop",
            }
        else:
            openai_choice = {
                "index": idx,
                "delta": {"content": content} if content else {},
                "finish_reason": finish_reason,
            }
        openai_choices.append(openai_choice)

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": openai_choices,
    }


def catpaw_to_openai_response(catpaw_data: dict, model: str) -> dict:
    """Convert CatPawAI non-streaming response to OpenAI format."""
    choices = catpaw_data.get("choices", [])
    openai_choices = []

    for choice in choices:
        message = choice.get("message", choice.get("delta", {}))
        content = message.get("content", choice.get("content", ""))

        # Strip agent-mode XML artifacts
        content = _strip_agent_xml(content)

        openai_choice = {
            "index": choice.get("index", 0),
            "message": {"role": "assistant", "content": content},
            "finish_reason": choice.get("finishReason", "stop"),
        }
        openai_choices.append(openai_choice)

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": openai_choices,
        "usage": catpaw_data.get("usage", {}),
    }


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    """Handle /v1/chat/completions - main chat endpoint."""
    try:
        auth = get_catpaw_auth()
    except Exception as e:
        return web.json_response(
            {"error": {"message": f"Auth failed: {e}", "type": "auth_error"}},
            status=503,
        )

    # Read OpenAI request body
    body = await request.read()
    try:
        openai_body = json.loads(body)
    except json.JSONDecodeError:
        return web.json_response(
            {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
            status=400,
        )

    model = openai_body.get("model", MODEL_NAME)
    is_stream = openai_body.get("stream", False)
    has_tools = bool(openai_body.get("tools"))

    # Convert to CatPawAI format
    catpaw_request = openai_to_catpaw_request(openai_body)
    catpaw_body_str = json.dumps(catpaw_request, ensure_ascii=False)

    if VERBOSE:
        print(f"[CatPawProxy] Model: {model}, Stream: {is_stream}, Tools: {has_tools}", flush=True)
        print(f"[CatPawProxy] CatPawAI request: {catpaw_body_str[:200]}...", flush=True)
        if has_tools:
            print(f"[CatPawProxy] Tool-aware mode: response will be buffered for tool call parsing", flush=True)

    # Build headers with SSO auth
    headers = build_catpaw_headers(auth)

    # Encrypt request body
    encrypted_body = encrypt_request(catpaw_body_str, headers)

    # For encrypted requests, the body is a base64 string, not JSON
    # The original CatPawAI extension keeps Content-Type: application/json
    # but also adds streaming headers
    headers["Accept"] = "text/event-stream"
    headers["Cache-Control"] = "no-cache"
    headers["Connection"] = "keep-alive"

    if VERBOSE:
        # Log all headers being sent (redact token)
        safe_headers = {k: (v[:20] + "..." if k.lower() in ("cookie", "catpaw-auth", "encrypted-key") and len(v) > 20 else v) for k, v in headers.items()}
        print(f"[CatPawProxy] Headers: {json.dumps(safe_headers, ensure_ascii=False)}", flush=True)
        print(f"[CatPawProxy] Encrypted body preview: {encrypted_body[:80]}...", flush=True)

    # Strip headers that should not be forwarded
    for key in ["Authorization", "Host", "Content-Length"]:
        headers.pop(key, None)

    # Target URL
    target_url = f"{CATPAW_API_BASE}/api/gpt/openai/stream"

    if VERBOSE:
        print(f"[CatPawProxy] POST -> {target_url}", flush=True)
        print(f"[CatPawProxy] Encrypted: {len(encrypted_body)} bytes", flush=True)

    try:
        timeout = aiohttp.ClientTimeout(total=600, connect=30, sock_read=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url=target_url,
                headers=headers,
                data=encrypted_body,
                ssl=False,
            ) as upstream_resp:

                if upstream_resp.status != 200:
                    error_body = await upstream_resp.read()
                    error_msg = error_body.decode("utf-8", errors="replace")[:500]
                    resp_headers = dict(upstream_resp.headers)
                    print(f"[CatPawProxy] Upstream error {upstream_resp.status}: {error_msg}", flush=True, file=sys.stderr)
                    print(f"[CatPawProxy] Response headers: {json.dumps(resp_headers, ensure_ascii=False)}", flush=True, file=sys.stderr)
                    return web.json_response(
                        {"error": {"message": f"Upstream error: {error_msg}", "type": "upstream_error", "code": upstream_resp.status}},
                        status=upstream_resp.status,
                    )

                # Get encryption key from response headers
                resp_encrypted_key = upstream_resp.headers.get("encrypted-key", "")

                if is_stream and not has_tools:
                    # Stream response back as OpenAI SSE format
                    # (when tools are present, we buffer for tool call parsing)
                    stream_response = web.StreamResponse(
                        status=200,
                        headers={
                            "Content-Type": "text/event-stream",
                            "Cache-Control": "no-cache",
                            "Connection": "keep-alive",
                            "X-Accel-Buffering": "no",
                        },
                    )
                    await stream_response.prepare(request)

                    chunk_count = 0
                    async for line in upstream_resp.content:
                        # Strip \r and whitespace
                        line_str = line.decode("utf-8", errors="replace").strip()

                        if not line_str:
                            continue

                        # ': ping' is CatPawAI's end-of-stream signal
                        # (original extension treats it as DONE)
                        if line_str.startswith(": ping"):
                            if VERBOSE:
                                print(f"[CatPawProxy] Received : ping (end signal), chunks sent: {chunk_count}", flush=True)
                            # Send final stop chunk
                            final_chunk = {
                                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model,
                                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            }
                            await stream_response.write(f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode())
                            await stream_response.write(b"data: [DONE]\n\n")
                            break

                        if line_str.startswith("data:"):
                            data_content = line_str[5:].strip()

                            if data_content == "[DONE]":
                                await stream_response.write(b"data: [DONE]\n\n")
                                break

                            # Decrypt if needed
                            if resp_encrypted_key:
                                try:
                                    decrypted = decrypt_response_data(data_content, resp_encrypted_key)
                                    catpaw_data = json.loads(decrypted)
                                except Exception as e:
                                    print(f"[CatPawProxy] Decrypt error: {e}", flush=True, file=sys.stderr)
                                    continue
                            else:
                                try:
                                    catpaw_data = json.loads(data_content)
                                except json.JSONDecodeError:
                                    continue

                            # Check for lastOne (end signal, content is usually [DONE])
                            if catpaw_data.get("lastOne", False):
                                # Send final chunk with finish_reason=stop
                                final_chunk = catpaw_sse_to_openai_sse(catpaw_data, model, is_last=True)
                                await stream_response.write(f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode())
                                await stream_response.write(b"data: [DONE]\n\n")
                                if VERBOSE:
                                    print(f"[CatPawProxy] Stream ended (lastOne), chunks sent: {chunk_count}", flush=True)
                                break

                            # Convert to OpenAI format and send
                            openai_chunk = catpaw_sse_to_openai_sse(catpaw_data, model, is_last=False)
                            await stream_response.write(f"data: {json.dumps(openai_chunk, ensure_ascii=False)}\n\n".encode())
                            chunk_count += 1
                        # Other lines (comments, event tags) are silently skipped

                    await stream_response.write_eof()
                    return stream_response
                else:
                    # Non-stream OR has_tools: collect all SSE data, parse for tool calls
                    # When has_tools, we buffer the stream to parse for tool calls.
                    # To prevent client timeout during long buffering, we send SSE
                    # keepalive comments (": keepalive\n\n") every 5 seconds.

                    # Prepare stream response early when streaming + has_tools
                    # so we can send keepalive comments during buffering
                    keepalive_stream = None
                    if is_stream and has_tools:
                        keepalive_stream = web.StreamResponse(
                            status=200,
                            headers={
                                "Content-Type": "text/event-stream",
                                "Cache-Control": "no-cache",
                                "Connection": "keep-alive",
                                "X-Accel-Buffering": "no",
                            },
                        )
                        await keepalive_stream.prepare(request)

                    all_content = []
                    last_keepalive = time.time()
                    async for line in upstream_resp.content:
                        line_str = line.decode("utf-8", errors="replace").strip()

                        if not line_str:
                            # Send keepalive comment every 5 seconds during buffering
                            if keepalive_stream and time.time() - last_keepalive > 5:
                                try:
                                    await keepalive_stream.write(b": keepalive\n\n")
                                    last_keepalive = time.time()
                                except Exception:
                                    pass
                            continue

                        # ': ping' is end-of-stream signal
                        if line_str.startswith(": ping"):
                            break

                        if not line_str.startswith("data:"):
                            # Send keepalive comment every 5 seconds during buffering
                            if keepalive_stream and time.time() - last_keepalive > 5:
                                try:
                                    await keepalive_stream.write(b": keepalive\n\n")
                                    last_keepalive = time.time()
                                except Exception:
                                    pass
                            continue

                        data_content = line_str[5:].strip()
                        if data_content == "[DONE]":
                            break

                        # Decrypt if needed
                        if resp_encrypted_key:
                            try:
                                decrypted = decrypt_response_data(data_content, resp_encrypted_key)
                                catpaw_data = json.loads(decrypted)
                            except Exception:
                                continue
                        else:
                            try:
                                catpaw_data = json.loads(data_content)
                            except json.JSONDecodeError:
                                continue

                        if catpaw_data.get("lastOne", False):
                            break

                        # Extract content
                        choices = catpaw_data.get("choices", [])
                        for choice in choices:
                            delta = choice.get("delta", {})
                            content = delta.get("content", "")
                            if not content and "content" in choice:
                                content = choice.get("content", "")
                            if content:
                                all_content.append(content)

                        # Send keepalive comment every 5 seconds during buffering
                        if keepalive_stream and time.time() - last_keepalive > 5:
                            try:
                                await keepalive_stream.write(b": keepalive\n\n")
                                last_keepalive = time.time()
                            except Exception:
                                pass

                    full_content = "".join(all_content)

                    # Log response content for debugging when tools are present
                    if has_tools and VERBOSE:
                        content_len = len(full_content)
                        preview_start = full_content[:300].replace("\n", "\\n")
                        preview_end = full_content[-300:].replace("\n", "\\n") if content_len > 300 else ""
                        print(f"[CatPawProxy] Response content ({content_len} chars):", flush=True)
                        print(f"[CatPawProxy]   START: {preview_start}", flush=True)
                        if preview_end:
                            print(f"[CatPawProxy]   END:   {preview_end}", flush=True)

                    # Parse for tool calls when tools were provided
                    if has_tools:
                        clean_text, tool_calls = _parse_tool_calls(full_content)

                        if tool_calls:
                            if VERBOSE:
                                print(f"[CatPawProxy] Parsed {len(tool_calls)} tool call(s): {[tc['function']['name'] for tc in tool_calls]}", flush=True)

                            # If original request was streaming, emit as SSE chunks
                            # (reuse the already-prepared keepalive_stream if available)
                            if is_stream:
                                sr = keepalive_stream if keepalive_stream else web.StreamResponse(
                                    status=200,
                                    headers={
                                        "Content-Type": "text/event-stream",
                                        "Cache-Control": "no-cache",
                                        "Connection": "keep-alive",
                                        "X-Accel-Buffering": "no",
                                    },
                                )
                                if not keepalive_stream:
                                    await sr.prepare(request)

                                resp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                                resp_created = int(time.time())

                                # Emit text content first (if any)
                                if clean_text:
                                    text_chunk = {
                                        "id": resp_id,
                                        "object": "chat.completion.chunk",
                                        "created": resp_created,
                                        "model": model,
                                        "choices": [{"index": 0, "delta": {"content": clean_text}, "finish_reason": None}],
                                    }
                                    await sr.write(f"data: {json.dumps(text_chunk, ensure_ascii=False)}\n\n".encode())

                                # Emit each tool call as a separate chunk
                                for i, tc in enumerate(tool_calls):
                                    tc_chunk = {
                                        "id": resp_id,
                                        "object": "chat.completion.chunk",
                                        "created": resp_created,
                                        "model": model,
                                        "choices": [{
                                            "index": 0,
                                            "delta": {
                                                "tool_calls": [{
                                                    "index": i,
                                                    "id": tc["id"],
                                                    "type": "function",
                                                    "function": {
                                                        "name": tc["function"]["name"],
                                                        "arguments": tc["function"]["arguments"],
                                                    },
                                                }]
                                            },
                                            "finish_reason": None,
                                        }],
                                    }
                                    await sr.write(f"data: {json.dumps(tc_chunk, ensure_ascii=False)}\n\n".encode())

                                # Emit final chunk with finish_reason
                                final_chunk = {
                                    "id": resp_id,
                                    "object": "chat.completion.chunk",
                                    "created": resp_created,
                                    "model": model,
                                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                                }
                                await sr.write(f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode())
                                await sr.write(b"data: [DONE]\n\n")
                                await sr.write_eof()

                                if VERBOSE:
                                    print(f"[CatPawProxy] Streamed {len(tool_calls)} tool_calls as SSE chunks", flush=True)
                                return sr
                            else:
                                # Non-stream: return JSON response with tool_calls
                                message = {"role": "assistant", "content": clean_text if clean_text else None}
                                message["tool_calls"] = tool_calls
                                openai_response = {
                                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                                    "object": "chat.completion",
                                    "created": int(time.time()),
                                    "model": model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "message": message,
                                            "finish_reason": "tool_calls",
                                        }
                                    ],
                                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                }
                                return web.json_response(openai_response)
                        else:
                            if VERBOSE:
                                # Log first 200 chars to help diagnose why no tool calls
                                preview = full_content[:200].replace("\n", "\\n")
                                print(f"[CatPawProxy] No tool calls found in response ({len(full_content)} chars), returning as text", flush=True)
                                print(f"[CatPawProxy]   Preview: {preview}", flush=True)

                    # No tool calls or no tools: return as normal text response
                    # If streaming was requested but we buffered (has_tools case),
                    # reuse the keepalive_stream that was already prepared
                    if is_stream and has_tools:
                        sr = keepalive_stream
                        resp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                        resp_created = int(time.time())

                        # Emit full content as a single chunk
                        if full_content:
                            text_chunk = {
                                "id": resp_id,
                                "object": "chat.completion.chunk",
                                "created": resp_created,
                                "model": model,
                                "choices": [{"index": 0, "delta": {"content": full_content}, "finish_reason": None}],
                            }
                            await sr.write(f"data: {json.dumps(text_chunk, ensure_ascii=False)}\n\n".encode())

                        # Emit final stop chunk
                        final_chunk = {
                            "id": resp_id,
                            "object": "chat.completion.chunk",
                            "created": resp_created,
                            "model": model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        }
                        await sr.write(f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode())
                        await sr.write(b"data: [DONE]\n\n")
                        await sr.write_eof()
                        return sr

                    # Non-stream response (no tools, or tools but non-streaming)
                    openai_response = {
                        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": full_content},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    }
                    return web.json_response(openai_response)

    except aiohttp.ClientError as e:
        print(f"[CatPawProxy] Upstream error: {e}", flush=True, file=sys.stderr)
        return web.json_response(
            {"error": {"message": f"Upstream error: {e}", "type": "upstream_error"}},
            status=502,
        )
    except asyncio.TimeoutError:
        return web.json_response(
            {"error": {"message": "Upstream timeout", "type": "timeout_error"}},
            status=504,
        )


async def handle_models(request: web.Request) -> web.Response:
    """Return a fake model list with glm-5.2 for CLIProxyAPI."""
    models = {
        "object": "list",
        "data": [
            {
                "id": MODEL_NAME,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "catpaw",
            },
        ],
    }
    return web.json_response(models)


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    try:
        auth = get_catpaw_auth()
        encryption_enabled = RSA_PUBLIC_KEY_PEM is not None
        return web.json_response({
            "status": "ok",
            "auth": {"mis_id": auth["mis_id"], "token_age": int(time.time() - auth["ts"])},
            "encryption": {"enabled": encryption_enabled},
            "upstream": CATPAW_API_BASE,
        })
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=503)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    app = web.Application(client_max_size=100 * 1024 * 1024)

    # Health check
    app.router.add_get("/health", handle_health)

    # Model list (for CLIProxyAPI discovery)
    app.router.add_get("/v1/models", handle_models)

    # Chat completions (both streaming and non-streaming)
    app.router.add_post("/v1/chat/completions", handle_chat_completions)

    return app


def main():
    print(f"[CatPawProxy] Starting on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    print(f"[CatPawProxy] Upstream: {CATPAW_API_BASE}", flush=True)
    print(f"[CatPawProxy] Data dir: {CATPAW_DATA_DIR}", flush=True)
    print(f"[CatPawProxy] Encryption: {'enabled' if RSA_PUBLIC_KEY_PEM else 'disabled'}", flush=True)
    print(f"[CatPawProxy] Model: {MODEL_NAME} (type_code={MODEL_TYPE_CODE})", flush=True)

    # Pre-load auth
    try:
        auth = get_catpaw_auth()
        print(f"[CatPawProxy] Auth OK: mis_id={auth['mis_id']}", flush=True)
    except Exception as e:
        print(f"[CatPawProxy] WARNING: Auth pre-load failed: {e}", flush=True, file=sys.stderr)
        print("[CatPawProxy] Make sure CatPawAI IDE is logged in.", flush=True, file=sys.stderr)

    app = create_app()
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT, print=None)


if __name__ == "__main__":
    main()
