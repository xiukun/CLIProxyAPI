"""CCG Lifecycle Manager — Task tracking + quality gate orchestration.

This module provides:
  1. Detection of code changes from conversation history (apply_patch, Write, Edit)
  2. CCG task scaffolding (creates .ccg/tasks/ directory + task.json template)
  3. Phase-aware CCG guidance injection (analysis → implementation → review)
  4. Quality gate trigger detection (based on code change volume + risk)

Architecture:
  - analyze_conversation() scans tool calls in message history
  - get_ccg_lifecycle_context() produces a compact guidance string
  - ensure_ccg_scaffold() creates the .ccg/ directory structure in the project

Why this matters:
  Previously, the CCG hook (ccg-workflow.py) silently returned when .ccg/
  didn't exist, losing all task tracking. The Bridge now takes responsibility
  for:
  - Detecting what the model has done (from tool calls in history)
  - Injecting phase-appropriate guidance into the system prompt
  - Scaffolding the .ccg/ directory so the hook can function
"""

import json
import os
import re
from pathlib import Path
from datetime import datetime

from proxy.config import VERBOSE, CCG_ENABLED


# ---------------------------------------------------------------------------
# Code change detection from conversation history
# ---------------------------------------------------------------------------

# Tool names that indicate file modifications
_FILE_WRITE_TOOLS = frozenset([
    "apply_patch", "write_file", "Write", "Edit", "MultiEdit",
    "NotebookEdit", "delete_file",
])

# Tool names that indicate file reads (context gathering)
_FILE_READ_TOOLS = frozenset([
    "read_file", "Read", "Grep", "Glob", "codebase_search",
    "list_dir", "List", "LS",
])

# Tool names that indicate command execution
_COMMAND_TOOLS = frozenset([
    "shell", "Bash", "exec_command", "container_exec", "run_terminal_cmd",
])

# High-risk file patterns (auth, crypto, migration, etc.)
_HIGH_RISK_PATTERNS = [
    "auth", "login", "password", "token", "secret", "crypto",
    "encrypt", "migration", "schema", "permission", "admin",
]


def _extract_tool_calls_from_messages(messages: list) -> list:
    """Extract all tool calls from conversation history.

    Returns list of dicts: {name, arguments, role}
    Handles both structured tool_calls and <tool_call> text format.
    """
    from proxy.utils import _extract_text_content

    tool_calls = []

    for msg in messages:
        role = msg.get("role", "")

        # Structured tool_calls (OpenAI format)
        if role == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                func = tc.get("function", {})
                name = func.get("name", "")
                args_str = func.get("arguments", "{}")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except (json.JSONDecodeError, TypeError):
                    args = {}
                if name:
                    tool_calls.append({"name": name, "arguments": args, "role": role})

        # Text-based <tool_call> format (from GLM-5.2 responses)
        content = _extract_text_content(msg.get("content", ""))
        if content and "<tool_call>" in content:
            for match in re.finditer(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', content, re.DOTALL):
                try:
                    tc = json.loads(match.group(1))
                    name = tc.get("name", "")
                    args = tc.get("arguments", {})
                    if name:
                        tool_calls.append({"name": name, "arguments": args, "role": role})
                except json.JSONDecodeError:
                    continue

    return tool_calls


def _detect_changed_files(tool_calls: list) -> list:
    """Detect which files were modified from tool call history."""
    changed = []

    for tc in tool_calls:
        name = tc["name"]
        args = tc["arguments"]

        if name in ("apply_patch", "ApplyPatch"):
            patch = args.get("patch", "") if isinstance(args, dict) else ""
            # Extract file paths from patch format
            for m in re.finditer(r'\*\*\* (?:Add|Update|Delete) File: (.+)', patch):
                changed.append(m.group(1).strip())

        elif name in ("write_file", "Write"):
            path = args.get("file_path", "") or args.get("path", "") or args.get("target_file", "")
            if path:
                changed.append(path)

        elif name in ("Edit", "MultiEdit"):
            path = args.get("file_path", "") or args.get("target_file", "")
            if path:
                changed.append(path)

        elif name == "delete_file":
            path = args.get("file_path", "") or args.get("target_file", "")
            if path:
                changed.append(path)

    return list(set(changed))


def _detect_high_risk(changed_files: list) -> bool:
    """Check if any changed files match high-risk patterns."""
    for f in changed_files:
        lower = f.lower()
        if any(p in lower for p in _HIGH_RISK_PATTERNS):
            return True
    return False


def _detect_has_tests_run(tool_calls: list) -> bool:
    """Check if any test commands were run."""
    for tc in tool_calls:
        if tc["name"] in _COMMAND_TOOLS:
            cmd = ""
            args = tc["arguments"]
            if isinstance(args, dict):
                cmd = args.get("command", "") or args.get("cmd", "")
            if cmd and re.search(r'(go test|pytest|jest|npm test|cargo test|make test|gocheck)', cmd, re.IGNORECASE):
                return True
    return False


def _detect_phase(tool_calls: list, changed_files: list) -> str:
    """Detect the current CCG phase from conversation history.

    Phases: analysis → implementation → review
    - analysis: only reads/searches, no writes
    - implementation: has file writes, may not have run tests
    - review: has writes + test runs, or large changes
    """
    has_writes = any(tc["name"] in _FILE_WRITE_TOOLS for tc in tool_calls)
    has_reads = any(tc["name"] in _FILE_READ_TOOLS for tc in tool_calls)
    has_tests = _detect_has_tests_run(tool_calls)

    if not has_writes and (has_reads or tool_calls):
        return "analysis"

    if has_writes and not has_tests:
        return "implementation"

    if has_writes and has_tests:
        return "review"

    return "analysis"


# ---------------------------------------------------------------------------
# CCG scaffold creation
# ---------------------------------------------------------------------------

_CCG_DIR_STRUCTURE = [
    ".ccg",
    ".ccg/tasks",
    ".ccg/tasks/archive",
    ".ccg/spec",
    ".ccg/spec/backend",
    ".ccg/spec/frontend",
    ".ccg/spec/guides",
    ".ccg/research",
]

_TASK_TEMPLATE = {
    "id": "",
    "title": "",
    "status": "in_progress",
    "complexity": "M",
    "risk": "low",
    "currentPhase": "analysis",
    "nextAction": "",
    "createdAt": "",
    "files": [],
}


def ensure_ccg_scaffold(project_dir: str) -> bool:
    """Ensure .ccg/ directory structure exists in the project.

    Creates the directory tree if missing. Does NOT overwrite existing files.

    Args:
        project_dir: Absolute path to the project root

    Returns:
        True if scaffold was created (or already existed), False on error
    """
    if not CCG_ENABLED:
        return False
    if not project_dir:
        return False

    ccg_root = os.path.join(project_dir, ".ccg")
    if os.path.isdir(ccg_root):
        return True  # Already exists

    try:
        for subdir in _CCG_DIR_STRUCTURE:
            path = os.path.join(project_dir, subdir)
            os.makedirs(path, exist_ok=True)

        # Create .gitignore for .ccg/ to avoid committing task state
        gitignore = os.path.join(ccg_root, ".gitignore")
        if not os.path.exists(gitignore):
            with open(gitignore, "w") as f:
                f.write("# CCG task state - do not commit\n")
                f.write("tasks/\n")
                f.write("research/\n")
                f.write("*.tmp\n")

        if VERBOSE:
            print(f"[CatPawProxy] CCG scaffold created at {ccg_root}", flush=True)
        return True
    except Exception as e:
        if VERBOSE:
            print(f"[CatPawProxy] CCG scaffold creation failed: {e}", flush=True)
        return False


def create_ccg_task(project_dir: str, title: str, complexity: str = "M") -> str:
    """Create a new CCG task in .ccg/tasks/.

    Args:
        project_dir: Project root path
        title: Task title (from user's first message)
        complexity: S/M/L/XL

    Returns:
        Task directory path, or empty string on failure
    """
    if not ensure_ccg_scaffold(project_dir):
        return ""

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # Slugify title
    slug = re.sub(r'[^a-zA-Z0-9]+', '-', title.lower())[:40].strip('-')
    if not slug:
        slug = "task"

    task_name = f"{timestamp}-{slug}"
    task_dir = os.path.join(project_dir, ".ccg", "tasks", task_name)

    try:
        os.makedirs(task_dir, exist_ok=True)
        task = dict(_TASK_TEMPLATE)
        task["id"] = task_name
        task["title"] = title[:200]
        task["complexity"] = complexity
        task["createdAt"] = datetime.now().isoformat()
        task["currentPhase"] = "analysis"
        task["nextAction"] = "Analyze requirements and read relevant files"

        task_file = os.path.join(task_dir, "task.json")
        with open(task_file, "w") as f:
            json.dump(task, f, indent=2, ensure_ascii=False)

        if VERBOSE:
            print(f"[CatPawProxy] CCG task created: {task_dir}", flush=True)
        return task_dir
    except Exception as e:
        if VERBOSE:
            print(f"[CatPawProxy] CCG task creation failed: {e}", flush=True)
        return ""


# ---------------------------------------------------------------------------
# Phase-aware CCG guidance
# ---------------------------------------------------------------------------

_PHASE_GUIDANCE = {
    "analysis": """## CCG Phase: Analysis
You are in the ANALYSIS phase. Before writing any code:
1. Read relevant source files to understand the current architecture
2. Check CCG routing rules for domain-specific expertise
3. Identify all files that will need to change
4. For M/L/XL complexity: consider calling external models for parallel analysis
When ready to code, state "Starting implementation" and begin.""",

    "implementation": """## CCG Phase: Implementation
You are in the IMPLEMENTATION phase. Code is being written.
1. Follow the plan from your analysis
2. Keep changes minimal and focused
3. Run tests after significant changes (>10 lines)
4. After all changes, transition to review phase
When done coding, run tests and state "Starting review".""",

    "review": """## CCG Phase: Review
You are in the REVIEW phase. Code has been written and tests run.
1. Review your changes for correctness and edge cases
2. For changes >30 lines or high-risk files: external review is recommended
3. Check code quality: no trailing whitespace, match existing style
4. Verify no security vulnerabilities introduced
5. Archive the task when complete""",
}


def get_ccg_lifecycle_context(messages: list, is_codex: bool = False, is_claude_code: bool = False) -> str:
    """Build phase-aware CCG guidance based on conversation history.

    This replaces the static CCG routing injection with dynamic, context-aware
    guidance that adapts to what the model has actually done.

    Args:
        messages: Conversation messages (for tool call analysis)
        is_codex: Whether this is a Codex CLI request
        is_claude_code: Whether this is a Claude Code request

    Returns:
        Compact CCG lifecycle guidance string (~500-1500 chars)
    """
    if not CCG_ENABLED:
        return ""
    tool_calls = _extract_tool_calls_from_messages(messages)
    changed_files = _detect_changed_files(tool_calls)
    phase = _detect_phase(tool_calls, changed_files)
    has_high_risk = _detect_high_risk(changed_files)
    has_tests = _detect_has_tests_run(tool_calls)
    change_count = len(changed_files)

    parts = []

    # Phase guidance
    phase_text = _PHASE_GUIDANCE.get(phase, "")
    if phase_text:
        parts.append(phase_text)

    # Change summary
    if change_count > 0:
        files_str = ", ".join(changed_files[:5])
        if change_count > 5:
            files_str += f" (+{change_count - 5} more)"
        parts.append(f"Changed files ({change_count}): {files_str}")

    # Quality gate triggers
    quality_gates = []
    if change_count > 0 and not has_tests and phase == "implementation":
        quality_gates.append("Reminder: run tests after significant changes")
    if change_count > 0 and has_high_risk:
        quality_gates.append("High-risk files detected: security review recommended before delivery")
    if change_count > 5:
        quality_gates.append(f"Large change set ({change_count} files): consider splitting into smaller PRs")

    if quality_gates:
        parts.append("Quality gates:\n" + "\n".join(f"- {g}" for g in quality_gates))

    # Task tracking reminder (only if this looks like a new task)
    if not tool_calls and len(messages) <= 2:
        parts.append("Tip: Track this task with TodoWrite or create a .ccg/tasks/ entry for complex work.")

    return "\n\n".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# Unified skill path resolution
# ---------------------------------------------------------------------------

def get_skill_base_path(is_codex: bool = False, is_claude_code: bool = False) -> str:
    """Get the correct skill file base path for the current CLI.

    Codex CLI uses ~/.codex/skills/ (or ~/.claude/skills/ccg/ as fallback)
    Claude Code uses ~/.claude/skills/ccg/

    Args:
        is_codex: Whether this is a Codex CLI request
        is_claude_code: Whether this is a Claude Code request

    Returns:
        Base path string for skill files
    """
    if is_codex:
        # Codex CLI: check if CCG skills are installed in ~/.codex/skills/
        codex_ccg = os.path.expanduser("~/.codex/skills/ccg")
        if os.path.isdir(codex_ccg):
            return codex_ccg
        # Fallback: use ~/.claude/skills/ccg/ (shared installation)
        return os.path.expanduser("~/.claude/skills/ccg")
    elif is_claude_code:
        return os.path.expanduser("~/.claude/skills/ccg")
    else:
        return os.path.expanduser("~/.claude/skills/ccg")


def get_tool_name_for_read(is_codex: bool = False) -> str:
    """Get the correct tool name for reading files in the current CLI."""
    return "read_file" if is_codex else "Read"


def get_tool_name_for_write(is_codex: bool = False) -> str:
    """Get the correct tool name for writing files in the current CLI."""
    return "apply_patch" if is_codex else "Write"
