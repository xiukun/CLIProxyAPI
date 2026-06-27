#!/usr/bin/env python3
"""Test the intelligent conversation compactor."""
import sys
sys.path.insert(0, '.')

from proxy.compactor import (
    compact_messages,
    compact_merged_content,
    estimate_target_budget,
    _phase1_compress_tool_results,
    _phase2_summarize_old_turns,
)
from proxy.config import MAX_ENCRYPTED_BODY

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name} {detail}")

# ---- Test 1: Small messages — no compaction ----
msgs_small = [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi there!"},
]
result = compact_messages(msgs_small, MAX_ENCRYPTED_BODY)
check("Small messages: no compaction", len(result) == 2 and result[0]["content"] == "Hello")

# ---- Test 2: Large OLD tool result — Phase 1 compression ----
# Need >4 tool results so the oldest ones get compressed
large_tool_result = "A" * 5000 + "B" * 5000  # 10KB
msgs_tool = [
    {"role": "user", "content": "Read the file"},
    {"role": "assistant", "content": "Reading...", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
    {"role": "tool", "content": large_tool_result, "tool_call_id": "c1"},
    {"role": "assistant", "content": "Done reading"},
    {"role": "user", "content": "Read another"},
    {"role": "assistant", "content": "Reading...", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
    {"role": "tool", "content": "C" * 1000, "tool_call_id": "c2"},
    {"role": "assistant", "content": "Done"},
    {"role": "user", "content": "Read another"},
    {"role": "assistant", "content": "Reading...", "tool_calls": [{"id": "c3", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
    {"role": "tool", "content": "D" * 1000, "tool_call_id": "c3"},
    {"role": "assistant", "content": "Done"},
    {"role": "user", "content": "Read another"},
    {"role": "assistant", "content": "Reading...", "tool_calls": [{"id": "c4", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
    {"role": "tool", "content": "E" * 1000, "tool_call_id": "c4"},
    {"role": "assistant", "content": "Done"},
    {"role": "user", "content": "Read another"},
    {"role": "assistant", "content": "Reading...", "tool_calls": [{"id": "c5", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
    {"role": "tool", "content": "F" * 1000, "tool_call_id": "c5"},
    {"role": "assistant", "content": "Done"},
    {"role": "user", "content": "What did it say?"},
]
# Set a small budget to trigger compaction
result = compact_messages(msgs_tool, 5000)  # 5KB encrypted limit
# The OLDEST tool result (c1) should be compressed (beyond recent 4)
tool_msgs = [m for m in result if m["role"] == "tool"]
first_tool = tool_msgs[0]  # c1 — oldest, should be compressed
check("Phase 1: old tool result compressed", "compacted" in first_tool["content"] or "summarized" in first_tool["content"], f"content: {first_tool['content'][:100]}")
check("Phase 1: old tool result smaller", len(first_tool["content"]) < len(large_tool_result))
# The NEWEST tool results should be intact
last_tool = tool_msgs[-1]  # c5 — newest, should be intact
check("Phase 1: recent tool result intact", "compacted" not in str(last_tool["content"]), f"content: {str(last_tool['content'])[:100]}")

# ---- Test 3: Many turns — Phase 2 summarization ----
msgs_many = []
for i in range(20):
    msgs_many.append({"role": "user", "content": f"Question {i}: " + "X" * 500})
    msgs_many.append({"role": "assistant", "content": f"Answer {i}: " + "Y" * 500})

result = compact_messages(msgs_many, 5000)
# Some messages should be summarized
summarized = [m for m in result if "summarized" in str(m.get("content", ""))]
check("Phase 2: old messages summarized", len(summarized) > 0, f"summarized count: {len(summarized)}")
# Recent messages should be intact
last_user = [m for m in result if m["role"] == "user"][-1]
check("Phase 2: recent message intact", "summarized" not in str(last_user["content"]), f"last user: {str(last_user['content'])[:80]}")

# ---- Test 4: Phase 3 hard truncation ----
# Create messages with very large content that can't be compressed enough
msgs_huge = [
    {"role": "user", "content": "X" * 50000},
    {"role": "user", "content": "Y" * 50000},
    {"role": "user", "content": "Z" * 50000},
]
result = compact_messages(msgs_huge, 3000)  # Very small budget
total = sum(len(str(m.get("content", ""))) for m in result)
check("Phase 3: total reduced", total < 150000, f"total: {total}")
# At least some messages should be hard-truncated
truncated = [m for m in result if "hard-truncated" in str(m.get("content", ""))]
check("Phase 3: hard truncation applied", len(truncated) > 0, f"truncated count: {len(truncated)}")

# ---- Test 5: compact_merged_content ----
merged = "A" * 100000
budget = estimate_target_budget(50000)
compacted = compact_merged_content(merged, 50000)
check("Merged: compacted", len(compacted) < len(merged), f"original: {len(merged)}, compacted: {len(compacted)}")
check("Merged: has marker", "compacted" in compacted)
check("Merged: preserves head", compacted.startswith("A" * 100))

# ---- Test 6: compact_merged_content — no compaction when under budget ----
small_merged = "Hello world"
result = compact_merged_content(small_merged, 180000)
check("Merged: no compaction when small", result == small_merged)

# ---- Test 7: Budget estimation ----
budget = estimate_target_budget(180000)
check("Budget: 52% of encrypted", budget == 93600, f"budget={budget}")

# ---- Test 8: OLD tool result with multimodal content (list format) ----
# Need >4 tool results for compression to trigger on old ones
msgs_multi = [
    {"role": "tool", "content": [{"type": "text", "text": "B" * 10000}], "tool_call_id": "c1"},
    {"role": "user", "content": "test"},
    {"role": "tool", "content": "x" * 100, "tool_call_id": "c2"},
    {"role": "user", "content": "test"},
    {"role": "tool", "content": "x" * 100, "tool_call_id": "c3"},
    {"role": "user", "content": "test"},
    {"role": "tool", "content": "x" * 100, "tool_call_id": "c4"},
    {"role": "user", "content": "test"},
    {"role": "tool", "content": "x" * 100, "tool_call_id": "c5"},
    {"role": "user", "content": "test"},
]
result = compact_messages(msgs_multi, 3000)
tool_msg = result[0]
content_str = str(tool_msg["content"])
check("Multi: old tool result compressed", "compacted" in content_str, f"content: {content_str[:100]}")

# ---- Test 9: Preserve tool_calls in recent assistant messages ----
msgs_preserve = []
for i in range(15):
    msgs_preserve.append({"role": "user", "content": f"Q{i} " + "X" * 300})
    msgs_preserve.append({"role": "assistant", "content": f"A{i} " + "Y" * 300, "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]})

result = compact_messages(msgs_preserve, 3000)
# Last assistant message should still have tool_calls
last_asst = [m for m in result if m["role"] == "assistant"][-1]
check("Preserve: recent tool_calls intact", "tool_calls" in last_asst, f"keys: {list(last_asst.keys())}")

# ---- Test 10: Realistic Claude Code scenario ----
msgs_realistic = [
    {"role": "system", "content": "You are Claude Code... " + "S" * 50000},
    {"role": "user", "content": "You have access to tools... " + "T" * 30000},
    {"role": "user", "content": "Read the file at /path/to/file.ts"},
    {"role": "assistant", "content": "I'll read that file.", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"/path/to/file.ts"}'}}]},
    {"role": "tool", "content": "import React from 'react';\n" + "code\n" * 5000, "tool_call_id": "c1"},
    {"role": "assistant", "content": "The file contains React code. " + "A" * 2000},
    {"role": "user", "content": "Now read another file"},
    {"role": "assistant", "content": "Reading another file.", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"/path/to/other.ts"}'}}]},
    {"role": "tool", "content": "export class Foo {}\n" + "code\n" * 5000, "tool_call_id": "c2"},
    {"role": "user", "content": "What do both files do?"},
]

# Calculate original size
orig_size = sum(len(str(m.get("content", ""))) for m in msgs_realistic)
result = compact_messages(msgs_realistic, 180000)
new_size = sum(len(str(m.get("content", ""))) for m in result)
check("Realistic: size reduced", new_size < orig_size, f"orig: {orig_size}, new: {new_size}")
check("Realistic: last user message intact", result[-1]["content"] == "What do both files do?")

print(f"\n{'='*60}")
print(f"Results: {passed} passed, {failed} failed, {passed+failed} total")
sys.exit(0 if failed == 0 else 1)
