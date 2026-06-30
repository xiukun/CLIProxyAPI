"""CCG Context Loader — CCG skill routing + project context for system prompt.

This module loads CCG (Code Craft Guide) routing rules at startup and
provides context-aware system prompt injection.

Architecture (v2 — Bridge Native CCG Orchestration):
  1. Static CCG routing rules loaded ONCE at module import (for non-CLI mode)
  2. Dynamic CCG routing built per-request via get_ccg_routing_for_cli()
     — detects Codex vs Claude Code
     — uses correct tool names (read_file vs Read)
     — uses correct skill paths (~/.codex/skills/ccg vs ~/.claude/skills/ccg)
  3. Phase-aware lifecycle guidance via ccg_lifecycle.get_ccg_lifecycle_context()

Cache strategy (高缓存):
  - Static rules loaded ONCE at module import
  - Dynamic routing context built per-request (lightweight, ~100 lines of string ops)
  - No file I/O on repeated requests — zero per-request overhead for static parts

The generated prompt is ~2-4KB — compact enough to leave room for
conversation history within the upstream body size limit.
"""

import os
from pathlib import Path

from proxy.config import VERBOSE, CCG_ENABLED


# ---------------------------------------------------------------------------
# CCG rules directory (Claude Code installation path)
# ---------------------------------------------------------------------------
_CCG_RULES_DIR = Path.home() / ".claude" / "rules"


def _check_ccg_available() -> bool:
    """Check if CCG routing rules are installed and enabled."""
    if not CCG_ENABLED:
        return False
    return (_CCG_RULES_DIR / "ccg-skill-routing.md").exists()


# Cache the availability check at module load time
_CCG_AVAILABLE = _check_ccg_available()

if VERBOSE and _CCG_AVAILABLE:
    print("[CatPawProxy] CCG routing rules detected — enabling CCG injection", flush=True)
elif VERBOSE:
    print("[CatPawProxy] CCG routing rules NOT found — CCG injection disabled", flush=True)


# ---------------------------------------------------------------------------
# CLI-aware CCG routing rules
# Built dynamically based on detected CLI (Codex vs Claude Code)
# ---------------------------------------------------------------------------

# Skill domain routing table (shared between Codex and Claude Code)
_DOMAIN_ROUTES = [
    ("pentest, red team, exploit, OWASP, SQLi, XSS, SSRF", "security/pentest.md"),
    ("blue team, incident response, forensics, SIEM, EDR", "security/blue-team.md"),
    ("code audit, taint analysis, dangerous function", "security/code-audit.md"),
    ("API design, REST, GraphQL, gRPC, versioning", "architecture/api-design.md"),
    ("caching, Redis, Memcached, CDN, invalidation", "architecture/caching.md"),
    ("Kubernetes, Docker, microservice, service mesh", "architecture/cloud-native.md"),
    ("Kafka, RabbitMQ, event driven, pub/sub", "architecture/message-queue.md"),
    ("RAG, vector database, embedding, chunking", "ai/rag-system.md"),
    ("AI agent, tool use, function calling, orchestration", "ai/agent-dev.md"),
    ("LLM security, prompt injection, jailbreak, guardrail", "ai/llm-security.md"),
    ("prompt engineering, model evaluation, fine-tuning", "ai/prompt-and-eval.md"),
    ("Git workflow, branching, trunk-based, GitFlow", "devops/git-workflow.md"),
    ("testing, unit test, integration, e2e, test pyramid", "devops/testing.md"),
    ("database, migration, schema, indexing, query opt", "devops/database.md"),
    ("performance, profiling, load test, latency", "devops/performance.md"),
    ("observability, logging, tracing, metrics, Grafana", "devops/observability.md"),
]

_LANGUAGE_ROUTES = [
    ("Python", ".py", "development/python.md"),
    ("Go", ".go", "development/go.md"),
    ("Rust", ".rs", "development/rust.md"),
    ("TypeScript/JavaScript", ".ts/.js", "development/typescript.md"),
    ("Java/Kotlin", ".java/.kt", "development/java.md"),
    ("C/C++", ".c/.cpp/.h", "development/cpp.md"),
    ("Shell/Bash", ".sh/.bash", "development/shell.md"),
]


def _build_ccg_routing_for_cli(is_codex: bool = False, is_claude_code: bool = False) -> str:
    """Build CCG routing context adapted for the detected CLI.

    Key differences from the old static version:
    1. Uses the correct read tool name (read_file for Codex, Read for Claude Code)
    2. Uses the correct skill base path (~/.codex/skills/ccg or ~/.claude/skills/ccg)
    3. Quality gates use the correct write tool name

    Args:
        is_codex: Whether this is a Codex CLI request
        is_claude_code: Whether this is a Claude Code request

    Returns:
        CCG routing context string (~1.5-2KB)
    """
    if not _CCG_AVAILABLE:
        return ""

    # Determine tool names and skill paths
    if is_codex:
        read_tool = "read_file"
        patch_tool = "apply_patch"
        # Codex can read from ~/.claude/skills/ccg/ via read_file (absolute paths work)
        skill_base = "~/.claude/skills/ccg/domains"
        tools_base = "~/.claude/skills/ccg/tools"
    elif is_claude_code:
        read_tool = "Read"
        patch_tool = "Write"
        skill_base = "~/.claude/skills/ccg/domains"
        tools_base = "~/.claude/skills/ccg/tools"
    else:
        read_tool = "read_file"
        patch_tool = "apply_patch"
        skill_base = "~/.claude/skills/ccg/domains"
        tools_base = "~/.claude/skills/ccg/tools"

    lines = [
        "## CCG Skill Routing",
        "",
        f"When the user's request matches trigger keywords, use {read_tool} to READ the",
        f"corresponding skill file at {skill_base}/ BEFORE responding.",
        "Do NOT fabricate domain knowledge when a skill file exists.",
        "",
        "### Domain Expertise Routing",
        "| Keywords | Skill File |",
        "|----------|-----------|",
    ]

    for keywords, skill_file in _DOMAIN_ROUTES:
        lines.append(f"| {keywords} | {skill_base}/{skill_file} |")

    lines.extend([
        "",
        "### Language-Specific Routing",
        "Auto-detect from file extensions, then read the matching skill:",
    ])
    for lang, ext, skill_file in _LANGUAGE_ROUTES:
        lines.append(f"- {lang} ({ext}) → {skill_base}/{skill_file}")

    lines.extend([
        "",
        "### Quality Gates (auto-trigger after code changes)",
        f"Quality gates are skill FILES you READ with {read_tool}, NOT Skill tool calls.",
        f"- New module created → Read {tools_base}/gen-docs/SKILL.md → {tools_base}/verify-module/SKILL.md → {tools_base}/verify-security/SKILL.md",
        f"- Code changes > 30 lines → Read {tools_base}/verify-change/SKILL.md → {tools_base}/verify-quality/SKILL.md",
        f"- Security-related changes → Read {tools_base}/verify-security/SKILL.md",
        f"- Refactoring → Read {tools_base}/verify-change/SKILL.md → {tools_base}/verify-quality/SKILL.md → {tools_base}/verify-security/SKILL.md",
        f"- Quality gates are non-blocking unless Critical/High severity found",
        f"- IMPORTANT: Do NOT use the Skill tool for quality gates. Use {read_tool} to read the SKILL.md file.",
        "",
        "### Routing Rules",
        "1. Keyword match is fuzzy — match on intent, not exact string",
        "2. Multiple matches → read both skill files",
        "3. Auto-detect language from file extensions or context",
        "4. Read once per conversation — no need to re-read same skill",
        "5. Skill files are authoritative over training data",
        "6. Quality gates chain: skip if previous gate fails",
    ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Static CCG routing (for non-CLI mode — backward compatible)
# ---------------------------------------------------------------------------

_CCG_ROUTING_COMPACT = _build_ccg_routing_for_cli(is_codex=False, is_claude_code=False)


# ---------------------------------------------------------------------------
# Initial setup instructions (stable, never changes)
# ---------------------------------------------------------------------------

_INITIAL_SETUP = """## Initial Setup (FIRST ACTION)
When starting a new task, FIRST read project context files before responding:
1. Read CLAUDE.md (or claude.md) in the project root — project rules & conventions
2. Read AGENTS.md in the project root — build commands & architecture overview
3. Check CCG routing rules below for domain expertise routing
Then proceed with the user's request."""


# ---------------------------------------------------------------------------
# Tool calling rules (stable, never changes)
# Adapted for BOTH Claude Code (Read/Write/Edit/Bash) and Codex CLI (shell/exec_command/read_file)
# ---------------------------------------------------------------------------

_TOOL_CALLING = """## Tool Calling (CRITICAL)
When you need to use ANY tool (Read, Write, Edit, Bash, shell, exec_command,
read_file, apply_patch, etc.), output:

<tool_call>{"name":"ToolName","arguments":{"param":"value"}}</tool_call>

### Rules
- Output ONE tool call at a time, then WAIT for the result before continuing.
- Do NOT output multiple tool calls in one response.
- Do NOT describe what you will do — just call the tool directly.
- Do NOT ask for confirmation before writing/editing files.
- For Read: always Read the file BEFORE editing it.
- For Edit: use the EXACT text from the Read result as old_string.
- For Write: provide the COMPLETE file content, not just a fragment.
- Results arrive as 'Tool Result: ...'

### Format Requirements (STRICT)
- ONLY use <tool_call> tags. Do NOT use any other format.
- NO: ToolName<parameters>{"key":"value"}</parameters>
- NO: ToolName(param="value")
- NO: ```json blocks with tool calls
- NO: bare JSON without <tool_call> tags
- Example: <tool_call>{"name":"shell","arguments":{"command":"ls -la"}}</tool_call>
- Example: <tool_call>{"name":"Read","arguments":{"file_path":"./CLAUDE.md"}}</tool_call>"""


def get_ccg_routing_context() -> str:
    """Get the static CCG routing context string for system prompt injection.

    Returns empty string if CCG is not available.
    The result is cached at module load time — no per-request overhead.
    """
    if not _CCG_AVAILABLE:
        return ""
    return _CCG_ROUTING_COMPACT


def get_ccg_routing_for_cli(is_codex: bool = False, is_claude_code: bool = False) -> str:
    """Get CLI-aware CCG routing context.

    This is the v2 version that adapts tool names and skill paths
    based on the detected CLI (Codex vs Claude Code).

    Args:
        is_codex: Whether this is a Codex CLI request
        is_claude_code: Whether this is a Claude Code request

    Returns:
        CCG routing context string adapted for the CLI, or empty string
    """
    if not _CCG_AVAILABLE:
        return ""
    return _build_ccg_routing_for_cli(is_codex=is_codex, is_claude_code=is_claude_code)


def build_system_prompt() -> str:
    """Build the full enhanced system prompt with CCG context.

    Structure (stable prefix for cache efficiency):
      1. Role description
      2. Initial setup instructions (read CLAUDE.md, AGENTS.md first)
      3. CCG routing rules (if available)
      4. Tool calling rules + format requirements

    The entire prompt is built ONCE at module import and cached.
    Same content every time → effective memory/compaction.

    Returns:
        str: Enhanced system prompt (~4KB)
    """
    parts = [
        "You are an AI coding assistant with CCG (Code Craft Guide) workflow integration.\n"
        "Follow the user's instructions carefully.\n"
        "Communicate in the user's language, keep technical terms in English.",
        "",
        _INITIAL_SETUP,
    ]

    # CCG routing rules (conditional — only if installed)
    ccg_context = get_ccg_routing_context()
    if ccg_context:
        parts.append(ccg_context)

    # Tool calling + format requirements (always present)
    parts.append(_TOOL_CALLING)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Build and cache the system prompt ONCE at module import time
# This is the "高缓存" core — zero per-request overhead
# ---------------------------------------------------------------------------
CUSTOM_SYSTEM_PROMPT = build_system_prompt()

if VERBOSE:
    print(f"[CatPawProxy] System prompt built: {len(CUSTOM_SYSTEM_PROMPT)} chars "
          f"({'with CCG' if _CCG_AVAILABLE else 'without CCG'})", flush=True)
