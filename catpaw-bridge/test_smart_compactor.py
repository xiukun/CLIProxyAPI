#!/usr/bin/env python3
"""Test intelligent message-type-aware compaction."""

import json
import sys
sys.path.insert(0, '.')

from proxy.compactor import (
    _truncate_tool_result,
    _build_tool_call_id_map,
    _get_tool_name_for_result,
    _phase1_compress_tool_results,
    _phase2_summarize_old_turns,
    _phase3_hard_truncate,
    compact_messages,
    _TOOL_TRUNCATION,
    _ROLE_SUMMARY_LEN,
    _RECENT_TOOL_RESULTS_KEEP,
    _RECENT_TURNS_KEEP,
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


# ---- Test 1: Tool-specific truncation — Read (head + tail) ----
read_content = "line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\n" * 100  # ~5500 chars
result = _truncate_tool_result(read_content, "Read")
check("Read: truncated", len(result) < len(read_content), f"{len(result)} vs {len(read_content)}")
check("Read: has head", "line1" in result)
check("Read: has tail", "line10" in result)
check("Read: has marker", "compacted" in result)
check("Read: approx head+tail+marker", len(result) < 700, f"got {len(result)}")

# ---- Test 2: Tool-specific truncation — Bash (head only) ----
bash_content = "total 1432\ndrwxr-xr-x\nfile1\nfile2\nfile3\n" * 200  # ~4800 chars
result = _truncate_tool_result(bash_content, "Bash")
check("Bash: truncated", len(result) < len(bash_content), f"{len(result)} vs {len(bash_content)}")
check("Bash: has head", "total 1432" in result)
check("Bash: no tail (no 'file3' at end)", not result.endswith("file3\n"))
check("Bash: has marker", "compacted" in result)
check("Bash: approx head only", len(result) < 500, f"got {len(result)}")

# ---- Test 3: Tool-specific truncation — Write (keep as-is) ----
write_content = "File created successfully at: /tmp/test.py (file state is current)"
result = _truncate_tool_result(write_content, "Write")
check("Write: kept as-is", result == write_content, f"got {result}")

# ---- Test 4: Tool-specific truncation — Edit (keep as-is) ----
edit_content = "The file has been updated successfully."
result = _truncate_tool_result(edit_content, "Edit")
check("Edit: kept as-is", result == edit_content)

# ---- Test 5: Tool-specific truncation — Grep (head + small tail) ----
grep_content = "src/file1.ts:1:import\nsrc/file2.ts:5:export\nsrc/file3.ts:10:const\n" * 100
result = _truncate_tool_result(grep_content, "Grep")
check("Grep: truncated", len(result) < len(grep_content))
check("Grep: has head", "src/file1.ts" in result)
check("Grep: has marker", "compacted" in result)

# ---- Test 6: Tool-specific truncation — unknown tool (default) ----
unknown_content = "x" * 1000
result = _truncate_tool_result(unknown_content, "SomeUnknownTool")
check("Unknown: truncated with default", len(result) < len(unknown_content))
check("Unknown: has marker", "compacted" in result)

# ---- Test 7: Small content not truncated ----
small_content = "short result"
result = _truncate_tool_result(small_content, "Read")
check("Small: not truncated", result == small_content)

# ---- Test 8: Tool call ID map building ----
messages = [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_abc", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"/a.py"}'}},
        {"id": "call_def", "type": "function", "function": {"name": "Bash", "arguments": '{"command":"ls"}'}},
    ]},
    {"role": "tool", "tool_call_id": "call_abc", "content": "file content A"},
    {"role": "tool", "tool_call_id": "call_def", "content": "shell output"},
]
id_map = _build_tool_call_id_map(messages)
check("ID map: has call_abc", "call_abc" in id_map)
check("ID map: call_abc = Read", id_map.get("call_abc") == "Read")
check("ID map: call_def = Bash", id_map.get("call_def") == "Bash")

# ---- Test 9: Get tool name for result ----
check("Tool name: Read result", _get_tool_name_for_result(messages[2], id_map) == "Read")
check("Tool name: Bash result", _get_tool_name_for_result(messages[3], id_map) == "Bash")
check("Tool name: unknown result", _get_tool_name_for_result({"tool_call_id": "unknown"}, id_map) == "")

# ---- Test 10: Phase 1 — tool-aware compression ----
# Build a conversation with multiple tool results from different tools
# Need >6 tool results so that call_2 (Bash) gets compressed (keep last 3 intact)
tool_msgs = [
    {"role": "user", "content": "Read file and run ls"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"/a.py"}'}},
    ]},
    {"role": "tool", "tool_call_id": "call_1", "content": "A" * 5000},  # Read result (old)
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_2", "type": "function", "function": {"name": "Bash", "arguments": '{"command":"ls"}'}},
    ]},
    {"role": "tool", "tool_call_id": "call_2", "content": "B" * 5000},  # Bash result (old)
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_3", "type": "function", "function": {"name": "Write", "arguments": '{"file_path":"/c.py","content":"x"}'}},
    ]},
    {"role": "tool", "tool_call_id": "call_3", "content": "File created successfully at: /c.py"},  # Write result (old, small)
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_4", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"/d.py"}'}},
    ]},
    {"role": "tool", "tool_call_id": "call_4", "content": "D" * 5000},  # Read result (old)
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_5", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"/e.py"}'}},
    ]},
    {"role": "tool", "tool_call_id": "call_5", "content": "E" * 200},  # Read result (recent, small)
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_6", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"/f.py"}'}},
    ]},
    {"role": "tool", "tool_call_id": "call_6", "content": "F" * 200},  # Read result (recent, small)
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_7", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"/g.py"}'}},
    ]},
    {"role": "tool", "tool_call_id": "call_7", "content": "G" * 200},  # Read result (recent, small)
]
id_map = _build_tool_call_id_map(tool_msgs)
compressed, size = _phase1_compress_tool_results(tool_msgs, id_map)

# call_1 (Read, old) should be compressed with head+tail
call_1_content = [m for m in compressed if m.get("tool_call_id") == "call_1"][0]["content"]
check("Phase1: Read old compressed", len(call_1_content) < 5000, f"got {len(call_1_content)}")
check("Phase1: Read old has marker", "compacted" in call_1_content)

# call_2 (Bash, old) should be compressed with head only
call_2_content = [m for m in compressed if m.get("tool_call_id") == "call_2"][0]["content"]
check("Phase1: Bash old compressed", len(call_2_content) < 5000, f"got {len(call_2_content)}")
check("Phase1: Bash old has marker", "compacted" in call_2_content)

# call_3 (Write, old) should be kept as-is (small content)
call_3_content = [m for m in compressed if m.get("tool_call_id") == "call_3"][0]["content"]
check("Phase1: Write old kept as-is", call_3_content == "File created successfully at: /c.py")

# call_7 (Read, recent) should be kept as-is
call_7_content = [m for m in compressed if m.get("tool_call_id") == "call_7"][0]["content"]
check("Phase1: Read recent kept as-is", call_7_content == "G" * 200)

# ---- Test 11: Phase 2 — role-aware summary lengths ----
# Build a long conversation with many turns
msgs = []
for i in range(20):
    msgs.append({"role": "user", "content": f"User instruction number {i} with some additional context text here " * 10})
    msgs.append({"role": "assistant", "content": f"Assistant response {i} explaining what it will do " * 10,
                 "tool_calls": [{"id": f"tc_{i}", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"/a.py"}'}}] if i < 15 else None})
    msgs.append({"role": "tool", "tool_call_id": f"tc_{i}", "content": f"Tool result {i} " * 20})

id_map = _build_tool_call_id_map(msgs)
summarized, size = _phase2_summarize_old_turns(msgs, id_map)

# Check that old messages were summarized
old_user = msgs[0]  # First user message
old_user_content = old_user["content"]
check("Phase2: old user summarized", "summarized" in old_user_content)
check("Phase2: old user has 200-char summary", len(old_user_content) < 300, f"got {len(old_user_content)}")

# Check that old tool results include tool name
old_tool = msgs[2]  # First tool result
old_tool_content = old_tool["content"]
check("Phase2: old tool has tool name", "[tool:Read]" in old_tool_content or "[tool]" in old_tool_content)

# Check that recent messages are kept intact
recent_idx = len(msgs) - 3  # Last user message
recent_content = msgs[recent_idx]["content"]
check("Phase2: recent user kept intact", "summarized" not in recent_content)

# ---- Test 12: Phase 3 — priority-aware truncation ----
# Tool results should be trimmed before user messages
msgs = []
for i in range(10):
    msgs.append({"role": "user", "content": f"User {i} " * 100})  # ~700 chars each
    msgs.append({"role": "assistant", "content": f"Assistant {i} " * 200})  # ~1400 chars each
    msgs.append({"role": "tool", "tool_call_id": f"tc_{i}", "content": f"Tool {i} " * 500})  # ~3500 chars each

original_total = sum(len(m.get("content", "")) for m in msgs)
id_map = {}
budget = 10000  # Very small budget to force aggressive truncation
truncated, new_total = _phase3_hard_truncate(msgs, budget, id_map)

check("Phase3: total reduced", new_total < original_total, f"{new_total} vs {original_total}")

# Check that user messages are preserved better than tool messages
user_msgs = [m for m in truncated if m.get("role") == "user"]
tool_msgs = [m for m in truncated if m.get("role") == "tool"]
assistant_msgs = [m for m in truncated if m.get("role") == "assistant"]

# At least some user messages should be intact (not hard-truncated)
intact_users = [m for m in user_msgs if "hard-truncated" not in m.get("content", "")]
check("Phase3: some user msgs intact", len(intact_users) > 0, f"only {len(intact_users)} intact")

# Tool messages should be truncated first (more aggressively)
truncated_tools = [m for m in tool_msgs if "hard-truncated" in m.get("content", "")]
check("Phase3: tool msgs truncated first", len(truncated_tools) > 0, f"only {len(truncated_tools)} truncated")

# ---- Test 13: Full compact_messages integration ----
# Build a realistic conversation
msgs = [
    {"role": "system", "content": "System prompt"},
]
for i in range(15):
    msgs.append({"role": "user", "content": f"Please do task {i}. " + "Context " * 50})
    msgs.append({"role": "assistant", "content": f"Working on task {i}. " + "Explanation " * 50,
                 "tool_calls": [{"id": f"tc_{i}", "type": "function", "function": {"name": "Read", "arguments": json.dumps({"file_path": f"/file{i}.py"})}}]})
    msgs.append({"role": "tool", "tool_call_id": f"tc_{i}", "content": f"1 import os\n2 " + "code line\n" * 200})

# Use a small budget to force compaction
result = compact_messages(msgs, max_encrypted_body=50000, overhead=5000)
result_size = sum(len(m.get("content", "")) for m in result)

check("Integration: compacted", result_size < sum(len(m.get("content", "")) for m in msgs))
check("Integration: has recent messages", len(result) > 5)
check("Integration: recent user preserved", any("task" in m.get("content", "") for m in result[-5:]))

# ---- Test 14: No compaction when under budget ----
small_msgs = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "hello"},
]
result = compact_messages(small_msgs, max_encrypted_body=200000, overhead=1000)
check("No compaction: returned as-is", result == small_msgs or len(result) == 2)

# ---- Test 15: Write tool_calls in old messages stripped by Phase 2 ----
msgs = []
for i in range(10):
    msgs.append({"role": "user", "content": f"Task {i}"})
    msgs.append({"role": "assistant", "content": f"Doing {i}",
                 "tool_calls": [{"id": f"tc_{i}", "type": "function",
                    "function": {"name": "Write", "arguments": json.dumps({"file_path": f"/f{i}.py", "content": "x" * 5000})}}]})
    msgs.append({"role": "tool", "tool_call_id": f"tc_{i}", "content": "File created"})

id_map = _build_tool_call_id_map(msgs)
summarized, _ = _phase2_summarize_old_turns(msgs, id_map)

# Old assistant messages should have tool_calls stripped
old_assistant = msgs[1]  # First assistant
check("Phase2: old tool_calls stripped", "tool_calls" not in old_assistant or old_assistant.get("tool_calls") is None)

# Recent assistant should keep tool_calls
recent_assistant = msgs[-2]  # Last assistant
check("Phase2: recent tool_calls kept", "tool_calls" in recent_assistant and recent_assistant.get("tool_calls") is not None)

print(f"\n{'='*60}")
print(f"Results: {passed} passed, {failed} failed, {passed+failed} total")
if failed:
    sys.exit(1)
