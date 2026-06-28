"""CCG Context Loader — CCG skill routing + project context for system prompt.

This module loads CCG (Code Craft Guide) routing rules at startup and
builds a compact, stable system prompt that includes:
  1. Role description + initial setup (read CLAUDE.md / AGENTS.md first)
  2. CCG skill routing rules (domain expertise auto-detection)
  3. CCG quality gates (auto-trigger after code changes)
  4. Tool calling rules (format + strict requirements)

Cache strategy (高缓存):
  - CCG rules loaded ONCE at module import (files rarely change)
  - Full system prompt built ONCE at module import and cached
  - No file I/O on repeated requests — zero per-request overhead
  - Stable content enables effective memory/compaction in the Bridge

The generated prompt is ~4KB — compact enough to leave room for
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
# Compact CCG routing rules
# Extracted from ~/.claude/rules/ccg-skill-routing.md and ccg-skills.md
# Kept compact (~2KB) to leave room for conversation history
# ---------------------------------------------------------------------------
_CCG_ROUTING_COMPACT = """## CCG Skill Routing

When the user's request matches trigger keywords, READ the corresponding skill
file at ~/.claude/skills/ccg/domains/ BEFORE responding. Do NOT fabricate
domain knowledge when a skill file exists.

### Domain Expertise Routing
| Keywords | Skill File |
|----------|-----------|
| pentest, red team, exploit, OWASP, SQLi, XSS, SSRF | domains/security/pentest.md |
| blue team, incident response, forensics, SIEM, EDR | domains/security/blue-team.md |
| code audit, taint analysis, dangerous function | domains/security/code-audit.md |
| API design, REST, GraphQL, gRPC, versioning | domains/architecture/api-design.md |
| caching, Redis, Memcached, CDN, invalidation | domains/architecture/caching.md |
| Kubernetes, Docker, microservice, service mesh | domains/architecture/cloud-native.md |
| Kafka, RabbitMQ, event driven, pub/sub | domains/architecture/message-queue.md |
| RAG, vector database, embedding, chunking | domains/ai/rag-system.md |
| AI agent, tool use, function calling, orchestration | domains/ai/agent-dev.md |
| LLM security, prompt injection, jailbreak, guardrail | domains/ai/llm-security.md |
| prompt engineering, model evaluation, fine-tuning | domains/ai/prompt-and-eval.md |
| Git workflow, branching, trunk-based, GitFlow | domains/devops/git-workflow.md |
| testing, unit test, integration, e2e, test pyramid | domains/devops/testing.md |
| database, migration, schema, indexing, query opt | domains/devops/database.md |
| performance, profiling, load test, latency | domains/devops/performance.md |
| observability, logging, tracing, metrics, Grafana | domains/devops/observability.md |

### Language-Specific Routing
Auto-detect from file extensions, then read the matching skill:
- Python (.py) → domains/development/python.md
- Go (.go) → domains/development/go.md
- Rust (.rs) → domains/development/rust.md
- TypeScript/JavaScript (.ts/.js) → domains/development/typescript.md
- Java/Kotlin (.java/.kt) → domains/development/java.md
- C/C++ (.c/.cpp/.h) → domains/development/cpp.md
- Shell/Bash (.sh/.bash) → domains/development/shell.md

### Quality Gates (auto-trigger after code changes)
- New module created → gen-docs → verify-module → verify-security
- Code changes > 30 lines → verify-change → verify-quality
- Security-related changes → verify-security
- Refactoring → verify-change → verify-quality → verify-security
- Quality gates are non-blocking unless Critical/High severity found

### Routing Rules
1. Keyword match is fuzzy — match on intent, not exact string
2. Multiple matches → read both skill files
3. Auto-detect language from file extensions or context
4. Read once per conversation — no need to re-read same skill
5. Skill files are authoritative over training data
6. Quality gates chain: skip if previous gate fails"""


# ---------------------------------------------------------------------------
# Initial setup instructions (stable, never changes)
# Instructs the model to read project context files FIRST
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
    """Get the CCG routing context string for system prompt injection.

    Returns empty string if CCG is not available.
    The result is cached at module load time — no per-request overhead.
    """
    if not _CCG_AVAILABLE:
        return ""
    return _CCG_ROUTING_COMPACT


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
