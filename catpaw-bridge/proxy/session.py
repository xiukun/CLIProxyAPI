"""Session Manager: conversation tracking for context caching.

Uses a two-phase hashing strategy on user messages to detect conversation
continuations and assign stable conversationId values.

Concurrency: _session_store is protected by _session_lock (asyncio.Lock)
to prevent race conditions when multiple concurrent requests check-then-set
the same conversation hash.
"""

import asyncio
import hashlib
import time
import uuid

from proxy.config import VERBOSE
from proxy.utils import _extract_text_content

# Map: conversation_hash -> {conversationId, message_count, timestamp}
_session_store = {}
_session_lock = asyncio.Lock()
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


async def get_or_create_conversation_id(messages: list) -> tuple:
    """Get or create a conversationId for the given message sequence.

    Returns: (conversationId, is_new_conversation)

    Thread-safe via _session_lock — prevents two concurrent requests for the
    same conversation from both creating new conversationIds.
    """
    async with _session_lock:
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
