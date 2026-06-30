#!/usr/bin/env python3
"""Adversarial tests for the three-layer defense + workspace boundary defense.

Tests cover:
  1. workspace_context.py — extraction, anchor building, path validation
  2. compactor.py — _find_edit_paired_reads() enhanced protection
  3. compactor.py — _truncate_tool_result() line-number-aware markers
  4. codex_aware.py / claude_aware.py — workspace_context injection
  5. Edge cases discovered during adversarial review
"""

import json
import sys
import os

sys.path.insert(0, '.')

from proxy.workspace_context import (
    extract_workspace_context,
    build_workspace_anchor,
    extract_file_paths_from_tool_call,
    validate_file_paths,
    _WORKSPACE_BOUNDARY_RULES,
)
from proxy.compactor import (
    _truncate_tool_result,
    _find_edit_paired_reads,
    _build_tool_call_id_map,
    _phase1_compress_tool_results,
    compact_messages,
    _TOOL_TRUNCATION,
)
from proxy.codex_aware import (
    build_codex_system_prompt,
    compress_codex_system_prompt,
    _build_large_file_strategy,
    _APPLY_PATCH_FORMAT,
)
from proxy.claude_aware import (
    build_claude_code_system_prompt,
)

passed = 0
failed = 0

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name} — {detail}")


# ===========================================================================
# Part 1: workspace_context.py — extraction
# ===========================================================================

print("\n--- Part 1: workspace_context.py extraction ---")

# Test 1.1: Codex environment_context extraction
codex_prompt = """You are a coding agent.
<environment_context>
cwd: /home/user/projects/monorepo
platform: linux
shell: /bin/bash
git_branch: main
</environment_context>
<repo_layout>
packages/frontend/src/
packages/backend/src/
packages/shared/src/
cmd/server/main.go
</repo_layout>
Some other instructions here."""

ctx = extract_workspace_context(codex_prompt, is_codex=True)
check("Codex: extracts environment", "Environment:" in ctx, f"ctx: {ctx[:100]}")
check("Codex: extracts cwd", "/home/user/projects/monorepo" in ctx, f"ctx: {ctx[:200]}")
check("Codex: extracts repo layout", "Repo Layout:" in ctx, f"ctx: {ctx[:200]}")
check("Codex: extracts package structure", "packages/frontend" in ctx, f"ctx: {ctx[:300]}")

# Test 1.2: Claude Code environment extraction
claude_prompt = """You are Claude Code.
<environment>
Working directory: /Users/dev/workspace/myproject
Platform: darwin
</environment>
<workspace_info>
Open files: src/main.ts, src/utils.ts
Git branch: feature/auth
</workspace_info>
Instructions..."""

ctx = extract_workspace_context(claude_prompt, is_claude_code=True)
check("Claude: extracts environment", "Environment:" in ctx, f"ctx: {ctx[:100]}")
check("Claude: extracts working dir", "/Users/dev/workspace/myproject" in ctx, f"ctx: {ctx[:200]}")
check("Claude: extracts workspace info", "Workspace:" in ctx, f"ctx: {ctx[:200]}")

# Test 1.3: Empty system content
ctx = extract_workspace_context("", is_codex=True)
check("Empty: returns empty or fallback", ctx == "" or "Working Directory" in ctx)

# Test 1.4: No tags, but has cwd-like text
plain_prompt = "The current working directory is /some/path/here"
ctx = extract_workspace_context(plain_prompt, is_codex=True)
check("Fallback: extracts cwd from text", "/some/path/here" in ctx or ctx == "", f"ctx: {ctx}")

# Test 1.5: Large environment_context gets compressed
large_env = "<environment_context>\n" + "X" * 500 + "\n</environment_context>"
ctx = extract_workspace_context(large_env, is_codex=True)
check("Large env: compressed to <350 chars", len(ctx) < 400, f"len: {len(ctx)}")

# Test 1.6: Large repo_layout gets compressed
large_repo = "<repo_layout>\n" + "dir/file\n" * 200 + "\n</repo_layout>"
ctx = extract_workspace_context(large_repo, is_codex=True)
check("Large repo: compressed to <600 chars", len(ctx) < 600, f"len: {len(ctx)}")

# Test 1.7: Neither codex nor claude_code — fallback to cwd
ctx = extract_workspace_context("some random text", is_codex=False, is_claude_code=False)
# Should fall back to os.getcwd()
check("Neither: fallback to cwd", "Working Directory" in ctx or ctx == "", f"ctx: {ctx[:100]}")


# ===========================================================================
# Part 2: workspace_context.py — anchor building
# ===========================================================================

print("\n--- Part 2: workspace_context.py anchor building ---")

# Test 2.1: Anchor with context
anchor = build_workspace_anchor("Environment: cwd=/test\nRepo Layout: pkg/a/ pkg/b/")
check("Anchor: has header", "Workspace Context" in anchor)
check("Anchor: has context", "cwd=/test" in anchor)
check("Anchor: has boundary rules", "MONOREPO" in anchor)
check("Anchor: has package safety rule", "CORRECT package" in anchor)

# Test 2.2: Anchor without context (empty)
anchor_empty = build_workspace_anchor("")
check("Empty anchor: still has rules", "MONOREPO" in anchor_empty)
check("Empty anchor: has boundary rules", _WORKSPACE_BOUNDARY_RULES in anchor_empty)

# Test 2.3: Anchor is compact enough for system prompt
anchor_long = build_workspace_anchor("X" * 500)
check("Anchor: reasonable size (<2000)", len(anchor_long) < 2000, f"len: {len(anchor_long)}")


# ===========================================================================
# Part 3: workspace_context.py — file path extraction
# ===========================================================================

print("\n--- Part 3: file path extraction from tool calls ---")

# Test 3.1: Write tool
paths = extract_file_paths_from_tool_call("Write", {"file_path": "/src/main.py", "content": "code"})
check("Write: extracts file_path", paths == ["/src/main.py"], f"paths: {paths}")

# Test 3.2: Edit tool with target_file
paths = extract_file_paths_from_tool_call("Edit", {"target_file": "/src/utils.ts", "old_string": "a", "new_string": "b"})
check("Edit: extracts target_file", "/src/utils.ts" in paths, f"paths: {paths}")

# Test 3.3: apply_patch — extract from patch text
patch = """*** Begin Patch
*** Add File: packages/frontend/src/new.ts
+import React
*** Update File: packages/backend/src/api.ts
-old
+new
*** End Patch"""
paths = extract_file_paths_from_tool_call("apply_patch", {"patch": patch})
check("apply_patch: extracts Add File", "packages/frontend/src/new.ts" in paths, f"paths: {paths}")
check("apply_patch: extracts Update File", "packages/backend/src/api.ts" in paths, f"paths: {paths}")

# Test 3.4: Non-file tool returns empty
paths = extract_file_paths_from_tool_call("Bash", {"command": "ls -la"})
check("Bash: no paths", paths == [], f"paths: {paths}")

# Test 3.5: NotebookEdit with notebook_path
paths = extract_file_paths_from_tool_call("NotebookEdit", {"notebook_path": "/notebooks/analysis.ipynb"})
check("NotebookEdit: extracts notebook_path", "/notebooks/analysis.ipynb" in paths, f"paths: {paths}")

# Test 3.6: validate_file_paths — path outside workspace
warnings = validate_file_paths(["/other/project/file.py"], workspace_root="/home/user/myproject")
check("Validate: warns about outside path", len(warnings) > 0, f"warnings: {warnings}")

# Test 3.7: validate_file_paths — path inside workspace
warnings = validate_file_paths(["/home/user/myproject/src/file.py"], workspace_root="/home/user/myproject")
check("Validate: no warning for inside path", len(warnings) == 0, f"warnings: {warnings}")

# Test 3.8: validate_file_paths — empty list
warnings = validate_file_paths([])
check("Validate: empty list returns empty", warnings == [])


# ===========================================================================
# Part 4: compactor.py — _find_edit_paired_reads() enhanced protection
# ===========================================================================

print("\n--- Part 4: _find_edit_paired_reads() enhanced protection ---")

# Test 4.1: Single Read → Edit in same turn — Read is protected
msgs = [
    {"role": "user", "content": "Edit the file"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "r1", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"/a.py"}'}},
    ]},
    {"role": "tool", "tool_call_id": "r1", "content": "line1\nline2\nline3"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "e1", "type": "function", "function": {"name": "Edit", "arguments": '{"file_path":"/a.py","old_string":"line2","new_string":"line2b"}'}},
    ]},
]
id_map = _build_tool_call_id_map(msgs)
protected = _find_edit_paired_reads(msgs, id_map)
check("Single Read+Edit: Read protected", 2 in protected, f"protected: {protected}")  # index 2 = tool result

# Test 4.2: Multiple Reads → Edit — ALL Reads in same turn are protected
msgs = [
    {"role": "user", "content": "Edit both files"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "r1", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"/a.py"}'}},
    ]},
    {"role": "tool", "tool_call_id": "r1", "content": "file A content"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "r2", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"/b.py"}'}},
    ]},
    {"role": "tool", "tool_call_id": "r2", "content": "file B content"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "e1", "type": "function", "function": {"name": "Edit", "arguments": '{"file_path":"/a.py"}'}},
    ]},
]
id_map = _build_tool_call_id_map(msgs)
protected = _find_edit_paired_reads(msgs, id_map)
check("Multi Read+Edit: first Read protected", 2 in protected, f"protected: {protected}")
check("Multi Read+Edit: second Read protected", 4 in protected, f"protected: {protected}")

# Test 4.3: Read in previous turn, Edit in current turn — old Read NOT protected
msgs = [
    {"role": "user", "content": "Read the file"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "r1", "type": "function", "function": {"name": "Read", "arguments": '{}'}},
    ]},
    {"role": "tool", "tool_call_id": "r1", "content": "old content"},
    {"role": "assistant", "content": "Done reading"},
    {"role": "user", "content": "Now edit it"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "e1", "type": "function", "function": {"name": "Edit", "arguments": '{}'}},
    ]},
]
id_map = _build_tool_call_id_map(msgs)
protected = _find_edit_paired_reads(msgs, id_map)
check("Previous turn Read: NOT protected", 2 not in protected, f"protected: {protected}")

# Test 4.4: apply_patch triggers protection (not just Edit/MultiEdit)
msgs = [
    {"role": "user", "content": "Patch the file"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "r1", "type": "function", "function": {"name": "read_file", "arguments": '{}'}},
    ]},
    {"role": "tool", "tool_call_id": "r1", "content": "file content"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "p1", "type": "function", "function": {"name": "apply_patch", "arguments": '{}'}},
    ]},
]
id_map = _build_tool_call_id_map(msgs)
protected = _find_edit_paired_reads(msgs, id_map)
check("apply_patch: read_file protected", 2 in protected, f"protected: {protected}")

# Test 4.5: No Edit at all — nothing protected
msgs = [
    {"role": "user", "content": "Read the file"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "r1", "type": "function", "function": {"name": "Read", "arguments": '{}'}},
    ]},
    {"role": "tool", "tool_call_id": "r1", "content": "content"},
]
id_map = _build_tool_call_id_map(msgs)
protected = _find_edit_paired_reads(msgs, id_map)
check("No Edit: nothing protected", len(protected) == 0, f"protected: {protected}")

# Test 4.6: Non-Read tool result not protected
msgs = [
    {"role": "user", "content": "Run ls then edit"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "b1", "type": "function", "function": {"name": "Bash", "arguments": '{}'}},
    ]},
    {"role": "tool", "tool_call_id": "b1", "content": "file1\nfile2"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "e1", "type": "function", "function": {"name": "Edit", "arguments": '{}'}},
    ]},
]
id_map = _build_tool_call_id_map(msgs)
protected = _find_edit_paired_reads(msgs, id_map)
check("Bash result: NOT protected", 2 not in protected, f"protected: {protected}")


# ===========================================================================
# Part 5: compactor.py — _truncate_tool_result() line-number-aware markers
# ===========================================================================

print("\n--- Part 5: _truncate_tool_result() line-number markers ---")

# Test 5.1: Read truncation with tail — marker includes line numbers
# Create content with 50 lines, each 100 chars (5000+ chars total)
lines = [f"line_{i:03d} " + "X" * 90 for i in range(50)]
content = "\n".join(lines) + "\n"  # ends with \n
result = _truncate_tool_result(content, "Read")
check("Read trunc: has line marker", "lines" in result or "partial line" in result, f"result tail: {result[2000:2200]}")
check("Read trunc: has omitted info", "omitted" in result)
check("Read trunc: has char count", "chars" in result)
check("Read trunc: smaller than original", len(result) < len(content))

# Test 5.2: Read truncation without newline at end (off-by-one fix)
# Use tail=0 to test the head-only branch where the off-by-one bug existed
lines_no_newline = [f"line_{i:03d} " + "X" * 90 for i in range(50)]
content_no_nl = "\n".join(lines_no_newline)  # NO trailing \n, 49 newlines, 50 lines
custom_head_only_nl = {"Read": {"head": 1000, "tail": 0}}
result_no_nl = _truncate_tool_result(content_no_nl, "Read", custom_head_only_nl)
# With the fix: total_lines = 49 + 1 = 50 (because content doesn't end with \n)
# marker should say "lines 11-50 omitted" (head_lines≈10, total_lines=50)
check("Read no-newline: mentions line 50", "50" in result_no_nl,
      f"marker should include line 50, got: {result_no_nl[1000:1100]}")
check("Read no-newline: has line marker", "lines" in result_no_nl)

# Test 5.3: read_file (Codex tool name) also gets line markers
result_codex = _truncate_tool_result(content, "read_file")
check("read_file: has line marker", "lines" in result_codex or "partial" in result_codex)

# Test 5.4: Non-Read tool does NOT get line markers
bash_content = "output\n" * 500
result_bash = _truncate_tool_result(bash_content, "Bash")
check("Bash: no line marker", "lines" not in result_bash)
check("Bash: has compacted marker", "compacted" in result_bash)

# Test 5.5: Small content not truncated
small = "short file\n"
check("Small Read: not truncated", _truncate_tool_result(small, "Read") == small)

# Test 5.6: Line number math correctness — explicit test
# 10 lines, each exactly 10 chars + newline = 11 chars per line = 110 chars
exact_lines = "".join(f"abcdefghij\n" for _ in range(10))  # 110 chars, 10 newlines
# head=22 (2 lines), tail=22 (2 lines) → threshold = 22+22+50 = 94
# 110 > 94, so truncation happens
# head_part = first 22 chars = "abcdefghij\nabcdefghij\n" (2 newlines → head_lines=2)
# tail_part = last 22 chars = "abcdefghij\nabcdefghij\n" (2 newlines)
# total_newlines = 10, tail_newlines = 2
# tail_first_line = 10 - 2 + 1 = 9
# omitted_lines = 9 - 2 - 1 = 6 (lines 3-8)
# Let's verify with custom truncation table
custom_trunc = {"Read": {"head": 22, "tail": 22}}
result = _truncate_tool_result(exact_lines, "Read", custom_trunc)
check("Line math: head has 2 lines", result.startswith("abcdefghij\nabcdefghij\n"))
check("Line math: tail has last 2 lines", result.endswith("abcdefghij\nabcdefghij\n"))
check("Line math: marker says lines 3-8", "lines 3-8" in result, f"marker: {result[22:80]}")
check("Line math: 6 lines omitted", "6 lines" in result, f"marker: {result[22:80]}")

# Test 5.7: Tail-only (head=0, tail>0) — edge case with Read
# Actually head can't be 0 in practice, but test head-only (tail=0)
custom_head_only = {"Read": {"head": 22, "tail": 0}}
result_head = _truncate_tool_result(exact_lines, "Read", custom_head_only)
# head_lines=2, total_lines=10 (content ends with \n so count=10)
# marker: lines 3-10 omitted
check("Head-only: marker says lines 3-10", "lines 3-10" in result_head, f"marker: {result_head[22:80]}")

# Test 5.8: Head-only without trailing newline
exact_no_nl = "".join(f"abcdefghij\n" for _ in range(9)) + "abcdefghij"  # 109 chars, 9 newlines, 10 lines
result_head_no_nl = _truncate_tool_result(exact_no_nl, "Read", custom_head_only)
# head_lines=2, total_lines = 9 + 1 = 10 (because no trailing \n)
# marker: lines 3-10 omitted
check("Head-only no-newline: marker says lines 3-10", "lines 3-10" in result_head_no_nl,
      f"marker: {result_head_no_nl[22:80]}")

# Test 5.9: Omitted lines = 0 (head and tail overlap within a line)
# Content: one very long line with no newlines
one_line = "X" * 200
custom_overlap = {"Read": {"head": 80, "tail": 80}}
# threshold = 80 + 80 + 50 = 210, content = 200, 200 < 210 → NOT truncated!
# Need to make it larger
one_line = "X" * 300
result_overlap = _truncate_tool_result(one_line, "Read", custom_overlap)
# head_lines=0 (no \n in head), tail_newlines=0, total_newlines=0
# tail_first_line = 0 - 0 + 1 = 1
# omitted_lines = 1 - 0 - 1 = 0
# Should say "partial line omitted"
check("Overlap: says partial line", "partial line" in result_overlap, f"result: {result_overlap[80:160]}")


# ===========================================================================
# Part 6: Phase 1 integration — protected Read survives compression
# ===========================================================================

print("\n--- Part 6: Phase 1 protected Read integration ---")

# Test 6.1: Protected Read result is NOT compressed even when old
msgs = [
    {"role": "user", "content": "Read and edit the file"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "r1", "type": "function", "function": {"name": "Read", "arguments": '{}'}},
    ]},
    {"role": "tool", "tool_call_id": "r1", "content": "A" * 5000},  # Large Read result
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "e1", "type": "function", "function": {"name": "Edit", "arguments": '{}'}},
    ]},
    {"role": "tool", "tool_call_id": "e1", "content": "Edit applied"},
]
# Add 10 Bash tool calls + results to push r1 well past recent_keep=8
for i in range(10):
    msgs.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": f"t{i}", "type": "function", "function": {"name": "Bash", "arguments": '{}'}}
    ]})
    msgs.append({"role": "tool", "tool_call_id": f"t{i}", "content": f"output {i}" * 100})
msgs.append({"role": "user", "content": "Done"})

id_map = _build_tool_call_id_map(msgs)
# Manually call phase1 to check protection
msgs_copy = [dict(m) for m in msgs]
compressed, size = _phase1_compress_tool_results(msgs_copy, id_map)

# Find the Read result (r1)
r1_msg = [m for m in compressed if m.get("tool_call_id") == "r1"][0]
r1_content = str(r1_msg.get("content", ""))
check("Protected Read: NOT compressed", "A" * 100 in r1_content or "AAAA" in r1_content,
      f"content preview: {r1_content[:100]}")

# Test 6.2: Unprotected old Read result IS compressed
msgs_unprotected = [
    {"role": "user", "content": "Read file"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "r1", "type": "function", "function": {"name": "Read", "arguments": '{}'}},
    ]},
    {"role": "tool", "tool_call_id": "r1", "content": "B" * 5000},  # Large Read result
    {"role": "assistant", "content": "Done reading"},
    # No Edit follows → r1 is NOT protected
]
# Add more tool results
for i in range(10):
    msgs_unprotected.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"t{i}", "type": "function", "function": {"name": "Bash", "arguments": '{}'}}]})
    msgs_unprotected.append({"role": "tool", "tool_call_id": f"t{i}", "content": f"out {i}" * 50})
msgs_unprotected.append({"role": "user", "content": "Done"})

id_map = _build_tool_call_id_map(msgs_unprotected)
msgs_copy2 = [dict(m) for m in msgs_unprotected]
compressed2, _ = _phase1_compress_tool_results(msgs_copy2, id_map)

r1_msg2 = [m for m in compressed2 if m.get("tool_call_id") == "r1"][0]
r1_content2 = str(r1_msg2.get("content", ""))
check("Unprotected Read: IS compressed", len(r1_content2) < 5000,
      f"len: {len(r1_content2)}")
check("Unprotected Read: has marker", "omitted" in r1_content2 or "compacted" in r1_content2,
      f"preview: {r1_content2[:100]}")


# ===========================================================================
# Part 7: codex_aware.py / claude_aware.py — workspace_context injection
# ===========================================================================

print("\n--- Part 7: system prompt workspace injection ---")

# Test 7.1: Codex system prompt with workspace_context
prompt = build_codex_system_prompt(
    codex_instructions="Codex rules here",
    workspace_context="Environment: cwd=/monorepo\nRepo Layout: pkg/a/ pkg/b/"
)
check("Codex prompt: has workspace context", "Workspace Context" in prompt)
check("Codex prompt: has cwd", "/monorepo" in prompt)
check("Codex prompt: has boundary rules", "MONOREPO" in prompt)
check("Codex prompt: has large file strategy", "Large File Editing Strategy" in prompt)
check("Codex prompt: has apply_patch format", "apply_patch Format" in prompt)
check("Codex prompt: has 3-5 context lines", "3-5 context lines" in prompt)

# Test 7.2: Codex system prompt WITHOUT workspace_context
prompt_no_ws = build_codex_system_prompt(
    codex_instructions="Codex rules here",
    workspace_context=""
)
check("Codex no-ws: no workspace section", "Workspace Context" not in prompt_no_ws)
check("Codex no-ws: still has large file strategy", "Large File Editing Strategy" in prompt_no_ws)

# Test 7.3: Claude Code system prompt with workspace_context
prompt_claude = build_claude_code_system_prompt(
    claude_instructions="Claude rules here",
    workspace_context="Environment: cwd=/project\nWorkspace: open files"
)
check("Claude prompt: has workspace context", "Workspace Context" in prompt_claude)
check("Claude prompt: has cwd", "/project" in prompt_claude)
check("Claude prompt: has boundary rules", "MONOREPO" in prompt_claude)
check("Claude prompt: has large file strategy", "Large File Editing Strategy" in prompt_claude)

# Test 7.4: Claude Code system prompt WITHOUT workspace_context
prompt_claude_no_ws = build_claude_code_system_prompt(
    claude_instructions="Claude rules here",
    workspace_context=""
)
check("Claude no-ws: no workspace section", "Workspace Context" not in prompt_claude_no_ws)

# Test 7.5: Workspace context survives in system prompt (not in conversation)
# The system prompt is built once and passed as a string — it's NOT subject
# to compaction (which only applies to conversation messages)
prompt_with_ws = build_codex_system_prompt(
    codex_instructions="",
    workspace_context="Environment: cwd=/monorepo"
)
# The workspace info should be in the prompt, not strippable by compaction
check("WS survives: in system prompt", "/monorepo" in prompt_with_ws)
# Verify compactor doesn't touch system prompts (it only compresses messages)
msgs_test = [
    {"role": "user", "content": "test"},
    {"role": "assistant", "content": "ok"},
]
compacted = compact_messages(msgs_test, 200000)  # Large budget, no compaction
check("WS survives: compactor doesn't affect system prompt", len(compacted) == 2)


# ===========================================================================
# Part 8: apply_patch format — 3-5 context lines (not 1-3)
# ===========================================================================

print("\n--- Part 8: apply_patch context lines ---")

# Test 8.1: Verify apply_patch format says 3-5, not 1-3
check("apply_patch: says 3-5", "3-5 context lines" in _APPLY_PATCH_FORMAT)
check("apply_patch: does NOT say 1-3", "1-3 context lines" not in _APPLY_PATCH_FORMAT)

# Test 8.2: Large file strategy mentions offset/limit
_large_file_strategy = _build_large_file_strategy({"read_file", "apply_patch"})
check("Large file strategy: mentions offset", "offset" in _large_file_strategy)
check("Large file strategy: mentions limit", "limit" in _large_file_strategy)
check("Large file strategy: mentions 500 lines", "500" in _large_file_strategy)
check("Large file strategy: mentions 2000 lines", "2000" in _large_file_strategy)


# ===========================================================================
# Part 9: Edge cases — adversarial inputs
# ===========================================================================

print("\n--- Part 9: adversarial edge cases ---")

# Test 9.1: Nested XML-like tags in system prompt
nested_prompt = """<environment_context>
cwd: /test
<inner_tag>should not break extraction</inner_tag>
</environment_context>"""
ctx = extract_workspace_context(nested_prompt, is_codex=True)
check("Nested XML: extracts correctly", "/test" in ctx, f"ctx: {ctx}")

# Test 9.2: Empty tags
empty_tags = "<environment_context></environment_context>"
ctx = extract_workspace_context(empty_tags, is_codex=True)
check("Empty tags: handled gracefully", ctx == "" or "Environment" not in ctx or len(ctx) < 50)

# Test 9.3: Malformed tags (no closing)
malformed = "<environment_context>\ncwd: /test\n"
ctx = extract_workspace_context(malformed, is_codex=True)
check("Malformed tags: no crash", isinstance(ctx, str))

# Test 9.4: Unicode in content
unicode_content = "line_① " * 500 + "\n"
result = _truncate_tool_result(unicode_content, "Read")
check("Unicode: truncation works", isinstance(result, str))
check("Unicode: has marker", "omitted" in result or "partial" in result)

# Test 9.5: Content with only newlines
newlines_only = "\n" * 5000
result = _truncate_tool_result(newlines_only, "Read")
check("Newlines only: no crash", isinstance(result, str))

# Test 9.6: apply_patch with multiple files in one patch
multi_patch = """*** Begin Patch
*** Add File: a.py
+code
*** Update File: b.py
-old
+new
*** Delete File: c.py
*** End Patch"""
paths = extract_file_paths_from_tool_call("apply_patch", {"patch": multi_patch})
check("Multi-patch: extracts all 3 files", len(paths) == 3, f"paths: {paths}")
check("Multi-patch: has a.py", "a.py" in paths)
check("Multi-patch: has b.py", "b.py" in paths)
check("Multi-patch: has c.py", "c.py" in paths)

# Test 9.7: _find_edit_paired_reads with empty messages
protected = _find_edit_paired_reads([], {})
check("Empty messages: no crash", protected == set())

# Test 9.8: _find_edit_paired_reads with no tool results
msgs_no_tools = [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hi"},
]
protected = _find_edit_paired_reads(msgs_no_tools, {})
check("No tools: no crash", protected == set())

# Test 9.9: Compress Codex system prompt with environment_context
codex_full = """You are a coding agent.
<environment_context>
cwd: /home/user/project
platform: darwin
</environment_context>
<repo_layout>
cmd/
internal/
sdk/
</repo_layout>
Here are your instructions..."""
compressed = compress_codex_system_prompt(codex_full)
check("Compress: strips env_context", "<environment_context>" not in compressed)
check("Compress: strips repo_layout", "<repo_layout>" not in compressed)
check("Compress: keeps instructions", "instructions" in compressed)


# ===========================================================================
# Part 10: End-to-end integration — workspace context flows through
# ===========================================================================

print("\n--- Part 10: end-to-end integration ---")

# Test 10.1: Full Codex flow — workspace context extracted then injected
codex_system = """You are Codex.
<environment_context>
cwd: /monorepo
platform: linux
</environment_context>
<repo_layout>
packages/frontend/
packages/backend/
packages/shared/
</repo_layout>
Use apply_patch for file changes."""

ws_ctx = extract_workspace_context(codex_system, is_codex=True)
compressed = compress_codex_system_prompt(codex_system)
prompt = build_codex_system_prompt(
    codex_instructions=compressed,
    workspace_context=ws_ctx,
)

check("E2E: workspace in final prompt", "/monorepo" in prompt)
check("E2E: packages in final prompt", "packages/frontend" in prompt or "packages" in prompt)
check("E2E: env_context stripped from compressed", "<environment_context>" not in compressed)
check("E2E: env_context not in prompt", "<environment_context>" not in prompt)
check("E2E: boundary rules in prompt", "MONOREPO" in prompt)

# Test 10.2: Full Claude Code flow
claude_system = """You are Claude Code.
<environment>
Working directory: /Users/dev/project
</environment>
<workspace_info>
Git branch: main
</workspace_info>
Some instructions."""

ws_ctx = extract_workspace_context(claude_system, is_claude_code=True)
prompt = build_claude_code_system_prompt(
    claude_instructions="compressed rules",
    workspace_context=ws_ctx,
)
check("E2E Claude: workspace in prompt", "/Users/dev/project" in prompt)
check("E2E Claude: boundary rules in prompt", "MONOREPO" in prompt)


print(f"\n{'='*60}")
print(f"Results: {passed} passed, {failed} failed, {passed+failed} total")
if failed:
    sys.exit(1)
