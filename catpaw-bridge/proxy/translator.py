"""Translator: OpenAI <-> CatPawAI request/response format conversion.

Key design decisions:
  - Claude Code sends 100KB+ of system prompt + tool definitions as messages.
    CatPawAI upstream rejects requests >~64KB encrypted. We DROP Claude Code's
    verbose messages and replace them with a compact custom system prompt.
  - CatPawAI response format: {"lastOne":false,"content":"text","model":"glm-5.2"}
    The `content` field is at the TOP LEVEL, not inside `choices[].delta`.
  - planPromptEnabled: false (avoids agent XML injection)
  - chatApplyModeType: "chat"
  - conversationId: stable per-conversation ID for tracking
"""

import json
import re
import time
import uuid

from proxy.config import MODEL_TYPE_CODE, MAX_MESSAGE_CONTENT, MAX_ENCRYPTED_BODY, STRIP_TOOL_DEFINITIONS, VERBOSE
from proxy.session import get_or_create_conversation_id
from proxy.toolcall import (
    _inject_tools_prompt,
    _convert_messages_with_tools,
    _strip_agent_xml,
)
from proxy.utils import _extract_text_content
from proxy.compactor import compact_messages, compact_merged_content
from proxy.memory import get_summary_prefix, save_memory, _conv_hash


# ---------------------------------------------------------------------------
# Patterns to detect Claude Code's tool-definition messages (redundant with
# our own compact injection). These messages are 80-120KB and must be dropped.
# ---------------------------------------------------------------------------
_TOOL_DEF_MARKERS = [
    "You have access to the following tools",
    "### Agent",
    "### Read",
    "### Bash",
    "### Write",
    "### Edit",
    "## Tools",
    "Use them when needed to accomplish tasks",
    "You are an AI coding assistant",
    "Here is the current state",
]


def _is_tool_definition_message(content: str) -> bool:
    """Check if a user message is actually Claude Code's tool definitions."""
    if len(content) < 500:
        return False
    head = content[:800]
    return any(marker in head for marker in _TOOL_DEF_MARKERS)


# Regex to match <system-reminder>...</system-reminder> blocks (non-greedy)
_RE_SYSTEM_REMINDER = re.compile(r'<system-reminder>.*?</system-reminder>', re.DOTALL)


def _strip_system_reminders(content: str) -> str:
    """Strip <system-reminder> blocks from user messages.

    Claude Code injects system-reminders with CLAUDE.md, skills, session
    context, etc. These are NOT the user's actual request and consume
    massive space (often 80KB+). We strip them to keep only real content.
    """
    return _RE_SYSTEM_REMINDER.sub('', content).strip()


def _smart_truncate(content: str, limit: int) -> str:
    """Truncate content keeping both beginning and end.

    The user's actual request is often at the END of a message (after
    system-reminders and context). Standard head-only truncation would
    cut it off. We keep the first 30% and last 70% to preserve both
    context and the actual request.
    """
    if len(content) <= limit:
        return content
    head_size = int(limit * 0.3)
    tail_size = limit - head_size - 60
    marker = f"\n... [truncated {len(content) - limit} chars] ...\n"
    return content[:head_size] + marker + content[-tail_size:]


def _truncate_messages(messages: list) -> list:
    """Truncate large messages to fit upstream body size limit.

    - System messages: truncated to MAX_SYSTEM_CONTENT (8KB)
    - Other messages: smart-truncated to MAX_MESSAGE_CONTENT (15KB)
      (keeps beginning + end to preserve user request at message tail)
    """
    from proxy.config import MAX_SYSTEM_CONTENT
    result = []
    for msg in messages:
        msg_copy = dict(msg)
        role = msg_copy.get("role", "user")
        content = _extract_text_content(msg_copy.get("content", ""))
        if not content:
            result.append(msg_copy)
            continue

        limit = MAX_SYSTEM_CONTENT if role == "system" else MAX_MESSAGE_CONTENT
        if len(content) > limit:
            truncated = _smart_truncate(content, limit)
            if isinstance(msg.get("content"), list):
                msg_copy["content"] = [{"type": "text", "text": truncated}]
            else:
                msg_copy["content"] = truncated
            if VERBOSE:
                print(f"[CatPawProxy] Truncated {role} message: {len(content)} -> {len(truncated)} chars", flush=True)
        result.append(msg_copy)
    return result


def _filter_messages(messages: list, has_tools: bool) -> list:
    """Filter out Claude Code's redundant messages.

    When has_tools is True:
    - DROP system messages (we inject our own custom system prompt)
    - DROP user messages that are tool definitions (detected by markers)
    - KEEP actual user questions, assistant responses, and tool results

    When has_tools is False:
    - Keep everything (no redundancy issue)
    """
    if not has_tools:
        return messages

    filtered = []
    dropped = 0
    for msg in messages:
        role = msg.get("role", "user")
        content = _extract_text_content(msg.get("content", ""))

        # Drop system messages — we replace with our own compact prompt
        if role == "system":
            if VERBOSE:
                print(f"[CatPawProxy] Dropped system message ({len(content)} chars)", flush=True)
            dropped += 1
            continue

        # Strip <system-reminder> blocks from user messages
        # These contain CLAUDE.md, skills, session context — not the user's
        # actual request. Stripping them can reduce 80KB → 100 bytes.
        if role == "user" and "<system-reminder>" in content:
            stripped = _strip_system_reminders(content)
            if stripped:
                # There's real content after stripping — keep it
                if len(stripped) < len(content):
                    if VERBOSE:
                        print(f"[CatPawProxy] Stripped system-reminder from user message: {len(content)} -> {len(stripped)} chars", flush=True)
                    msg = dict(msg)
                    if isinstance(msg.get("content"), list):
                        msg["content"] = [{"type": "text", "text": stripped}]
                    else:
                        msg["content"] = stripped
                    content = stripped
            else:
                # Message was ONLY system-reminder — drop it entirely
                if VERBOSE:
                    print(f"[CatPawProxy] Dropped system-reminder-only user message ({len(content)} chars)", flush=True)
                dropped += 1
                continue

        # Drop tool-definition user messages
        if role == "user" and _is_tool_definition_message(content):
            if VERBOSE:
                print(f"[CatPawProxy] Dropped tool-definition user message ({len(content)} chars)", flush=True)
            dropped += 1
            continue

        filtered.append(msg)

    if VERBOSE and dropped:
        print(f"[CatPawProxy] Filtered {dropped} redundant message(s), {len(filtered)} remaining", flush=True)

    return filtered


# Custom system prompt to replace Claude Code's verbose one
_CUSTOM_SYSTEM_PROMPT = (
    "You are an AI coding assistant. Follow the user's instructions carefully.\n"
    "\n"
    "## Tool Calling (CRITICAL)\n"
    "When you need to use ANY tool (Read, Write, Edit, Bash, etc.), output:\n"
    '<tool_call>{"name":"ToolName","arguments":{"param":"value"}}</tool_call>\n'
    "\n"
    "### Rules\n"
    "- Output ONE tool call at a time, then WAIT for the result before continuing.\n"
    "- Do NOT output multiple tool calls in one response.\n"
    "- Do NOT describe what you will do — just call the tool directly.\n"
    "- Do NOT ask for confirmation before writing/editing files.\n"
    "- For Read: always Read the file BEFORE editing it.\n"
    "- For Edit: use the EXACT text from the Read result as old_string.\n"
    "- For Write: provide the COMPLETE file content, not just a fragment.\n"
    "- Results arrive as 'Tool Result: ...'\n"
    "\n"
    "### Format Requirements (STRICT)\n"
    "- ONLY use <tool_call> tags. Do NOT use any other format.\n"
    '- NO: ToolName<parameters>{"key":"value"}</parameters>\n'
    "- NO: ToolName(param=\"value\")\n"
    "- NO: ```json blocks with tool calls\n"
    "- NO: bare JSON without <tool_call> tags\n"
    '- Example: <tool_call>{"name":"shell","arguments":{"command":"ls -la"}}</tool_call>'
)


async def openai_to_catpaw_request(openai_body: dict) -> dict:
    """Convert OpenAI chat completion request to CatPawAI agent-mode format."""
    messages = openai_body.get("messages", [])
    tools = openai_body.get("tools", [])
    has_tools = bool(tools)

    # Log individual message sizes for debugging
    if VERBOSE and messages:
        for i, msg in enumerate(messages):
            role = msg.get("role", "user")
            content = _extract_text_content(msg.get("content", ""))
            print(f"[CatPawProxy]   msg[{i}] role={role} len={len(content)} ({len(content)/1024:.1f} KB)", flush=True)
            # Print first 200 chars for debugging message content
            preview = content[:200].replace("\n", "\\n")
            print(f"[CatPawProxy]     preview: {preview}", flush=True)

    # Get or create conversation session
    conversation_id, is_new = await get_or_create_conversation_id(messages)

    # Build the merged content
    if len(messages) == 0:
        merged_content = ""
    elif len(messages) == 1 and not has_tools:
        merged_content = _extract_text_content(messages[0].get("content", ""))
    else:
        if has_tools:
            # ---- Tool-aware mode ----
            # 1. Filter out Claude Code's redundant system prompt + tool definitions
            filtered = _filter_messages(messages, has_tools=True)

            # 2. Truncate remaining messages (per-message hard limits)
            filtered = _truncate_messages(filtered)

            # 3. Build system prompt + tool definitions FIRST, so we know
            #    their size and can pass the overhead to the compactor
            parts = [_CUSTOM_SYSTEM_PROMPT]
            tools_prompt = ""
            if not STRIP_TOOL_DEFINITIONS:
                tools_prompt = _inject_tools_prompt(tools)
                if tools_prompt:
                    parts.append(tools_prompt)

            # 3.5. Load external memory — if we have a summary of old turns,
            #      prepend it and drop the old messages it covers
            #
            # CRITICAL: Compute the memory hash and save a copy of filtered
            # BEFORE any modification (memory drop, compaction). The hash
            # must be consistent between save and load:
            #   - save_memory uses exclude_last=False (all user messages)
            #   - load_memory uses exclude_last=True (drops last user msg)
            # Without pre-computing, compaction modifies user message content
            # (Phase 2 summarizes to 200 chars), causing the hash to differ
            # and the memory to never load on subsequent requests.
            pre_modification_filtered = list(filtered)  # shallow copy (dicts not mutated later)
            pre_compaction_hash = _conv_hash(filtered, conversation_id, exclude_last=False)

            memory_prefix = get_summary_prefix(filtered, conversation_id)
            if memory_prefix:
                # Drop old messages that are covered by the summary
                from proxy.memory import _KEEP_RECENT
                if len(filtered) > _KEEP_RECENT:
                    old_msgs = filtered[:-_KEEP_RECENT]
                    recent_msgs = filtered[-_KEEP_RECENT:]
                    # Replace old messages with the summary prefix
                    filtered = recent_msgs
                    parts.append(memory_prefix)
                    if VERBOSE:
                        print(f"[CatPawProxy] Memory: replaced {len(old_msgs)} old msgs with summary prefix ({len(memory_prefix)} chars)", flush=True)

            prompt_overhead = len("\n\n".join(parts))  # system + tools prompt + memory size

            # 4. Intelligent compaction: budget accounts for prompt overhead
            #    and encryption ratio (handled inside compactor)
            filtered = compact_messages(filtered, MAX_ENCRYPTED_BODY, overhead=prompt_overhead)

            # 5. Add conversation history
            conversation = _convert_messages_with_tools(filtered)
            if conversation:
                parts.append(conversation)

            merged_content = "\n\n".join(parts)

            # 6. Post-compaction verification: if merged content still exceeds
            #    the safe budget, hard-truncate it (last resort)
            from proxy.compactor import estimate_target_budget as _est_budget
            safe_budget = _est_budget(MAX_ENCRYPTED_BODY)
            if len(merged_content) > safe_budget:
                if VERBOSE:
                    print(f"[CatPawProxy] Post-compaction truncation: {len(merged_content)} -> {safe_budget} bytes", flush=True)
                merged_content = compact_merged_content(merged_content, MAX_ENCRYPTED_BODY)

            # 7. Save memory for future requests in this conversation
            #    CRITICAL: use pre_modification_filtered (before memory drop +
            #    compaction) and pre_compaction_hash (computed before compaction
            #    modified user message content). This ensures:
            #    - The summary covers all messages (not just the 10 kept recent)
            #    - The hash matches what load_memory will compute next request
            #      (load uses exclude_last=True, which drops the new user msg,
            #      giving the same hash as our exclude_last=False storage hash)
            save_memory(pre_modification_filtered, conversation_id, conv_hash=pre_compaction_hash)
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
            # Compact merged content if it exceeds budget
            merged_content = compact_merged_content(merged_content, MAX_ENCRYPTED_BODY)

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
        "userModelTypeCode": MODEL_TYPE_CODE,
        "extra": {},
        "planPromptEnabled": False,
        "chatApplyModeType": "chat",
        "parentSuggestUuid": "",
        "before": "",
        "after": "",
    }


def _extract_content_from_catpaw(catpaw_data: dict) -> str:
    """Extract text content from a CatPawAI SSE data object.

    CatPawAI has TWO response formats:
    1. Native format: {"content": "text", "lastOne": false, ...}
       — content is at the TOP LEVEL
    2. OpenAI format: {"choices": [{"delta": {"content": "text"}}]}
       — content is inside choices[].delta
    """
    # Format 1: Native CatPawAI (content at top level)
    content = catpaw_data.get("content", "")
    if content:
        # Skip [DONE] marker
        if content == "[DONE]":
            return ""
        return _strip_agent_xml(content)

    # Format 2: OpenAI-style (content inside choices)
    choices = catpaw_data.get("choices", [])
    for choice in choices:
        delta = choice.get("delta", {})
        c = delta.get("content", "")
        if not c and "content" in choice:
            c = choice.get("content", "")
        if c and c != "[DONE]":
            return _strip_agent_xml(c)

    return ""


def catpaw_sse_to_openai_sse(catpaw_data: dict, model: str, is_last: bool = False, prev_content: str = "") -> dict:
    """Convert a single CatPawAI SSE data object to OpenAI streaming format.

    CatPawAI's `content` field contains the FULL accumulated text, not a delta.
    We compute the delta by comparing with prev_content.
    """
    content = _extract_content_from_catpaw(catpaw_data)

    # Compute delta: only the new part of the content
    if content and prev_content and content.startswith(prev_content):
        delta_content = content[len(prev_content):]
    elif content:
        delta_content = content
    else:
        delta_content = ""

    finish_reason = None
    if is_last:
        finish_reason = "stop"

    openai_choice = {
        "index": 0,
        "delta": {"content": delta_content} if delta_content and not is_last else {},
        "finish_reason": finish_reason,
    }

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [openai_choice],
    }


def catpaw_to_openai_response(catpaw_data: dict, model: str) -> dict:
    """Convert CatPawAI non-streaming response to OpenAI format."""
    content = _extract_content_from_catpaw(catpaw_data)

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": catpaw_data.get("usage", {}),
    }
