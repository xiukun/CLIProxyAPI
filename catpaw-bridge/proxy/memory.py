"""External memory: file-based conversation summary cache.

When a conversation grows long, we persist a SUMMARY of earlier turns to disk.
On subsequent requests in the same conversation, we load this summary instead
of sending the full message history. This dramatically reduces request body
size for long coding sessions.

Storage format: one JSON file per conversation, stored in .memory/ directory.
Each file contains:
  - conversation_id: stable UUID
  - summary: compressed text of old turns
  - last_updated: timestamp
  - msg_count: number of messages at last summary
"""

import hashlib
import json
import os
import time
from pathlib import Path

from proxy.config import VERBOSE
from proxy.utils import _extract_text_content


_SCRIPT_DIR = Path(__file__).resolve().parent.parent  # catpaw-bridge/
_MEMORY_DIR = _SCRIPT_DIR / ".memory"

# Summarize when conversation has more than this many messages
_SUMMARY_THRESHOLD = 30  # don't summarize short conversations

# Keep this many recent messages intact (don't include in summary)
_KEEP_RECENT = 10

# Max age for memory files (auto-cleanup)
_MEMORY_TTL = 3600  # 1 hour

# Max memory files to keep
_MAX_MEMORY_FILES = 100


def _ensure_memory_dir():
    """Ensure the .memory directory exists."""
    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def _conv_hash(messages: list, conversation_id: str = "") -> str:
    """Generate a stable hash for the conversation.

    Uses BOTH user message content AND conversation_id to ensure
    different sessions with similar messages don't collide.
    """
    user_msgs = []
    for msg in messages:
        if msg.get("role") == "user":
            content = _extract_text_content(msg.get("content", ""))
            # Use first 200 chars for efficiency
            user_msgs.append(content[:200])
    # Include conversation_id in hash to prevent cross-session collisions
    raw = f"{conversation_id}\n" + "\n".join(user_msgs)
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _memory_path(conv_hash: str) -> Path:
    """Get the file path for a conversation's memory."""
    return _MEMORY_DIR / f"{conv_hash}.json"


def load_memory(messages: list, conversation_id: str = "") -> dict | None:
    """Load conversation memory if available.

    Returns dict with:
      - summary: text summary of old turns
      - msg_count: how many messages were summarized
      - conversation_id: stable conversation ID
    Or None if no memory exists.
    """
    if len(messages) < _SUMMARY_THRESHOLD:
        return None

    _ensure_memory_dir()
    ch = _conv_hash(messages, conversation_id)
    path = _memory_path(ch)

    if not path.exists():
        return None

    try:
        with open(path, "r") as f:
            data = json.load(f)
        # Check TTL
        if time.time() - data.get("last_updated", 0) > _MEMORY_TTL:
            path.unlink(missing_ok=True)
            return None
        if VERBOSE:
            print(f"[CatPawProxy] Memory loaded: {data.get('msg_count', 0)} msgs summarized, {len(data.get('summary', ''))} chars", flush=True)
        return data
    except (json.JSONDecodeError, OSError):
        return None


def save_memory(messages: list, conversation_id: str) -> None:
    """Save a summary of old conversation turns to disk.

    Only summarizes messages older than _KEEP_RECENT.
    The summary includes:
      - Role and first N chars of each old message
      - Tool call names (not arguments)
    """
    if len(messages) < _SUMMARY_THRESHOLD:
        return

    _ensure_memory_dir()
    ch = _conv_hash(messages, conversation_id)
    path = _memory_path(ch)

    # Summarize messages before the recent ones
    summary_msgs = messages[:-_KEEP_RECENT] if len(messages) > _KEEP_RECENT else []
    if not summary_msgs:
        return

    summary_parts = []
    for msg in summary_msgs:
        role = msg.get("role", "user")
        content = _extract_text_content(msg.get("content", ""))

        if role == "tool":
            # For tool results, just note what tool was used
            summary_parts.append(f"[tool result: {content[:100]}...]")
        elif role == "assistant":
            # For assistant messages, note the content + tool call names
            text = content[:200] if content else ""
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                tc_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                text += f" [called: {', '.join(tc_names)}]"
            summary_parts.append(f"[assistant: {text}]")
        elif role == "user":
            # For user messages, keep first 150 chars
            summary_parts.append(f"[user: {content[:150]}]")
        elif role == "system":
            # Skip system messages in summary
            pass

    summary = "\n".join(summary_parts)

    data = {
        "conversation_id": conversation_id,
        "summary": summary,
        "msg_count": len(summary_msgs),
        "last_updated": time.time(),
    }

    try:
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        if VERBOSE:
            print(f"[CatPawProxy] Memory saved: {len(summary_msgs)} msgs → {len(summary)} chars summary", flush=True)
    except OSError:
        pass

    # Cleanup old files
    _cleanup_old_files()


def get_summary_prefix(messages: list, conversation_id: str = "") -> str | None:
    """Get a summary prefix to prepend to the conversation.

    If we have a memory for this conversation, return the summary text
    to use as context prefix (instead of sending all old messages).
    Returns None if no memory or conversation is too short.
    """
    mem = load_memory(messages, conversation_id)
    if not mem:
        return None

    summary = mem.get("summary", "")
    if not summary:
        return None

    return f"[Previous conversation summary ({mem['msg_count']} messages):\n{summary}\n--- End of summary ---]\n\n"


def _cleanup_old_files():
    """Remove expired memory files."""
    try:
        files = list(_MEMORY_DIR.glob("*.json"))
        if len(files) > _MAX_MEMORY_FILES:
            # Sort by modification time, remove oldest
            files.sort(key=lambda f: f.stat().st_mtime)
            for f in files[:len(files) - _MAX_MEMORY_FILES]:
                f.unlink(missing_ok=True)

        # Also remove expired files
        now = time.time()
        for f in files:
            if now - f.stat().st_mtime > _MEMORY_TTL:
                f.unlink(missing_ok=True)
    except OSError:
        pass
