#!/usr/bin/env python3
"""Diagnose Write/Edit/MultiEdit tool call parsing issues."""
import json
import sys
sys.path.insert(0, '.')

from proxy.toolcall import _parse_tool_calls, _extract_tool_call, _clean_json_string, _find_balanced_json

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

print("=" * 60)
print("WRITE TOOL CALL TESTS")
print("=" * 60)

# ---- Test 1: Simple Write call ----
test1 = '<tool_call>{"name":"Write","arguments":{"file_path":"/tmp/test.txt","content":"Hello World"}}</tool_call>'
text, calls = _parse_tool_calls(test1)
check("Simple Write", len(calls) == 1 and calls[0]['function']['name'] == 'Write', f"calls={len(calls)}")
if calls:
    args = json.loads(calls[0]['function']['arguments'])
    check("  file_path", args.get('file_path') == '/tmp/test.txt')
    check("  content", args.get('content') == 'Hello World')

# ---- Test 2: Write with multi-line content (escaped newlines) ----
test2 = '<tool_call>{"name":"Write","arguments":{"file_path":"/tmp/test.py","content":"import os\\n\\ndef main():\\n    print(\\"hello\\")\\n"}}</tool_call>'
text, calls = _parse_tool_calls(test2)
check("Write multi-line (escaped)", len(calls) == 1, f"calls={len(calls)}")
if calls:
    args = json.loads(calls[0]['function']['arguments'])
    check("  name", calls[0]['function']['name'] == 'Write')
    check("  content has newlines", '\n' in args.get('content', ''), f"content={args.get('content', '')[:50]!r}")

# ---- Test 3: Write with raw newlines in content (invalid JSON but model does this) ----
test3 = '<tool_call>{"name":"Write","arguments":{"file_path":"/tmp/test.py","content":"line1\nline2\nline3"}}</tool_call>'
text, calls = _parse_tool_calls(test3)
check("Write raw newlines", len(calls) == 1, f"calls={len(calls)}")
if calls:
    args = json.loads(calls[0]['function']['arguments'])
    check("  content correct", 'line1' in args.get('content', '') and 'line2' in args.get('content', ''))

# ---- Test 4: Write with content containing quotes ----
test4 = '<tool_call>{"name":"Write","arguments":{"file_path":"/tmp/test.py","content":"x = \\"hello world\\"\\nprint(x)"}}</tool_call>'
text, calls = _parse_tool_calls(test4)
check("Write with quotes", len(calls) == 1, f"calls={len(calls)}")
if calls:
    args = json.loads(calls[0]['function']['arguments'])
    check("  content has quotes", '\\"' in args.get('content', '') or 'hello world' in args.get('content', ''))

# ---- Test 5: Write with content containing colons ----
test5 = '<tool_call>{"name":"Write","arguments":{"file_path":"/tmp/test.json","content":"{\\"key\\": \\"value\\",\\"nested\\": {\\"a\\": \\"b\\"}}"}}</tool_call>'
text, calls = _parse_tool_calls(test5)
check("Write with colons in content", len(calls) == 1, f"calls={len(calls)}")
if calls:
    args = json.loads(calls[0]['function']['arguments'])
    content = args.get('content', '')
    check("  content preserved", 'key' in content and 'value' in content, f"content={content[:80]!r}")

# ---- Test 5b: CRITICAL - _clean_json_string doesn't corrupt content with ": " ----
raw_json = '{"name":"Write","arguments":{"content":"line with \\"key\\": \\"value\\" end"}}'
cleaned = _clean_json_string(raw_json)
try:
    data_orig = json.loads(raw_json)
    data_clean = json.loads(cleaned)
    check("_clean_json_string preserves content", 
          data_orig.get('arguments', {}).get('content') == data_clean.get('arguments', {}).get('content'),
          f"orig={data_orig.get('arguments', {}).get('content', '')!r}\nclean={data_clean.get('arguments', {}).get('content', '')!r}")
except json.JSONDecodeError as e:
    check("_clean_json_string preserves content", False, f"JSON parse error: {e}")

# ---- Test 6: Edit tool call ----
test6 = '<tool_call>{"name":"Edit","arguments":{"file_path":"/tmp/test.py","old_string":"print(\\"hello\\")","new_string":"print(\\"goodbye\\")"}}</tool_call>'
text, calls = _parse_tool_calls(test6)
check("Edit tool call", len(calls) == 1 and calls[0]['function']['name'] == 'Edit', f"calls={len(calls)}")
if calls:
    args = json.loads(calls[0]['function']['arguments'])
    check("  old_string", 'hello' in args.get('old_string', ''))
    check("  new_string", 'goodbye' in args.get('new_string', ''))

# ---- Test 7: MultiEdit tool call ----
test7 = '<tool_call>{"name":"MultiEdit","arguments":{"file_path":"/tmp/test.py","edits":[{"old_string":"a","new_string":"b"},{"old_string":"c","new_string":"d"}]}}</tool_call>'
text, calls = _parse_tool_calls(test7)
check("MultiEdit tool call", len(calls) == 1 and calls[0]['function']['name'] == 'MultiEdit', f"calls={len(calls)}")
if calls:
    args = json.loads(calls[0]['function']['arguments'])
    check("  edits array", isinstance(args.get('edits'), list) and len(args.get('edits', [])) == 2)

# ---- Test 8: Write with very large content ----
large_content = "x" * 5000
test8 = f'<tool_call>{{"name":"Write","arguments":{{"file_path":"/tmp/big.txt","content":"{large_content}"}}}}</tool_call>'
text, calls = _parse_tool_calls(test8)
check("Write large content", len(calls) == 1, f"calls={len(calls)}")
if calls:
    args = json.loads(calls[0]['function']['arguments'])
    check("  content size", len(args.get('content', '')) == 5000)

# ---- Test 9: Write with special chars (backslash, tabs, etc.) ----
test9 = '<tool_call>{"name":"Write","arguments":{"file_path":"/tmp/test.txt","content":"tab\\there\\nback\\\\slash"}}</tool_call>'
text, calls = _parse_tool_calls(test9)
check("Write special chars", len(calls) == 1, f"calls={len(calls)}")
if calls:
    args = json.loads(calls[0]['function']['arguments'])
    content = args.get('content', '')
    check("  tab preserved", '\t' in content)
    check("  backslash preserved", '\\' in content)

# ---- Test 10: Multiple tool calls — only first is returned ----
test10 = (
'<tool_call>{"name":"Read","arguments":{"file_path":"/src/a.py"}}</tool_call>'
'\nSome text\n'
'<tool_call>{"name":"Write","arguments":{"file_path":"/dst/b.py","content":"print(1)"}}</tool_call>'
)
text, calls = _parse_tool_calls(test10)
check("Multiple calls: only first returned", len(calls) == 1, f"calls={len(calls)}")
if calls:
    check("  call 1 Read", calls[0]['function']['name'] == 'Read')
    check("  no <tool_call> in text", '<tool_call>' not in text)

# ---- Test 11: Write with content containing closing braces ----
test11 = '<tool_call>{"name":"Write","arguments":{"file_path":"/tmp/test.js","content":"function foo() { return { a: 1, b: 2 }; }"}}</tool_call>'
text, calls = _parse_tool_calls(test11)
check("Write with braces in content", len(calls) == 1, f"calls={len(calls)}")
if calls:
    args = json.loads(calls[0]['function']['arguments'])
    content = args.get('content', '')
    check("  braces preserved", '{' in content and '}' in content and 'a: 1' in content)

# ---- Test 12: Write with content containing </tool_call> ----
test12 = '<tool_call>{"name":"Write","arguments":{"file_path":"/tmp/test.html","content":"<tool_call>fake</tool_call>"}}</tool_call>'
text, calls = _parse_tool_calls(test12)
check("Write with </tool_call> in content", len(calls) == 1, f"calls={len(calls)}")
if calls:
    args = json.loads(calls[0]['function']['arguments'])
    check("  content has tool_call tag", 'tool_call' in args.get('content', ''))

# ---- Test 13: Bash with command containing special chars ----
test13 = '<tool_call>{"name":"Bash","arguments":{"command":"echo \\"hello\\" | grep \\"h\\" && rm -rf /tmp/test"}}}</tool_call>'
text, calls = _parse_tool_calls(test13)
check("Bash with special chars", len(calls) == 1, f"calls={len(calls)}")

# ---- Test 14: delete_file tool call ----
test14 = '<tool_call>{"name":"delete_file","arguments":{"target_file":"/tmp/test.txt","explanation":"cleanup"}}}</tool_call>'
text, calls = _parse_tool_calls(test14)
check("delete_file tool call", len(calls) == 1 and calls[0]['function']['name'] == 'delete_file', f"calls={len(calls)}")
if calls:
    args = json.loads(calls[0]['function']['arguments'])
    check("  target_file", args.get('target_file') == '/tmp/test.txt')

print()
print("=" * 60)
print("EDGE CASES: Model output formats for Write")
print("=" * 60)

# ---- Test 15: Model outputs Write as function-call syntax ----
test15 = '<tool_call>Write(file_path="/tmp/test.txt", content="Hello World")</tool_call>'
text, calls = _parse_tool_calls(test15)
check("Write func-call syntax", len(calls) == 1, f"calls={len(calls)}")
if calls:
    check("  name Write", calls[0]['function']['name'] == 'Write')
    args = json.loads(calls[0]['function']['arguments'])
    check("  file_path", args.get('file_path') == '/tmp/test.txt')
    check("  content", args.get('content') == 'Hello World')

# ---- Test 16: Model outputs Write as space-separated ----
test16 = '<tool_call>Write file_path="/tmp/test.txt" content="Hello World"</tool_call>'
text, calls = _parse_tool_calls(test16)
check("Write space-separated", len(calls) == 1, f"calls={len(calls)}")
if calls:
    check("  name Write", calls[0]['function']['name'] == 'Write')

# ---- Test 17: Write func-call with multi-line content (breaks on commas in content) ----
test17 = '<tool_call>Write(file_path="/tmp/test.py", content="line1\\nline2, line3")</tool_call>'
text, calls = _parse_tool_calls(test17)
check("Write func-call with comma in content", len(calls) == 1, f"calls={len(calls)}")
if calls:
    args = json.loads(calls[0]['function']['arguments'])
    check("  content has comma", ',' in args.get('content', ''), f"content={args.get('content', '')!r}")

# ---- Test 18: Write func-call with quotes in content (BREAKS!) ----
test18 = '<tool_call>Write(file_path="/tmp/test.py", content="x = \\"hello\\"")</tool_call>'
text, calls = _parse_tool_calls(test18)
check("Write func-call with quotes in content", len(calls) == 1, f"calls={len(calls)}")
if calls:
    args = json.loads(calls[0]['function']['arguments'])
    check("  content has quotes", 'hello' in args.get('content', ''), f"content={args.get('content', '')!r}")

# ---- Test 19: Write with content containing unicode ----
test19 = '<tool_call>{"name":"Write","arguments":{"file_path":"/tmp/test.txt","content":"Hello 世界 \\n \\"emoji\\" test"}}</tool_call>'
text, calls = _parse_tool_calls(test19)
check("Write with unicode", len(calls) == 1, f"calls={len(calls)}")
if calls:
    args = json.loads(calls[0]['function']['arguments'])
    check("  unicode preserved", '世界' in args.get('content', ''))

# ---- Test 20: Write with content that looks like JSON keys ----
test20 = '<tool_call>{"name":"Write","arguments":{"file_path":"/tmp/config.json","content":"{\\"name\\": \\"test\\", \\"value\\": 42}"}}</tool_call>'
text, calls = _parse_tool_calls(test20)
check("Write JSON config content", len(calls) == 1, f"calls={len(calls)}")
if calls:
    args = json.loads(calls[0]['function']['arguments'])
    content = args.get('content', '')
    check("  content is valid JSON", '"name"' in content and '"test"' in content, f"content={content!r}")

print(f"\n{'='*60}")
print(f"Results: {passed} passed, {failed} failed, {passed+failed} total")
sys.exit(0 if failed == 0 else 1)
