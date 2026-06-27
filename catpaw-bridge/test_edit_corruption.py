#!/usr/bin/env python3
"""Test that _clean_json_string doesn't corrupt Edit tool call arguments.

This test reproduces the bug where _clean_json_string's regex
  "\s*:\s*" → ":"
corrupts string values containing " : " patterns, causing Edit's old_string
to not match the actual file content.
"""

import json
import sys
sys.path.insert(0, '.')

from proxy.toolcall import (
    _clean_json_string,
    _extract_tool_call,
    _parse_tool_calls,
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


# ---- Test 1: _clean_json_string should NOT corrupt " : " inside string values ----
# This is the CORE BUG: the regex "\s*:\s*" matches inside string values
print("\n=== Test 1: _clean_json_string with ' : ' inside string values ===")

# Simulate an Edit tool call where old_string contains JSON-like syntax
# The model outputs (with unescaped quotes, as LLMs often do):
raw_json = '{"name":"Edit","arguments":{"file_path":"/src/file.ts","old_string":"const obj = { "key" : "value" }","new_string":"const obj = { "key" : "value" }"}}'

cleaned = _clean_json_string(raw_json)
print(f"  Raw:     {raw_json}")
print(f"  Cleaned: {cleaned}")

# The " : " inside the old_string should NOT be changed to ":"
# But with the current bug, it IS changed:
check("clean_json preserves ' : ' in strings", '" : "' in cleaned, f"' : ' was corrupted to ':\"' — this changes old_string content!")

# ---- Test 2: Edit tool call with properly escaped quotes ----
print("\n=== Test 2: Edit with properly escaped quotes ===")
# Model outputs properly escaped JSON:
escaped_json = r'{"name":"Edit","arguments":{"file_path":"/src/file.ts","old_string":"const x = \"hello : world\"","new_string":"const x = \"hello world\""}}'

tc = _extract_tool_call(escaped_json)
check("Escaped Edit parsed", tc is not None, "Failed to parse")
if tc:
    args = json.loads(tc["function"]["arguments"])
    check("old_string preserves content", "hello : world" in args.get("old_string", ""), f"old_string was: {args.get('old_string', '')!r}")
    check("new_string preserves content", "hello world" in args.get("new_string", ""), f"new_string was: {args.get('new_string', '')!r}")


# ---- Test 3: Edit tool call with unescaped quotes (common LLM behavior) ----
print("\n=== Test 3: Edit with unescaped quotes (LLM-style) ===")
# This is what LLMs often output — unescaped quotes inside string values
# The old_string is: const x = "hello : world"
# The new_string is: const x = "hello world"
unescaped_json = '{"name":"Edit","arguments":{"file_path":"/src/file.ts","old_string":"const x = "hello : world"","new_string":"const x = "hello world""}}'

tc = _extract_tool_call(unescaped_json)
check("Unescaped Edit parsed", tc is not None, "Failed to parse")
if tc:
    args = json.loads(tc["function"]["arguments"])
    # The old_string should contain "hello : world" (with spaces around colon)
    # But with the bug, it becomes "hello":"world" (no spaces)
    check("old_string preserves ' : '", "hello : world" in args.get("old_string", ""), f"old_string was corrupted: {args.get('old_string', '')!r}")
    check("new_string preserves content", "hello world" in args.get("new_string", ""), f"new_string was: {args.get('new_string', '')!r}")


# ---- Test 4: Edit with comma-space-quote pattern ----
print("\n=== Test 4: Edit with ', ' pattern inside string values ===")
# The regex ,\s*" → ," also corrupts string values
# old_string contains: func(a, "b")
raw4 = '{"name":"Edit","arguments":{"old_string":"result = func(a, "b")","new_string":"result = func(a, "c")"}}'

tc = _extract_tool_call(raw4)
check("Comma-quote Edit parsed", tc is not None, "Failed to parse")
if tc:
    args = json.loads(tc["function"]["arguments"])
    check("old_string preserves ', \"", 'a, "b"' in args.get("old_string", ""), f"old_string was: {args.get('old_string', '')!r}")


# ---- Test 5: Full tool call parsing through _parse_tool_calls ----
print("\n=== Test 5: Full _parse_tool_calls with Edit ===")
# Simulate what the model outputs in the SSE stream
model_output = '''I'll fix the syntax error on line 364.

<tool_call>{"name":"Edit","arguments":{"file_path":"/src/batch-embedding-service.ts","old_string":"const result = transform(\\"${text}\\")")","new_string":"const result = transform(\\"${text}\\")"}}</tool_call>'''

clean_text, tool_calls = _parse_tool_calls(model_output)
check("Tool call found", len(tool_calls) == 1, f"Found {len(tool_calls)} tool calls")
if tool_calls:
    check("Tool name is Edit", tool_calls[0]["function"]["name"] == "Edit", f"Name: {tool_calls[0]['function']['name']}")
    args = json.loads(tool_calls[0]["function"]["arguments"])
    # The old_string should have more ) than new_string
    old_count = args.get("old_string", "").count(")")
    new_count = args.get("new_string", "").count(")")
    check("old_string has more ) than new_string", old_count > new_count, f"old={old_count}, new={new_count}")
    check("old_string has ${text}", '${text}' in args.get("old_string", ""), f"old_string: {args.get('old_string', '')!r}")
    check("new_string has ${text}", '${text}' in args.get("new_string", ""), f"new_string: {args.get('new_string', '')!r}")


# ---- Test 6: Edit with TypeScript template literals ----
print("\n=== Test 6: Edit with TypeScript template literals ===")
# The user's actual case: "${text}") should be "${text}"
# old_string: something("${text})")
# new_string: something("${text})")
# Note: backticks and ${} don't need escaping in JSON, but " does
raw6 = r'{"name":"Edit","arguments":{"file_path":"/src/file.ts","old_string":"const x = func(\"${text}\")\")","new_string":"const x = func(\"${text}\")"}}'

tc = _extract_tool_call(raw6)
check("Template literal Edit parsed", tc is not None, "Failed to parse")
if tc:
    args = json.loads(tc["function"]["arguments"])
    check("old_string has ${text}", '${text}' in args.get("old_string", ""), f"old_string: {args.get('old_string', '')!r}")
    check("old_string has extra )", args.get("old_string", "").count(")") == 2, f"old_string: {args.get('old_string', '')!r}")
    check("new_string has one )", args.get("new_string", "").count(")") == 1, f"new_string: {args.get('new_string', '')!r}")


# ---- Test 7: Multiple spaces around colon in key-value pairs ----
print("\n=== Test 7: Key-value with extra spaces (should be normalized) ===")
# This is the INTENDED use case for _clean_json_string:
# {"name" : "Edit"} should become {"name":"Edit"}
# But only for KEYS, not for string VALUES
raw7 = '{"name" : "Edit", "arguments" : {"file_path" : "/src/file.ts"}}'

tc = _extract_tool_call(raw7)
check("Spaced keys parsed", tc is not None, "Failed to parse")
if tc:
    check("Tool name is Edit", tc["function"]["name"] == "Edit", f"Name: {tc['function']['name']}")


print(f"\n{'='*60}")
print(f"Results: {passed} passed, {failed} failed, {passed+failed} total")
if failed:
    sys.exit(1)
