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
    _normalize_assistant_content,
)
from proxy.utils import _extract_text_content
from proxy.compactor import compact_messages, compact_merged_content
from proxy.memory import get_summary_prefix, save_memory, _conv_hash
from proxy.ccg_context import CUSTOM_SYSTEM_PROMPT as _CUSTOM_SYSTEM_PROMPT
from proxy.ccg_context import get_ccg_routing_context as _get_ccg_routing_context
from proxy.codex_aware import (
    detect_codex,
    extract_codex_instructions,
    build_codex_system_prompt,
    enhance_codex_tools_prompt,
    CodexCompactionConfig,
)
from proxy.claude_aware import (
    detect_claude_code,
    extract_claude_code_instructions,
    build_claude_code_system_prompt,
    enhance_claude_code_tools_prompt,
    ClaudeCodeCompactionConfig,
    extract_useful_reminder_context,
)


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
    "### MultiEdit",
    "### Grep",
    "### Glob",
    "### TodoWrite",
    "### Task",
    "## Tools",
    "Use them when needed to accomplish tasks",
    "You are an AI coding assistant",
    "Here is the current state",
    "Tool availability (filtered by policy)",
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


def _filter_messages(messages: list, has_tools: bool, is_codex: bool = False, is_claude_code: bool = False) -> list:
    """Filter out redundant messages from Claude Code or Codex CLI.

    When has_tools is True:
    - DROP system messages (we inject our own custom system prompt)
      EXCEPTION: For Codex/Claude Code, system messages are EXTRACTED before dropping.
    - DROP user messages that are tool definitions (detected by markers)
    - KEEP actual user questions, assistant responses, and tool results
    - For Claude Code: smart-extract useful context from system-reminders

    When has_tools is False:
    - Keep everything (no redundancy issue)
    """
    if not has_tools:
        return messages

    filtered = []
    dropped = 0
    useful_reminder_context = []  # Collected from Claude Code system-reminders

    for msg in messages:
        role = msg.get("role", "user")
        content = _extract_text_content(msg.get("content", ""))

        # Drop system messages — we replace with our own compact prompt.
        # For Codex/Claude Code, the system prompt has already been extracted
        # by the caller, so it's safe to drop here.
        if role == "system":
            if VERBOSE:
                label = "Codex" if is_codex else ("Claude Code" if is_claude_code else "Claude Code")
                print(f"[CatPawProxy] Dropped {label} system message ({len(content)} chars)", flush=True)
            dropped += 1
            continue

        # Strip <system-reminder> blocks from user messages
        # These contain CLAUDE.md, skills, session context — not the user's
        # actual request. Stripping them can reduce 80KB → 100 bytes.
        if role == "user" and "<system-reminder>" in content:
            if is_claude_code:
                # Claude Code mode: smart-extract useful context before stripping
                stripped, useful_ctx = extract_useful_reminder_context(content)
                if useful_ctx:
                    useful_reminder_context.append(useful_ctx)
            else:
                stripped = _strip_system_reminders(content)

            if stripped:
                # There's real content after stripping — keep it
                if len(stripped) < len(content):
                    if VERBOSE:
                        mode_label = " (Claude Code smart-extract)" if is_claude_code else ""
                        print(f"[CatPawProxy] Stripped system-reminder from user message{mode_label}: {len(content)} -> {len(stripped)} chars", flush=True)
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

    # If we collected useful reminder context (Claude Code mode),
    # append it as a compact system note to the first user message.
    # Allow up to 1000 chars (was 500) since we now extract more useful info
    # (todo state, errors, git status, file modifications).
    if useful_reminder_context and filtered:
        context_text = "\n".join(useful_reminder_context)
        if len(context_text) > 1000:
            context_text = context_text[:997] + "..."
        for msg in filtered:
            if msg.get("role") == "user":
                content = _extract_text_content(msg.get("content", ""))
                enhanced = f"[Context: {context_text}]\n\n{content}"
                if isinstance(msg.get("content"), list):
                    msg["content"] = [{"type": "text", "text": enhanced}]
                else:
                    msg["content"] = enhanced
                break

    if VERBOSE and dropped:
        print(f"[CatPawProxy] Filtered {dropped} redundant message(s), {len(filtered)} remaining", flush=True)

    return filtered


# _CUSTOM_SYSTEM_PROMPT is now imported from proxy.ccg_context
# It includes: role description + initial setup (read CLAUDE.md/AGENTS.md)
# + CCG routing rules + tool calling rules + format requirements
# Built ONCE at module load time for maximum cache efficiency


async def openai_to_catpaw_request(openai_body: dict) -> dict:
    """Convert OpenAI chat completion request to CatPawAI agent-mode format."""
    messages = openai_body.get("messages", [])
    tools = openai_body.get("tools", [])
    has_tools = bool(tools)

    # ------------------------------------------------------------------
    # CLI detection: check if this request comes from Codex CLI or
    # Claude Code. This determines which system prompt, tool definitions,
    # and compaction settings we use.
    # Detection is exclusive: Codex takes priority over Claude Code.
    # ------------------------------------------------------------------
    is_codex, codex_system_content = detect_codex(messages, tools)
    codex_instructions = ""
    if is_codex:
        codex_instructions = extract_codex_instructions(codex_system_content)
        if VERBOSE:
            print(f"[CatPawProxy] Codex mode: extracted {len(codex_instructions)} chars of behavioral instructions", flush=True)

    is_claude_code = False
    claude_code_instructions = ""
    if not is_codex:
        is_claude_code, claude_code_system_content = detect_claude_code(messages, tools)
        if is_claude_code:
            claude_code_instructions = extract_claude_code_instructions(claude_code_system_content)
            if VERBOSE:
                print(f"[CatPawProxy] Claude Code mode: extracted {len(claude_code_instructions)} chars of behavioral instructions", flush=True)

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
            # 1. Filter out redundant system prompt + tool definitions
            #    For Codex/Claude Code: system prompt has already been extracted above
            filtered = _filter_messages(messages, has_tools=True, is_codex=is_codex, is_claude_code=is_claude_code)

            # 2. Truncate remaining messages (per-message hard limits)
            filtered = _truncate_messages(filtered)

            # 3. Build system prompt + tool definitions FIRST, so we know
            #    their size and can pass the overhead to the compactor
            if is_codex:
                # Codex-aware system prompt: includes extracted behavioral
                # rules + apply_patch format + CCG routing + tool calling
                ccg_ctx = _get_ccg_routing_context()
                system_prompt = build_codex_system_prompt(codex_instructions, ccg_ctx)
            elif is_claude_code:
                # Claude Code-aware system prompt: includes extracted behavioral
                # rules + CCG routing + tool calling
                ccg_ctx = _get_ccg_routing_context()
                system_prompt = build_claude_code_system_prompt(claude_code_instructions, ccg_ctx)
            else:
                # Non-CLI: use the original cached system prompt
                system_prompt = _CUSTOM_SYSTEM_PROMPT

            parts = [system_prompt]
            tools_prompt = ""
            if not STRIP_TOOL_DEFINITIONS:
                if is_codex:
                    # Enhanced tool definitions: preserve parameter types,
                    # descriptions, and enum values (critical for apply_patch)
                    tools_prompt = enhance_codex_tools_prompt(tools)
                elif is_claude_code:
                    # Enhanced tool definitions: preserve parameter types,
                    # descriptions, and enum values (critical for Edit/Write)
                    tools_prompt = enhance_claude_code_tools_prompt(tools)
                else:
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

            # 3.6. Pre-compaction normalization: normalize bare JSON in assistant
            #      messages BEFORE compaction. The compactor may truncate message
            #      content, and if it truncates a bare JSON tool call mid-string,
            #      the resulting truncated JSON is unparseable. By normalizing
            #      first, bare JSON becomes <tool_call> tags which are more
            #      resilient to truncation (they're just text, not structured JSON).
            normalized_count = 0
            for msg in filtered:
                if msg.get("role") == "assistant":
                    content = _extract_text_content(msg.get("content", ""))
                    if content and '{"name"' in content and '<tool_call>' not in content:
                        normalized = _normalize_assistant_content(content)
                        if normalized != content:
                            if isinstance(msg.get("content"), list):
                                msg["content"] = [{"type": "text", "text": normalized}]
                            else:
                                msg["content"] = normalized
                            normalized_count += 1
            if normalized_count and VERBOSE:
                print(f"[CatPawProxy] Pre-compaction: normalized {normalized_count} bare JSON assistant message(s)", flush=True)

            # 4. Intelligent compaction: budget accounts for prompt overhead
            #    and encryption ratio (handled inside compactor)
            #    For Codex: use Codex-tuned compaction settings (less aggressive)
            #    For Claude Code: use Claude Code-tuned compaction settings
            compaction_cfg = None
            if is_codex:
                compaction_cfg = CodexCompactionConfig
            elif is_claude_code:
                compaction_cfg = ClaudeCodeCompactionConfig
            filtered = compact_messages(
                filtered, MAX_ENCRYPTED_BODY, overhead=prompt_overhead,
                codex_config=compaction_cfg,
            )

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
