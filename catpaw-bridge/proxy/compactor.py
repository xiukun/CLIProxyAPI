"""Intelligent conversation compactor with message-type-aware compression.

When the merged request body exceeds the upstream size limit, this module
progressively compresses conversation history to fit within the budget.

Strategy (applied in order until under budget):
  Phase 1 — Tool-aware result compression:
      Identify which tool produced each result (Read, Bash, Write, Grep, etc.)
      and apply tool-specific truncation:
        - Read:  head 350 + tail 200  (file structure at top + end for context)
        - Bash:  head 400 only        (shell output is most useful at top)
        - Write/Edit: keep as-is      (usually "File created successfully", tiny)
        - Grep:  head 300 + tail 80   (matches at top + count at bottom)
        - List:  head 200 only        (directory listing)
        - Other: head 300 + tail 100  (default)

  Phase 2 — Role-aware old turn summarization:
      Keep the most recent N turns intact. For older turns, summarize
      with role-specific limits:
        - User:      200 chars  (user's actual intent is critical)
        - Assistant: 100 chars  (what the model was doing)
        - Tool:       80 chars  (just tool name + first line of result)
      Also strips tool_calls from summarized assistant messages.

  Phase 3 — Priority-aware hard truncation:
      Trim from oldest first, but skip user messages (small but critical).
      Trim tool results before assistant messages.

CRITICAL: Size calculation MUST include tool_calls arguments, not just
message content. Assistant messages often have empty content but large
tool_calls (e.g., Write with 10KB file content in arguments).
"""

import json
import re
from proxy.config import VERBOSE


def _get_compaction_mode_label(codex_config) -> str:
    """Get a human-readable label for the compaction mode being used."""
    if not codex_config:
        return ""
    class_name = type(codex_config).__name__
    if "ClaudeCode" in class_name:
        return " (Claude Code)"
    elif "Codex" in class_name:
        return " (Codex)"
    return f" ({class_name})"

# Budget: target encrypted body size in bytes.
# Encryption adds ~43% overhead (AES + base64), and JSON structure adds ~5%.
# So the safe ratio from raw text to encrypted is ~0.55.
# We use 0.52 to leave headroom for system prompt + tools prompt overhead.
_COMPACT_RATIO = 0.52

# Phase 1: Tool-specific truncation limits (head + tail chars)
# Key insight: different tools produce different output patterns.
# Read results need file structure (head) + end of file (tail).
# Bash results are most useful at the top (ls, find, error messages).
# Write/Edit results are tiny ("File created successfully") — don't truncate.
_TOOL_TRUNCATION = {
    "Read":          {"head": 2000, "tail": 1000},  # file structure + end (large for code understanding)
    "Bash":          {"head": 1000, "tail": 200},   # shell output: head + error tail
    "Write":         {"head": 99999, "tail": 0},    # keep as-is (usually < 200 chars)
    "Edit":          {"head": 99999, "tail": 0},    # keep as-is
    "MultiEdit":     {"head": 99999, "tail": 0},    # keep as-is
    "Grep":          {"head": 500, "tail": 150},    # matches + file count
    "Glob":          {"head": 400, "tail": 100},    # file list
    "list_dir":      {"head": 300, "tail": 0},      # directory listing
    "List":          {"head": 300, "tail": 0},
    "WebFetch":      {"head": 600, "tail": 200},    # article + conclusion
    "WebSearch":     {"head": 600, "tail": 200},
    "codebase_search": {"head": 500, "tail": 150},
    "TodoWrite":     {"head": 99999, "tail": 0},    # keep as-is (structured data)
    "delete_file":   {"head": 99999, "tail": 0},    # keep as-is
    "run_terminal_cmd": {"head": 1000, "tail": 200}, # command output
}
_DEFAULT_TRUNCATION = {"head": 500, "tail": 200}  # default for unknown tools
_RECENT_TOOL_RESULTS_KEEP = 8  # keep this many recent tool results intact

# Phase 2: Role-specific summary lengths
# User messages contain the actual task — preserve more.
# Assistant messages are explanations — less critical.
# Tool results are just data — least critical when old.
_ROLE_SUMMARY_LEN = {
    "user": 500,       # user's actual intent is critical
    "assistant": 300,  # what the model was doing
    "tool": 250,       # just tool name + first line
    "system": 100,     # usually already filtered, but just in case
}
_DEFAULT_SUMMARY_LEN = 300
_RECENT_TURNS_KEEP = 8    # keep this many most recent message turns intact

# Phase 3: Hard truncation per message
_HARD_TRUNCATE_LIMIT = 5000  # final hard limit per message after phases 1&2

# Priority for Phase 3 trimming (lower = trim first)
# Tool results are largest and least valuable when old → trim first
# User messages are small but critical → trim last
_TRIM_PRIORITY = {
    "tool": 0,       # trim first
    "assistant": 1,  # trim second
    "user": 2,       # trim last (preserve user intent)
    "system": 3,     # never trim (should already be filtered)
}


def _measure_message_size(msg: dict) -> int:
    """Measure the FULL text representation size of a message.

    This includes:
    - message content (text)
    - tool_calls arguments (function name + arguments JSON)

    This is critical because _convert_messages_with_tools outputs tool_calls
    as text, so we must count them to get an accurate size estimate.
    """
    from proxy.utils import _extract_text_content

    size = len(_extract_text_content(msg.get("content", "")))

    # Count tool_calls arguments (assistant messages)
    tool_calls = msg.get("tool_calls", [])
    if tool_calls:
        for tc in tool_calls:
            func = tc.get("function", {})
            name = func.get("name", "")
            args = func.get("arguments", "{}")
            size += len(name) + len(args) + 60  # 60 chars for formatting overhead

    return size


def estimate_target_budget(max_encrypted_body: int, overhead: int = 0) -> int:
    """Estimate the unencrypted text budget from the encrypted body limit.

    Args:
        max_encrypted_body: max encrypted body size in bytes
        overhead: bytes already consumed by system prompt + tools prompt
                  (subtracted from the budget before applying ratio)
    """
    return max(0, round((max_encrypted_body - overhead) * _COMPACT_RATIO))


def _build_tool_call_id_map(messages: list) -> dict:
    """Build a mapping: tool_call_id → tool_name.

    Scans assistant messages for tool_calls and maps each call's ID to
    the tool function name. This lets us identify which tool produced
    a given tool result message.
    """
    id_map = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls") or []
        for tc in tool_calls:
            tc_id = tc.get("id", "")
            tc_name = tc.get("function", {}).get("name", "")
            if tc_id and tc_name:
                id_map[tc_id] = tc_name
    return id_map


def _get_tool_name_for_result(msg: dict, tool_call_id_map: dict) -> str:
    """Get the tool name that produced a tool result message.

    Tool result messages have a 'tool_call_id' field that matches the
    assistant's tool_calls[].id. We look up the name from our map.
    """
    tc_id = msg.get("tool_call_id", "")
    if tc_id and tc_id in tool_call_id_map:
        return tool_call_id_map[tc_id]
    return ""


def _truncate_tool_result(content: str, tool_name: str, tool_truncation: dict = None) -> str:
    """Truncate a tool result using tool-specific limits.

    Returns the (possibly truncated) content.
    If content is small enough, returns it unchanged.

    For Read/read_file results, the truncation marker includes line number
    ranges so the model knows which lines were omitted. This helps the model
    re-read the omitted section with offset/limit if it needs to edit there.

    Args:
        tool_truncation: override truncation table (for Codex-aware compaction)
    """
    truncation_table = tool_truncation if tool_truncation else _TOOL_TRUNCATION
    limits = truncation_table.get(tool_name, _DEFAULT_TRUNCATION)
    head = limits["head"]
    tail = limits["tail"]

    # If content is small enough, don't truncate
    threshold = head + tail + 50 if tail > 0 else head + 50
    if len(content) <= threshold:
        return content

    # "Keep as-is" guard (head=99999 means don't truncate this tool type)
    if head >= 99999:
        return content

    if tail > 0:
        # Head + marker + Tail
        head_part = content[:head]
        tail_part = content[-tail:]
        skipped = len(content) - head - tail

        # For Read/read_file results, include line number range in the marker
        # so the model knows which lines were omitted and can re-read them.
        if tool_name in ("Read", "read_file"):
            head_lines = head_part.count('\n')
            # Calculate the line number where tail_part begins
            # total_newlines_in_content minus newlines_in_tail gives the
            # last newline before tail_part; +1 = first line of tail_part
            total_newlines = content.count('\n')
            tail_newlines = tail_part.count('\n')
            tail_first_line = total_newlines - tail_newlines + 1
            omitted_lines = tail_first_line - head_lines - 1
            if omitted_lines > 0:
                marker = (f"\n\n... [lines {head_lines + 1}-{tail_first_line - 1} omitted, "
                          f"{omitted_lines} lines, {skipped} chars] ...\n\n")
            else:
                # Omitted section is within a single line (partial line)
                marker = f"\n\n... [partial line omitted, {skipped} chars] ...\n\n"
        else:
            marker = f"\n\n... [compacted: {skipped} chars omitted] ...\n\n"

        return f"{head_part}{marker}{tail_part}"
    else:
        # Head only
        head_part = content[:head]
        skipped = len(content) - head

        # For Read/read_file results, include line number range
        if tool_name in ("Read", "read_file"):
            head_lines = head_part.count('\n')
            # Total lines = newlines + 1 if content doesn't end with newline
            total_lines = content.count('\n')
            if not content.endswith('\n'):
                total_lines += 1
            marker = f"\n... [lines {head_lines + 1}-{total_lines} omitted, {skipped} chars] ..."
        else:
            marker = f"\n... [compacted: {skipped} chars omitted] ..."

        return f"{head_part}{marker}"


def compact_messages(messages: list, max_encrypted_body: int, overhead: int = 0, codex_config=None) -> list:
    """Compact message list to fit within the encrypted body budget.

    Args:
        messages: list of OpenAI-format message dicts
        max_encrypted_body: max encrypted body size in bytes
        overhead: bytes consumed by system prompt + tools prompt (not counted
                  in message text, but part of the final body)
        codex_config: optional compaction config instance (CodexCompactionConfig
                      or ClaudeCodeCompactionConfig) for CLI-aware compaction
                      (less aggressive, larger retention limits)

    Returns:
        Compacted message list (may be the same list if no compaction needed)
    """
    budget = estimate_target_budget(max_encrypted_body, overhead)

    # Calculate current total text size (INCLUDING tool_calls arguments!)
    total_size = sum(_measure_message_size(m) for m in messages)

    if total_size <= budget:
        if VERBOSE:
            print(f"[CatPawProxy] Compactor: total={total_size} bytes, budget={budget} bytes — no compaction needed", flush=True)
        return messages

    if VERBOSE:
        print(f"[CatPawProxy] Compactor: total={total_size} bytes ({total_size/1024:.1f} KB) > budget={budget} bytes ({budget/1024:.1f} KB) — starting compaction", flush=True)

    # Make a deep copy so we don't mutate the original
    result = []
    for msg in messages:
        msg_copy = dict(msg)
        if isinstance(msg.get("content"), list):
            msg_copy["content"] = [dict(p) if isinstance(p, dict) else p for p in msg["content"]]
        if msg.get("tool_calls"):
            msg_copy["tool_calls"] = [dict(tc) if isinstance(tc, dict) else tc for tc in msg["tool_calls"]]
        result.append(msg_copy)

    # Build tool_call_id → tool_name mapping for tool-aware compression
    tool_call_id_map = _build_tool_call_id_map(result)

    # ---- Phase 1: Tool-aware result compression ----
    result, size_after_p1 = _phase1_compress_tool_results(result, tool_call_id_map, codex_config)
    if VERBOSE:
        mode = _get_compaction_mode_label(codex_config)
        print(f"[CatPawProxy] Compactor Phase 1{mode} (tool results): {total_size} -> {size_after_p1} bytes", flush=True)

    if size_after_p1 <= budget:
        return result

    # ---- Phase 2: Role-aware old turn summarization ----
    result, size_after_p2 = _phase2_summarize_old_turns(result, tool_call_id_map, codex_config)
    if VERBOSE:
        print(f"[CatPawProxy] Compactor Phase 2 (old turns): {size_after_p1} -> {size_after_p2} bytes", flush=True)

    if size_after_p2 <= budget:
        return result

    # ---- Phase 3: Priority-aware hard truncation ----
    result, size_after_p3 = _phase3_hard_truncate(result, budget, tool_call_id_map, codex_config)
    if VERBOSE:
        print(f"[CatPawProxy] Compactor Phase 3 (hard trunc): {size_after_p2} -> {size_after_p3} bytes", flush=True)

    return result


def _find_edit_paired_reads(messages: list, tool_call_id_map: dict) -> set:
    """Find indices of Read tool results that are paired with a subsequent Edit.

    When an Edit follows a Read, the model needs the Read result to get exact
    context for old_string. Compressing the Read result would break the Edit.
    This function identifies such protected Read results.

    Enhanced: protects ALL Read results within the same turn as an Edit,
    not just the single most recent Read. This handles multi-step workflows
    where the model reads multiple files before editing.

    Args:
        messages: message list
        tool_call_id_map: mapping of tool_call_id → tool_name

    Returns:
        Set of message indices that are Read results paired with a subsequent Edit.
    """
    protected = set()
    # Find all Edit/MultiEdit/apply_patch tool calls (assistant messages with tool_calls)
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls") or []
        has_edit = False
        for tc in tool_calls:
            tc_name = tc.get("function", {}).get("name", "")
            if tc_name in ("Edit", "MultiEdit", "apply_patch"):
                has_edit = True
                break
        if not has_edit:
            continue

        # Walk backwards from the Edit to the start of the current turn
        # (i.e., until we hit a user message), protecting ALL Read results
        # in the same turn — not just the most recent one.
        for j in range(i - 1, -1, -1):
            prev_msg = messages[j]
            if prev_msg.get("role") == "user":
                break  # Reached the start of the current turn
            if prev_msg.get("role") == "tool":
                tc_id = prev_msg.get("tool_call_id", "")
                tool_name = tool_call_id_map.get(tc_id, "")
                if tool_name in ("Read", "read_file"):
                    protected.add(j)
                    # Do NOT break — keep scanning for other Read results in the same turn

    return protected


def _phase1_compress_tool_results(messages: list, tool_call_id_map: dict, codex_config=None) -> tuple:
    """Phase 1: Compress OLD tool results using tool-specific strategies.

    Identifies which tool produced each result (Read, Bash, Write, etc.)
    and applies appropriate truncation:
      - Read results: head 350 + tail 200 (file structure + end)
      - Bash results: head 400 only (shell output, head is key)
      - Write/Edit results: keep as-is (already tiny)
      - Grep results: head 300 + tail 80 (matches + count)

    When codex_config is provided, uses CLI-tuned limits (larger retention).
    Supports CodexCompactionConfig and ClaudeCodeCompactionConfig.
    The most recent _RECENT_TOOL_RESULTS_KEEP tool results are kept intact.

    CRITICAL: Read results that are paired with a subsequent Edit are NEVER
    compressed — the Edit needs the exact context from Read to match old_string.
    """
    # Use Codex-tuned settings if provided
    if codex_config:
        tool_truncation = codex_config.TOOL_TRUNCATION
        recent_keep = codex_config.RECENT_TOOL_RESULTS_KEEP
    else:
        tool_truncation = _TOOL_TRUNCATION
        recent_keep = _RECENT_TOOL_RESULTS_KEEP
    from proxy.utils import _extract_text_content

    # Find Read results paired with subsequent Edits — these are protected
    protected_reads = _find_edit_paired_reads(messages, tool_call_id_map)

    # Find indices of all tool messages
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]

    # Determine which tool results to compress (skip the last N)
    compress_from_end = len(tool_indices) - recent_keep
    indices_to_compress = set(tool_indices[:max(0, compress_from_end)])

    # Remove protected Read results from compression set
    protected_count = len(indices_to_compress & protected_reads)
    indices_to_compress -= protected_reads

    total = 0
    compressed_by_tool = {}  # tool_name → count

    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = _extract_text_content(msg.get("content", ""))

        if i in indices_to_compress and role == "tool":
            # Identify which tool produced this result
            tool_name = _get_tool_name_for_result(msg, tool_call_id_map)
            original_len = len(content)

            compressed = _truncate_tool_result(content, tool_name, tool_truncation)

            if len(compressed) < original_len:
                compressed_by_tool[tool_name or "unknown"] = compressed_by_tool.get(tool_name or "unknown", 0) + 1

            if isinstance(msg.get("content"), list):
                msg["content"] = [{"type": "text", "text": compressed}]
            else:
                msg["content"] = compressed

            total += len(compressed)
        else:
            total += _measure_message_size(msg)

    if VERBOSE and compressed_by_tool:
        details = ", ".join(f"{k}={v}" for k, v in sorted(compressed_by_tool.items(), key=lambda x: -x[1]))
        protect_note = f", protected {protected_count} Read+Edit paired results" if protected_count else ""
        print(f"[CatPawProxy]   Phase 1: compressed {sum(compressed_by_tool.values())} tool results ({details}), kept {recent_keep} recent intact{protect_note}", flush=True)

    return messages, total


def _phase2_summarize_old_turns(messages: list, tool_call_id_map: dict, codex_config=None) -> tuple:
    """Phase 2: Summarize old conversation turns with role-specific limits.

    A "turn" is a user message + assistant response (+ optional tool results).
    We keep the last _RECENT_TURNS_KEEP turns fully intact and summarize older
    ones with role-specific summary lengths:
      - User:      200 chars (user's actual intent is critical)
      - Assistant: 100 chars (what the model was doing)
      - Tool:       80 chars (just tool name + first line of result)

    When codex_config is provided, uses larger limits to preserve more context.
    Supports CodexCompactionConfig and ClaudeCodeCompactionConfig.
    """
    if codex_config:
        role_summary_len = codex_config.ROLE_SUMMARY_LEN
        recent_turns_keep = codex_config.RECENT_TURNS_KEEP
    else:
        role_summary_len = _ROLE_SUMMARY_LEN
        recent_turns_keep = _RECENT_TURNS_KEEP
    # Identify turn boundaries (each user message starts a new turn)
    turn_starts = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "user":
            turn_starts.append(i)

    if len(turn_starts) <= recent_turns_keep:
        total = sum(_measure_message_size(m) for m in messages)
        return messages, total

    keep_from_idx = turn_starts[-recent_turns_keep] if len(turn_starts) >= recent_turns_keep else 0

    from proxy.utils import _extract_text_content

    total = 0
    summarized_count = 0
    stripped_tc_count = 0

    for i, msg in enumerate(messages):
        if i >= keep_from_idx:
            total += _measure_message_size(msg)
            continue

        role = msg.get("role", "user")
        content_size = _measure_message_size(msg)

        # For old assistant messages, strip tool_calls entirely
        if role == "assistant" and msg.get("tool_calls"):
            tc_count = len(msg["tool_calls"])
            msg.pop("tool_calls", None)
            stripped_tc_count += tc_count
            content_size = _measure_message_size(msg)

        content = _extract_text_content(msg.get("content", ""))

        # Get role-specific summary length
        summary_len = role_summary_len.get(role, _DEFAULT_SUMMARY_LEN)

        if not content or len(content) <= summary_len:
            total += content_size
            continue

        # For tool messages, include the tool name in the summary
        if role == "tool":
            tool_name = _get_tool_name_for_result(msg, tool_call_id_map)
            prefix = f"[tool:{tool_name}] " if tool_name else "[tool] "
            summary = content[:summary_len - len(prefix)].rstrip()
            suffix = f"... [summarized, original {len(content)} chars]"
            compressed = f"{prefix}{summary}{suffix}"
        else:
            summary = content[:summary_len].rstrip()
            suffix = f"... [summarized, original {len(content)} chars]"
            compressed = f"[{role}] {summary}{suffix}"

        if isinstance(msg.get("content"), list):
            msg["content"] = [{"type": "text", "text": compressed}]
        else:
            msg["content"] = compressed

        summarized_count += 1
        total += len(compressed)

    if VERBOSE and (summarized_count or stripped_tc_count):
        print(f"[CatPawProxy]   Phase 2: summarized {summarized_count} old message(s), stripped {stripped_tc_count} old tool_calls, kept recent from index {keep_from_idx}", flush=True)

    return messages, total


def _phase3_hard_truncate(messages: list, budget: int, tool_call_id_map: dict, codex_config=None) -> tuple:
    """Phase 3: Hard truncate with priority-aware ordering.

    Trims from oldest first, but respects priority:
      - Tool results trimmed first (largest, least valuable when old)
      - Assistant messages trimmed second
      - User messages trimmed last (small but critical)

    This ensures user intent is preserved even under extreme pressure.
    When codex_config is provided, uses a larger hard truncate limit.
    Supports CodexCompactionConfig and ClaudeCodeCompactionConfig.
    """
    hard_limit = codex_config.HARD_TRUNCATE_LIMIT if codex_config else _HARD_TRUNCATE_LIMIT
    from proxy.utils import _extract_text_content

    # Calculate sizes
    sizes = []
    total = 0
    for msg in messages:
        s = _measure_message_size(msg)
        sizes.append(s)
        total += s

    if total <= budget:
        return messages, total

    # Build a sorted index: oldest first, but grouped by priority
    # Priority: tool (0) < assistant (1) < user (2) < system (3)
    # Within the same priority, oldest first
    def _trim_order(idx):
        role = messages[idx].get("role", "user")
        priority = _TRIM_PRIORITY.get(role, 2)
        return (priority, idx)

    trim_order = sorted(range(len(messages)), key=_trim_order)

    overflow = total - budget
    for i in trim_order:
        if overflow <= 0:
            break

        content = _extract_text_content(messages[i].get("content", ""))
        if len(content) <= hard_limit:
            continue

        excess = len(content) - hard_limit
        trim = min(excess, overflow)
        if trim <= 0:
            continue

        new_len = len(content) - trim
        truncated = content[:new_len].rstrip() + f"\n... [hard-truncated, {trim} chars removed]"
        if isinstance(messages[i].get("content"), list):
            messages[i]["content"] = [{"type": "text", "text": truncated}]
        else:
            messages[i]["content"] = truncated

        # Also strip tool_calls from hard-truncated messages
        if messages[i].get("tool_calls"):
            messages[i].pop("tool_calls", None)

        overflow -= trim
        if VERBOSE:
            role = messages[i].get("role", "?")
            print(f"[CatPawProxy]   Phase 3: hard-truncated msg[{i}] ({role}) by {trim} chars", flush=True)

    # Recalculate total
    total = sum(_measure_message_size(m) for m in messages)
    return messages, total


def compact_merged_content(merged_content: str, max_encrypted_body: int) -> str:
    """Compact a pre-merged content string (used in non-tool mode).

    This is simpler than compact_messages — it just truncates the merged
    text to fit the budget, keeping the beginning and end.
    """
    budget = estimate_target_budget(max_encrypted_body)

    if len(merged_content) <= budget:
        return merged_content

    # Keep head (context) + tail (recent messages / user's actual question)
    head_size = int(budget * 0.4)
    tail_size = budget - head_size - 100  # 100 chars for marker

    head = merged_content[:head_size]
    tail = merged_content[-tail_size:]
    skipped = len(merged_content) - head_size - tail_size

    result = f"{head}\n\n... [compacted: {skipped} chars omitted] ...\n\n{tail}"

    if VERBOSE:
        print(f"[CatPawProxy] Compactor (merged): {len(merged_content)} -> {len(result)} bytes", flush=True)

    return result
