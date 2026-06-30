"""Workspace Context Extractor — preserves monorepo structure awareness.

Problem:
  In long-running agent sessions within a monorepo, the model creates files in
  the wrong package/directory. This happens because:
  1. The bridge strips <environment_context>/<repo_layout> as "noise"
  2. After compaction, even residual workspace info is lost
  3. The model has no persistent anchor telling it which package it's in

Solution:
  Instead of completely stripping environment/repo context, EXTRACT and COMPRESS
  the essential workspace info (cwd, top-level package structure) into a compact
  "workspace anchor" that survives compaction by living in the system prompt.

How Codex handles this:
  Codex's system prompt explicitly states: "The task involves working with Git
  repositories in your current working directory." It includes <environment_context>
  with the cwd and <repo_layout> with the project structure. Codex's sandbox
  enforces file access at the OS level — the model literally can't write outside
  the sandbox. We replicate this awareness at the prompt level.
"""

import re
import os
from proxy.config import VERBOSE


# ---------------------------------------------------------------------------
# Extraction patterns for environment/repo context from system prompts
# ---------------------------------------------------------------------------

# Codex CLI: <environment_context>...</environment_context>
_RE_CODEX_ENV = re.compile(
    r'<environment_context>(.*?)</environment_context>',
    re.DOTALL
)

# Codex CLI: <repo_layout>...</repo_layout>
_RE_CODEX_REPO = re.compile(
    r'<repo_layout>(.*?)</repo_layout>',
    re.DOTALL
)

# Claude Code: <environment>...</environment>
_RE_CLAUDE_ENV = re.compile(
    r'<environment>(.*?)</environment>',
    re.DOTALL
)

# Claude Code: <workspace_info>...</workspace_info>
_RE_CLAUDE_WORKSPACE = re.compile(
    r'<workspace_info>(.*?)</workspace_info>',
    re.DOTALL
)

# Generic: extract cwd-like paths from text
_RE_CWD_PATTERN = re.compile(
    r'(?:cwd|working.?dir|current.?dir|project.?dir|root)[:\s]+([^\n<]+)',
    re.IGNORECASE
)

# Generic: extract directory listing patterns (indented tree structure)
_RE_DIR_TREE = re.compile(
    r'^([A-Za-z0-9_\-./]+/)\s*$',
    re.MULTILINE
)


def extract_workspace_context(system_content: str, is_codex: bool = False,
                               is_claude_code: bool = False) -> str:
    """Extract workspace context from a CLI system prompt.

    Instead of completely stripping environment/repo context, this extracts
    the essential info (cwd, package structure) and returns a compact string.

    Args:
        system_content: Full system prompt text from Codex or Claude Code
        is_codex: Whether this is a Codex system prompt
        is_claude_code: Whether this is a Claude Code system prompt

    Returns:
        Compact workspace context string (~200-500 chars), or empty string
        if no workspace info was found.
    """
    if not system_content:
        return ""

    parts = []

    if is_codex:
        # Extract <environment_context>
        env_match = _RE_CODEX_ENV.search(system_content)
        if env_match:
            env_text = env_match.group(1).strip()
            # Compress: keep cwd, platform, shell info (first ~200 chars)
            if len(env_text) > 300:
                env_text = env_text[:297] + "..."
            parts.append(f"Environment: {env_text}")

        # Extract <repo_layout> — this is critical for monorepo awareness
        repo_match = _RE_CODEX_REPO.search(system_content)
        if repo_match:
            repo_text = repo_match.group(1).strip()
            # Compress: keep top-level directory structure (first ~400 chars)
            # This tells the model which packages exist in the monorepo
            if len(repo_text) > 500:
                repo_text = repo_text[:497] + "..."
            parts.append(f"Repo Layout: {repo_text}")

    elif is_claude_code:
        # Extract <environment>
        env_match = _RE_CLAUDE_ENV.search(system_content)
        if env_match:
            env_text = env_match.group(1).strip()
            if len(env_text) > 300:
                env_text = env_text[:297] + "..."
            parts.append(f"Environment: {env_text}")

        # Extract <workspace_info>
        ws_match = _RE_CLAUDE_WORKSPACE.search(system_content)
        if ws_match:
            ws_text = ws_match.group(1).strip()
            if len(ws_text) > 500:
                ws_text = ws_text[:497] + "..."
            parts.append(f"Workspace: {ws_text}")

    # Fallback: try to extract cwd from any text
    if not parts:
        cwd_match = _RE_CWD_PATTERN.search(system_content)
        if cwd_match:
            parts.append(f"Working Directory: {cwd_match.group(1).strip()}")

    # Also try to get cwd from environment as a fallback
    if not parts:
        try:
            cwd = os.environ.get("CODEX_PROJECT_DIR", os.getcwd())
            parts.append(f"Working Directory: {cwd}")
        except Exception:
            pass

    return "\n".join(parts) if parts else ""


def build_workspace_anchor(workspace_context: str) -> str:
    """Build a compact workspace anchor string for system prompt injection.

    This is injected into the system prompt (not conversation history) so it
    survives compaction. It tells the model:
    1. Which directory it's working in
    2. The monorepo structure (if available)
    3. Rules for file creation to prevent wrong-package errors

    Args:
        workspace_context: Extracted workspace context from extract_workspace_context()

    Returns:
        Compact workspace anchor string (~300-600 chars)
    """
    if not workspace_context:
        # Still inject the rules even without specific workspace info
        return _WORKSPACE_BOUNDARY_RULES

    return f"""## Workspace Context (CRITICAL — Do Not Forget)
{workspace_context}

{_WORKSPACE_BOUNDARY_RULES}"""


# ---------------------------------------------------------------------------
# Workspace boundary rules — prevents wrong-package file creation
# ---------------------------------------------------------------------------

_WORKSPACE_BOUNDARY_RULES = """### Workspace Boundary Rules (Monorepo Safety)
- You are working in a MONOREPO. Files must be created in the CORRECT package.
- Before creating a new file, verify the target directory exists and matches
  the package you're working in.
- Use `list_dir` or `ls` to verify the directory structure before writing.
- ALWAYS use paths relative to the working directory shown above.
- If unsure which package a file belongs to, check neighboring files for
  import paths, package.json, go.mod, or similar module markers.
- NEVER create files in a sibling package unless explicitly asked.
- When using apply_patch, verify the file path starts with the correct
  package prefix before submitting the patch.
- If you see "File not found" after creating a file, you likely wrote to
  the wrong directory — check your path immediately."""


# ---------------------------------------------------------------------------
# File path extraction from tool calls (for validation/logging)
# ---------------------------------------------------------------------------

# Tool names that create/modify files
_FILE_WRITE_TOOLS = frozenset([
    "Write", "write_file", "create_file",
    "Edit", "MultiEdit", "edit_file",
    "apply_patch", "NotebookEdit",
])

# Patterns to extract file paths from tool call arguments
_RE_PATCH_FILE_PATH = re.compile(
    r'\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(.+)',
    re.MULTILINE
)


def extract_file_paths_from_tool_call(tool_name: str, arguments: dict) -> list:
    """Extract file paths from a tool call's arguments.

    Useful for logging and validation. Returns a list of file paths
    referenced in the tool call.

    Args:
        tool_name: Name of the tool (Write, Edit, apply_patch, etc.)
        arguments: Tool call arguments dict

    Returns:
        List of file path strings found in the arguments
    """
    paths = []

    if tool_name not in _FILE_WRITE_TOOLS:
        return paths

    # Direct file_path parameter (Write, Edit, etc.)
    for key in ("file_path", "target_file", "path", "filepath"):
        val = arguments.get(key)
        if val and isinstance(val, str):
            paths.append(val)

    # apply_patch: extract from patch text
    if tool_name == "apply_patch":
        patch_text = arguments.get("patch", "")
        if patch_text:
            paths.extend(_RE_PATCH_FILE_PATH.findall(patch_text))

    return paths


def validate_file_paths(paths: list, workspace_root: str = None) -> list:
    """Validate file paths against workspace root.

    Returns a list of warning messages for suspicious paths.
    Does NOT block execution — just generates warnings that can
    be injected into the conversation.

    Args:
        paths: List of file paths to validate
        workspace_root: Expected workspace root directory

    Returns:
        List of warning strings (empty if all paths look OK)
    """
    if not paths:
        return []

    warnings = []
    if not workspace_root:
        try:
            workspace_root = os.environ.get("CODEX_PROJECT_DIR", os.getcwd())
        except Exception:
            workspace_root = None

    for path in paths:
        # Normalize the path
        clean = path.strip().lstrip("./")

        # Check for absolute paths outside workspace
        if os.path.isabs(clean) and workspace_root:
            try:
                rel = os.path.relpath(clean, workspace_root)
                if rel.startswith(".."):
                    warnings.append(
                        f"Warning: File path '{clean}' is outside the workspace "
                        f"root '{workspace_root}'. This may create a file in the "
                        f"wrong location."
                    )
            except Exception:
                pass

        # Check for suspicious patterns: creating files at root without
        # any package prefix in a monorepo
        if "/" not in clean and "." in clean:
            # e.g., "main.go" at root — might be OK, but suspicious in monorepo
            # Only warn if we know the workspace has subdirectories
            pass  # Too many false positives, skip for now

    return warnings
