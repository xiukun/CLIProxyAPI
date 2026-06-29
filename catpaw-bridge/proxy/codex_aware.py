"""Codex-aware translation layer.

Detects Codex CLI requests and provides Codex-specific enhancements:
  1. Preserves key behavioral instructions from Codex's system prompt
  2. Injects apply_patch format documentation
  3. Enhances tool definitions with parameter details
  4. Tunes compaction to be less aggressive for Codex workflows
  5. Incremental merge: appends Bridge supplements to the original prompt
     instead of extracting + rebuilding (preserves Codex's prompt structure)

Architecture (v2 — Incremental Merge):
  - detect_codex() is called early in the request pipeline
  - build_codex_system_prompt() now does INCREMENTAL MERGE:
    * Keeps the original Codex system prompt (compressed, not extracted)
    * Appends apply_patch format + CCG routing + tool calling as supplements
    * Preserves Codex's original instruction structure and priority
  - Non-Codex requests are completely unaffected (zero overhead)

Why this matters:
  Codex CLI sends a ~15KB system prompt with critical behavioral rules
  (how to use apply_patch, when to verify changes, conciseness rules, etc.).
  The v1 approach extracted behavioral lines and rebuilt the prompt from
  scratch, losing the original structure and priority. The v2 approach
  keeps the original prompt (compressed via noise stripping) and appends
  Bridge-specific supplements at the end.
"""

import re
from proxy.config import VERBOSE


# ---------------------------------------------------------------------------
# Codex detection markers
# ---------------------------------------------------------------------------

# Markers in the system prompt that indicate Codex CLI
_CODEX_SYSTEM_MARKERS = [
    "apply_patch",
    "container_exec",
    "coding agent",
    "You are an agent",
    "model_reasoning_effort",
]

# Tool names that are unique to Codex CLI
_CODEX_TOOL_NAMES = frozenset([
    "shell", "exec_command", "container_exec", "apply_patch",
    "read_file", "write_file",
])

# Codex-specific tool names that we should enhance
_CODEEX_ENHANCED_TOOLS = frozenset([
    "shell", "exec_command", "container_exec", "apply_patch",
    "read_file", "write_file",
])


# ---------------------------------------------------------------------------
# Codex behavioral instructions (compact, ~1.5KB)
# Extracted from Codex CLI source code (codex-rs/core/src/protocol.rs)
# These are the MOST CRITICAL rules that the model needs to follow
# ---------------------------------------------------------------------------

_CODEX_BEHAVIORAL_RULES = """## Codex Behavioral Rules
You are a coding agent operating in a CLI environment via a proxy bridge.

### File Changes
- Use apply_patch for ALL file modifications (create, edit, delete).
- For new files: use *** Add File: path header.
- For modifications: use *** Update File: path with @@ context, - remove, + add.
- For deletions: use *** Delete File: path.
- Always read a file before editing it to get exact context lines.

### Code Quality
- Never proactively add comments unless the user asks.
- Never add docstrings unless the user asks.
- Do not add trailing whitespace.
- Keep changes minimal and focused.
- Match existing code style and conventions.

### Verification
- After making changes, verify they compile/pass by running appropriate commands.
- If a change might break something, check before declaring done.
- Run tests when appropriate.

### Communication
- Be concise. Do not over-explain.
- State what you changed and why, briefly.
- If you encounter an error, explain it and propose a fix."""


# ---------------------------------------------------------------------------
# apply_patch format documentation (~800 bytes)
# Critical: without this, the model cannot produce valid patches
# ---------------------------------------------------------------------------

_APPLY_PATCH_FORMAT = """## apply_patch Format
Use apply_patch tool for file changes. The patch text uses this format:

*** Begin Patch
*** Add File: path/to/new/file.py
+import os
+def main():
+    pass
*** End Patch

*** Begin Patch
*** Update File: path/to/existing.py
 context_line_before
-removed_line
+added_line
 context_line_after
*** End Patch

*** Begin Patch
*** Delete File: path/to/unused.py
*** End Patch

Rules:
- Each + line is added, - line is removed, space-prefixed line is context.
- Include 1-3 context lines around changes for matching.
- For multiple files, use separate *** Begin/End Patch blocks.
- For move/rename: delete old file + add new file."""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_codex(messages: list, tools: list = None) -> tuple:
    """Detect if this request comes from Codex CLI.

    Args:
        messages: OpenAI messages array
        tools: OpenAI tools array (optional)

    Returns:
        (is_codex: bool, system_content: str)
        system_content is the extracted system prompt text (empty if not Codex).
    """
    # Collect system message content
    system_content = ""
    for msg in messages:
        if msg.get("role") == "system":
            from proxy.utils import _extract_text_content
            system_content += _extract_text_content(msg.get("content", ""))

    # Strategy 1: Check tool names for Codex-specific tools
    if tools:
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            func = tool.get("function", tool)
            name = func.get("name", "")
            if name in _CODEX_TOOL_NAMES:
                if VERBOSE:
                    print(f"[CatPawProxy] Codex detected: tool '{name}' found", flush=True)
                return True, system_content

    # Strategy 2: Check system prompt for Codex markers
    if system_content:
        for marker in _CODEX_SYSTEM_MARKERS:
            if marker in system_content:
                if VERBOSE:
                    print(f"[CatPawProxy] Codex detected: marker '{marker}' in system prompt ({len(system_content)} chars)", flush=True)
                return True, system_content

    return False, ""


# ---------------------------------------------------------------------------
# Codex instruction extraction
# ---------------------------------------------------------------------------

def _extract_section(text: str, start_marker: str, end_markers: list) -> str:
    """Extract a section from text between start_marker and the first end_marker."""
    start_idx = text.find(start_marker)
    if start_idx == -1:
        return ""
    start_idx += len(start_marker)

    end_idx = len(text)
    for em in end_markers:
        idx = text.find(em, start_idx)
        if idx != -1 and idx < end_idx:
            end_idx = idx

    return text[start_idx:end_idx].strip()


# Lines in Codex system prompt that are pure noise (environment info, etc.)
# and should be stripped to save space
_RE_CODEX_NOISE = re.compile(
    r'(<environment_context>.*?</environment_context>)|'
    r'(<repo_layout>.*?</repo_layout>)|'
    r'(<user_instructions>.*?</user_instructions>)',
    re.DOTALL
)

# Keep lines that start with these patterns (behavioral rules)
_BEHAVIORAL_LINE_PREFIXES = (
    "Never ", "Always ", "Do not ", "Don't ", "Use ",
    "If ", "When ", "After ", "Before ", "For ",
    "Verify ", "Run ", "Check ", "Be ", "Keep ", "Match ",
    "- ", "* ",
)


def compress_codex_system_prompt(system_content: str) -> str:
    """Compress Codex system prompt by stripping noise, keeping structure.

    v2 approach: Instead of extracting behavioral lines and rebuilding,
    we strip noise sections (environment, repo layout) and truncate to
    a budget. This preserves the original prompt's structure, priority,
    and formatting — the model sees the same instructions Codex intended,
    just smaller.

    Args:
        system_content: Full Codex system prompt text (~15KB)

    Returns:
        Compressed system prompt (~3-4KB), structure preserved
    """
    if not system_content or len(system_content) < 100:
        return ""

    # Strip noise sections (environment, repo layout, user instructions)
    cleaned = _RE_CODEX_NOISE.sub('', system_content)

    # Remove excessive blank lines (3+ consecutive → 2)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)

    # If small enough, keep as-is
    if len(cleaned) <= 4000:
        return cleaned.strip()

    # For larger prompts: keep the beginning (role + core rules) and end (tool rules)
    # The beginning has the role definition and core behavioral rules.
    # The end often has tool usage instructions.
    # The middle has verbose examples and edge cases we can trim.
    target_size = 3500
    head_size = int(target_size * 0.65)  # 65% from the beginning
    tail_size = target_size - head_size - 80  # 35% from the end, minus marker

    head = cleaned[:head_size]
    tail = cleaned[-tail_size:]

    # Try to cut at a line boundary to avoid breaking mid-sentence
    last_newline = head.rfind('\n')
    if last_newline > head_size * 0.5:
        head = head[:last_newline]

    first_newline = tail.find('\n')
    if first_newline != -1:
        tail = tail[first_newline + 1:]

    result = head + "\n\n... [middle section trimmed for brevity] ...\n\n" + tail
    return result.strip()


# Backward compatibility alias
extract_codex_instructions = compress_codex_system_prompt


# ---------------------------------------------------------------------------
# Codex-aware system prompt builder
# ---------------------------------------------------------------------------

def build_codex_system_prompt(codex_instructions: str, ccg_context: str = "",
                              lifecycle_context: str = "") -> str:
    """Build a Codex-aware system prompt using incremental merge.

    v2 Architecture — Incremental Merge:
      1. Bridge role header (compact, 3 lines)
      2. Original Codex system prompt (compressed, structure preserved)
         — OR default behavioral rules if no system prompt was provided
      3. apply_patch format documentation (Bridge supplement)
      4. CCG routing rules (if available, CLI-aware)
      5. CCG lifecycle guidance (phase-aware, from conversation analysis)
      6. Tool calling format (Bridge supplement)

    Total size: ~5-8KB (original prompt compressed + supplements)

    Args:
        codex_instructions: Compressed Codex system prompt (from compress_codex_system_prompt)
        ccg_context: CCG routing context string (empty if not available)
        lifecycle_context: Phase-aware CCG lifecycle guidance (empty if not applicable)

    Returns:
        Complete system prompt string
    """
    parts = [
        "You are a coding agent operating via a proxy bridge to CatPawAI.\n"
        "Follow the user's instructions carefully.\n"
        "Communicate in the user's language, keep technical terms in English.",
    ]

    # Original Codex system prompt (compressed, structure preserved)
    # This is the KEY change from v1: we keep the original prompt structure
    # rather than extracting and rebuilding from scratch.
    if codex_instructions:
        parts.append(codex_instructions)
    else:
        # No system prompt was provided — use default behavioral rules
        parts.append(_CODEX_BEHAVIORAL_RULES)

    # apply_patch format (critical for Codex — Bridge supplement)
    parts.append(_APPLY_PATCH_FORMAT)

    # CCG routing rules (if available, CLI-aware)
    if ccg_context:
        parts.append(ccg_context)

    # CCG lifecycle guidance (phase-aware, dynamic)
    if lifecycle_context:
        parts.append(lifecycle_context)

    # Tool calling format (always present — Bridge supplement)
    parts.append(_CODEX_TOOL_CALLING)

    return "\n\n".join(parts)


# Compact tool calling rules adapted for Codex tools
_CODEX_TOOL_CALLING = """## Tool Calling (CRITICAL)
When you need to use ANY tool (shell, exec_command, read_file, apply_patch,
Read, Write, Edit, Bash, etc.), output:

<tool_call>{"name":"ToolName","arguments":{"param":"value"}}</tool_call>

### Rules
- Output ONE tool call at a time, then WAIT for the result.
- Do NOT output multiple tool calls in one response.
- Do NOT describe what you will do — just call the tool directly.
- For read_file/Read: always read BEFORE editing.
- For apply_patch: use the exact format documented above.
- Results arrive as 'Tool Result: ...'

### Format Requirements (STRICT)
- ONLY use <tool_call> tags. Do NOT use any other format.
- Example: <tool_call>{"name":"shell","arguments":{"command":"ls -la"}}</tool_call>
- Example: <tool_call>{"name":"read_file","arguments":{"target_file":"./main.py"}}</tool_call>
- Example: <tool_call>{"name":"apply_patch","arguments":{"patch":"*** Begin Patch\\n*** Update File: main.py\\n ctx\\n-old\\n+new\\n*** End Patch"}}</tool_call>"""


# ---------------------------------------------------------------------------
# Enhanced tool definitions for Codex
# ---------------------------------------------------------------------------

def enhance_codex_tools_prompt(tools: list) -> str:
    """Build enhanced tool definitions for Codex CLI tools.

    Unlike the generic _inject_tools_prompt which compresses to single-line
    summaries, this preserves parameter types, descriptions, and enum values
    that are critical for Codex tools (especially apply_patch).

    Args:
        tools: OpenAI tools array

    Returns:
        Enhanced tool definitions text (~2-4KB)
    """
    if not tools:
        return ""

    lines = [
        "## Tools",
        'Output: <tool_call>{"name":"ToolName","arguments":{"param":"value"}}</tool_call>',
        "ONE tool call per response. Wait for result before continuing.",
        "",
    ]

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        func = tool.get("function", tool)
        name = func.get("name", "")
        if not name:
            continue

        # Get full description (not truncated for Codex tools)
        desc = func.get("description", "")
        # Truncate to 200 chars max (vs 120 for generic)
        if len(desc) > 200:
            desc = desc[:197] + "..."

        params = func.get("parameters", {})
        param_details = []
        if params and isinstance(params, dict):
            props = params.get("properties", {})
            required = params.get("required", [])
            for pname, pinfo in props.items():
                if not isinstance(pinfo, dict):
                    continue
                ptype = pinfo.get("type", "any")
                req = "required" if pname in required else "optional"
                pdesc = pinfo.get("description", "")
                # Truncate param description to 80 chars
                if len(pdesc) > 80:
                    pdesc = pdesc[:77] + "..."

                # Build parameter detail line
                detail = f"  - {pname} ({ptype}, {req})"
                if pdesc:
                    detail += f": {pdesc}"
                # Include enum values if present
                enum_vals = pinfo.get("enum")
                if enum_vals and isinstance(enum_vals, list):
                    detail += f" [values: {', '.join(str(v) for v in enum_vals[:5])}]"
                param_details.append(detail)

        # Format tool entry
        if param_details:
            lines.append(f"### {name}")
            if desc:
                lines.append(f"{desc}")
            lines.append("Parameters:")
            lines.extend(param_details)
            lines.append("")
        else:
            lines.append(f"- {name}: {desc}")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Codex-aware compaction configuration
# ---------------------------------------------------------------------------

class CodexCompactionConfig:
    """Compaction settings tuned for Codex CLI workflows.

    Codex workflows involve more file reading and patching than Claude Code.
    We increase retention for read_file/Read results and keep more recent
    turns to preserve context for multi-step edits.
    """

    # Tool-specific truncation for Codex (larger than defaults)
    TOOL_TRUNCATION = {
        "read_file":      {"head": 600, "tail": 300},   # vs 350+200 default
        "Read":           {"head": 600, "tail": 300},
        "shell":          {"head": 500, "tail": 100},   # vs 400+0 default
        "Bash":           {"head": 500, "tail": 100},
        "exec_command":   {"head": 500, "tail": 100},
        "container_exec": {"head": 500, "tail": 100},
        "apply_patch":    {"head": 99999, "tail": 0},   # keep as-is (usually small)
        "write_file":     {"head": 99999, "tail": 0},
        "Write":          {"head": 99999, "tail": 0},
        "Edit":           {"head": 99999, "tail": 0},
        "MultiEdit":      {"head": 99999, "tail": 0},
        "Grep":           {"head": 400, "tail": 100},   # vs 300+80 default
        "Glob":           {"head": 400, "tail": 100},
        "codebase_search": {"head": 400, "tail": 100},
        "WebFetch":       {"head": 500, "tail": 150},
        "WebSearch":      {"head": 500, "tail": 150},
        "list_dir":       {"head": 300, "tail": 0},
        "List":           {"head": 300, "tail": 0},
        "TodoWrite":      {"head": 99999, "tail": 0},
        "delete_file":    {"head": 99999, "tail": 0},
        "run_terminal_cmd": {"head": 500, "tail": 0},
    }

    # Keep more recent items for Codex
    RECENT_TOOL_RESULTS_KEEP = 5     # vs 3 default
    RECENT_TURNS_KEEP = 5            # vs 3 default

    # Less aggressive role summarization for Codex
    ROLE_SUMMARY_LEN = {
        "user": 300,       # vs 200 default — user intent is critical
        "assistant": 150,  # vs 100 default — what the model did
        "tool": 120,       # vs 80 default — tool name + result summary
        "system": 100,
    }

    # Larger hard truncate limit for Codex
    HARD_TRUNCATE_LIMIT = 3500  # vs 2500 default
