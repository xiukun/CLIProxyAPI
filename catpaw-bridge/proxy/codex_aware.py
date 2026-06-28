"""Codex-aware translation layer.

Detects Codex CLI requests and provides Codex-specific enhancements:
  1. Preserves key behavioral instructions from Codex's system prompt
  2. Injects apply_patch format documentation
  3. Enhances tool definitions with parameter details
  4. Tunes compaction to be less aggressive for Codex workflows

Architecture:
  - detect_codex() is called early in the request pipeline
  - The CodexContext is passed through to translator/toolcall/compactor
  - Non-Codex requests are completely unaffected (zero overhead)

Why this matters:
  Codex CLI sends a ~15KB system prompt with critical behavioral rules
  (how to use apply_patch, when to verify changes, conciseness rules, etc.)
  The bridge previously DROPPED this entirely, replacing it with a generic
  4KB prompt. This caused the model to lose Codex-specific behaviors,
  resulting in failed patches, over-verbose responses, and missing
  verification steps.
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


def extract_codex_instructions(system_content: str) -> str:
    """Extract key behavioral instructions from Codex system prompt.

    The Codex system prompt is ~15KB. We extract behavioral rules and
    compress them to ~2KB, focusing on the rules that affect code quality.

    Args:
        system_content: Full Codex system prompt text

    Returns:
        Compact behavioral instructions (~2KB)
    """
    if not system_content or len(system_content) < 100:
        return ""

    # Strip noise sections (environment, repo layout, user instructions)
    # These are dynamic and not useful as behavioral rules
    cleaned = _RE_CODEX_NOISE.sub('', system_content)

    # If the prompt is already small enough (< 4KB), keep it as-is
    if len(cleaned) < 4000:
        return cleaned.strip()

    # For larger prompts, extract behavioral lines
    lines = cleaned.split('\n')
    kept_lines = []
    in_section = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_section and kept_lines and kept_lines[-1]:
                kept_lines.append("")  # preserve paragraph breaks
            continue

        # Keep section headers (## or ###)
        if stripped.startswith('#'):
            kept_lines.append(stripped)
            in_section = True
            continue

        # Keep behavioral rule lines
        if stripped.startswith(_BEHAVIORAL_LINE_PREFIXES):
            kept_lines.append(stripped)
            in_section = True
            continue

        # Keep lines that look like instructions (imperative mood)
        if re.match(r'^[A-Z][a-z]+ ', stripped) and len(stripped) < 200:
            kept_lines.append(stripped)
            in_section = True
            continue

        in_section = False

    # Clean up trailing empty lines
    while kept_lines and not kept_lines[-1]:
        kept_lines.pop()

    result = '\n'.join(kept_lines)

    # Cap at 3KB to leave room for other prompt components
    if len(result) > 3000:
        result = result[:2950] + "\n... [additional rules truncated]"

    return result


# ---------------------------------------------------------------------------
# Codex-aware system prompt builder
# ---------------------------------------------------------------------------

def build_codex_system_prompt(codex_instructions: str, ccg_context: str = "") -> str:
    """Build a Codex-aware system prompt.

    Structure:
      1. Bridge role description
      2. Codex behavioral rules (from extracted instructions)
      3. apply_patch format documentation
      4. CCG routing rules (if available)
      5. Tool calling format

    Total size: ~4-6KB (vs 4KB for non-Codex prompt)

    Args:
        codex_instructions: Extracted Codex behavioral instructions
        ccg_context: CCG routing context string (empty if not available)

    Returns:
        Complete system prompt string
    """
    parts = [
        "You are a coding agent operating via a proxy bridge to CatPawAI.\n"
        "Follow the user's instructions carefully.\n"
        "Communicate in the user's language, keep technical terms in English.",
    ]

    # Codex behavioral rules (extracted from original system prompt)
    if codex_instructions:
        parts.append("## Extracted Instructions\n" + codex_instructions)
    else:
        # No system prompt was provided — use default behavioral rules
        parts.append(_CODEX_BEHAVIORAL_RULES)

    # apply_patch format (critical for Codex)
    parts.append(_APPLY_PATCH_FORMAT)

    # CCG routing rules (if available)
    if ccg_context:
        parts.append(ccg_context)

    # Tool calling format (always present)
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
