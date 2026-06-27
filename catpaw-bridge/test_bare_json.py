#!/usr/bin/env python3
"""Test bare JSON tool call parsing (Format 6)."""

import json
import sys
sys.path.insert(0, '.')
from proxy.toolcall import _parse_tool_calls, _find_bare_json_tool_call

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


# ---- Test 1: Bare JSON Write call ----
content1 = 'Let me create the file.\n{"name":"Write","arguments":{"file_path":"/tmp/test.ts","content":"export const x = 1;"}}'
text1, tcs1 = _parse_tool_calls(content1)
check("Bare JSON: 1 tool call", len(tcs1) == 1, f"got {len(tcs1)}")
if tcs1:
    args1 = json.loads(tcs1[0]["function"]["arguments"])
    check("Bare JSON: name is Write", tcs1[0]["function"]["name"] == "Write")
    check("Bare JSON: file_path correct", args1.get("file_path") == "/tmp/test.ts")
    check("Bare JSON: content correct", args1.get("content") == "export const x = 1;")
check("Bare JSON: clean text has no JSON", '{"name"' not in text1)


# ---- Test 2: Bare JSON Read call ----
content2 = '{"name":"Read","arguments":{"file_path":"/src/main.py"}}'
text2, tcs2 = _parse_tool_calls(content2)
check("Bare JSON Read: 1 tool call", len(tcs2) == 1, f"got {len(tcs2)}")
if tcs2:
    check("Bare JSON Read: name is Read", tcs2[0]["function"]["name"] == "Read")


# ---- Test 3: Bare JSON with multi-line content (raw newlines) ----
content3 = '{"name":"Write","arguments":{"file_path":"/tmp/test.py","content":"import os\n\nprint(os.getcwd())"}}'
text3, tcs3 = _parse_tool_calls(content3)
check("Bare JSON multiline: 1 tool call", len(tcs3) == 1, f"got {len(tcs3)}")
if tcs3:
    args3 = json.loads(tcs3[0]["function"]["arguments"])
    check("Bare JSON multiline: content has newline", "\n" in args3.get("content", ""))


# ---- Test 4: Bare JSON Bash call with unescaped quotes ----
content4 = '{"name":"Bash","arguments":{"command":"find . -name "*.tsx" | grep foo"}}'
text4, tcs4 = _parse_tool_calls(content4)
check("Bare JSON unescaped: 1 tool call", len(tcs4) == 1, f"got {len(tcs4)}")
if tcs4:
    args4 = json.loads(tcs4[0]["function"]["arguments"])
    check("Bare JSON unescaped: command has *.tsx", "*.tsx" in args4.get("command", ""))


# ---- Test 5: Bare JSON should NOT match regular JSON in code blocks ----
content5 = 'Here is some JSON:\n```json\n{"name": "John", "age": 30}\n```\nThat was just data.'
text5, tcs5 = _parse_tool_calls(content5)
check("Code block JSON: no tool calls", len(tcs5) == 0, f"got {len(tcs5)}")


# ---- Test 6: Bare JSON should NOT match JSON without "arguments" key ----
content6 = 'Config: {"name": "config", "value": 42}'
text6, tcs6 = _parse_tool_calls(content6)
check("No arguments key: no tool calls", len(tcs6) == 0, f"got {len(tcs6)}")


# ---- Test 7: Bare JSON with MCP tool name (contains __) ----
content7 = '{"name":"mcp__code-review-graph__get_architecture_overview_tool","arguments":{"detail_level":"medium","repo_root":"/path"}}'
text7, tcs7 = _parse_tool_calls(content7)
check("MCP tool: 1 tool call", len(tcs7) == 1, f"got {len(tcs7)}")
if tcs7:
    check("MCP tool: name correct", tcs7[0]["function"]["name"] == "mcp__code-review-graph__get_architecture_overview_tool")


# ---- Test 8: Bare JSON embedded in text ----
content8 = 'I will now write the file.\n\n{"name":"Write","arguments":{"file_path":"/tmp/x.txt","content":"hello"}}\n\nDone.'
text8, tcs8 = _parse_tool_calls(content8)
check("Embedded bare JSON: 1 tool call", len(tcs8) == 1, f"got {len(tcs8)}")
if tcs8:
    args8 = json.loads(tcs8[0]["function"]["arguments"])
    check("Embedded: file_path correct", args8.get("file_path") == "/tmp/x.txt")
    check("Embedded: content correct", args8.get("content") == "hello")
check("Embedded: text preserved", "I will now write" in text8)
check("Embedded: JSON stripped", '{"name"' not in text8)


# ---- Test 9: Bare JSON with large content (the user's actual case) ----
big_content = "export interface EmbeddingModelConfig {\n  provider: 'openai' | 'anthropic'\n  model: string\n  baseUrl?: string\n  apiKey: string\n  dimensions?: number\n  batchSize?: number\n}\n"
content9 = f'让我检查子代理的执行状态并继续推进任务：\n\n{{"name":"Write","arguments":{{"file_path":"/Users/mac/Documents/GitHub/maita-orag/packages/embedding/src/types.ts","content":"{big_content}"}}}}'
text9, tcs9 = _parse_tool_calls(content9)
check("Large bare JSON: 1 tool call", len(tcs9) == 1, f"got {len(tcs9)}")
if tcs9:
    args9 = json.loads(tcs9[0]["function"]["arguments"])
    check("Large bare JSON: name is Write", tcs9[0]["function"]["name"] == "Write")
    check("Large bare JSON: file_path correct", "types.ts" in args9.get("file_path", ""))
    check("Large bare JSON: content has interface", "EmbeddingModelConfig" in args9.get("content", ""))


# ---- Test 10: Tag-based tool call takes priority over bare JSON ----
content10 = '<tool_call>{"name":"Read","arguments":{"file_path":"/a.py"}}</tool_call>\n{"name":"Write","arguments":{"file_path":"/b.py","content":"x"}}'
text10, tcs10 = _parse_tool_calls(content10)
check("Priority: tag-based wins", len(tcs10) == 1, f"got {len(tcs10)}")
if tcs10:
    check("Priority: first is Read", tcs10[0]["function"]["name"] == "Read")


print(f"\n{'='*60}")
print(f"Results: {passed} passed, {failed} failed, {passed+failed} total")
if failed:
    sys.exit(1)
