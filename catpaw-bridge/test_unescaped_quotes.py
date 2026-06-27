#!/usr/bin/env python3
"""Test unescaped quotes in JSON and multi-tool-call handling."""
import json
import sys
sys.path.insert(0, ".")
from proxy.toolcall import _parse_tool_calls, _extract_tool_call, _fix_unescaped_quotes_in_json

passed = 0
failed = 0

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name} {detail}")


# ---- Test 1: Bash command with unescaped *.ts quotes ----
content = '<tool_call>{"name":"Bash","arguments":{"command":"find . -name "*.ts" | head -20","description":"find ts files"}}</tool_call>'
clean, tcs = _parse_tool_calls(content)
check("Unescaped quotes: 1 tool call found", len(tcs) == 1, f"got {len(tcs)}")
if tcs:
    args = json.loads(tcs[0]["function"]["arguments"])
    check("Unescaped quotes: command correct", "*.ts" in args.get("command", ""), f"command: {args.get('command','')}")
    check("Unescaped quotes: name is Bash", tcs[0]["function"]["name"] == "Bash")


# ---- Test 2: Multiple tool calls — only first should be returned ----
content2 = (
    '<tool_call>{"name":"Bash","arguments":{"command":"ls"}}</tool_call>\n'
    '◯ Goal not yet met… continuing\n'
    '<tool_call>{"name":"Read","arguments":{"file_path":"/foo"}}</tool_call>\n'
    '◯ Goal not yet met… continuing\n'
    '<tool_call>{"name":"Bash","arguments":{"command":"pwd"}}</tool_call>'
)
clean2, tcs2 = _parse_tool_calls(content2)
check("Multi: only 1 tool call", len(tcs2) == 1, f"got {len(tcs2)}")
if tcs2:
    check("Multi: first is Bash", tcs2[0]["function"]["name"] == "Bash", f"got {tcs2[0]['function']['name']}")
check("Multi: no ◯ in clean_text", "◯" not in clean2, f"clean_text: {clean2[:100]}")
check("Multi: no <tool_call> in clean_text", "<tool_call>" not in clean2, f"clean_text: {clean2[:100]}")


# ---- Test 3: Multiple tool calls with unescaped quotes — only first kept, rest stripped ----
content3 = (
    'Let me check.\n'
    '<tool_call>{"name":"Bash","arguments":{"command":"find . -name "*.ts"}}</tool_call>\n'
    '◯ Goal not yet met… continuing\n'
    '<tool_call>{"name":"Read","arguments":{"file_path":"/foo"}}</tool_call>'
)
clean3, tcs3 = _parse_tool_calls(content3)
# Now that unescaped quotes are fixed, the first tool call IS parsed
check("Test3: 1 tool call (first only)", len(tcs3) == 1, f"got {len(tcs3)}")
check("Test3: no <tool_call> tags in clean", "<tool_call>" not in clean3, f"clean: {clean3[:100]}")
check("Test3: no ◯ in clean", "◯" not in clean3, f"clean: {clean3[:100]}")


# ---- Test 4: Multiple unescaped quotes ----
content4 = '<tool_call>{"name":"Bash","arguments":{"command":"find . -name "*.tsx" -o -name "*.ts" | grep -E "(model|kb)"","description":"search files"}}</tool_call>'
clean4, tcs4 = _parse_tool_calls(content4)
check("Multi unescaped: 1 tool call", len(tcs4) == 1, f"got {len(tcs4)}")
if tcs4:
    args4 = json.loads(tcs4[0]["function"]["arguments"])
    check("Multi unescaped: command has *.tsx", "*.tsx" in args4.get("command", ""), f"cmd: {args4.get('command','')}")


# ---- Test 5: _fix_unescaped_quotes_in_json directly ----
raw = '{"command":"find . -name "*.ts" | head"}'
fixed = _fix_unescaped_quotes_in_json(raw)
try:
    data = json.loads(fixed)
    check("Direct fix: parses OK", True)
    check("Direct fix: command correct", "*.ts" in data["command"], f"cmd: {data['command']}")
except json.JSONDecodeError as e:
    check("Direct fix: parses OK", False, f"error: {e}")


# ---- Test 6: Already-escaped quotes should not be double-escaped ----
raw6 = '{"command":"find . -name \\"*.ts\\" | head"}'
fixed6 = _fix_unescaped_quotes_in_json(raw6)
try:
    data6 = json.loads(fixed6)
    check("Already escaped: parses OK", True)
    check("Already escaped: command correct", "*.ts" in data6["command"], f"cmd: {data6['command']}")
except json.JSONDecodeError as e:
    check("Already escaped: parses OK", False, f"error: {e}, fixed: {fixed6}")


print(f"\n{'='*60}")
print(f"Results: {passed} passed, {failed} failed, {passed+failed} total")
sys.exit(0 if failed == 0 else 1)
