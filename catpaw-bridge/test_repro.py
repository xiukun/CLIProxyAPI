#!/usr/bin/env python3
"""Reproduce the exact parsing failure from user report."""

import sys
sys.path.insert(0, ".")

from proxy.toolcall import (
    _find_balanced_json,
    _extract_tool_call,
    _find_bare_json_tool_call,
    _parse_tool_calls,
    _escape_raw_newlines_in_strings,
    _fix_invalid_json_escapes,
)

# Exact content from user report - with ACTUAL newlines in cmd value
# The model outputs raw newlines inside JSON string values (invalid JSON)
content = (
    '{"name":"exec_command","arguments":{"cmd": "sed -i \'\' \'s/export interface ChunkRow {/export interface ChunkRow {\n'
    'id: string\n'
    'chunkType: \'\'\'parent\'\'\' | \'\'\'child\'\'\'\n'
    'content: string\n'
    'ordinal: number\n'
    'tokenCount: number | null\n'
    'parentOrdinal?: number\n'
    'document_id: string\n'
    'kb_id: string\n'
    'tenant_id: string\n'
    'metadata: string\n'
    'hash: string\n'
    'parent_id: string | null\n'
    '}/\' src/domain/ports/repositories.ts", "workdir": "/Users/mac/Documents/GitHub/maita-orag/packages/rag"}}'
)

print("Content length:", len(content))
print("Has actual newlines:", "\n" in content)
print("Has triple quotes:", "'''" in content)
print("Has { inside cmd:", "ChunkRow {" in content)
print()

# Test 1: _find_balanced_json
print("=== Test 1: _find_balanced_json ===")
js, end = _find_balanced_json(content, 0)
if js:
    print(f"  FOUND: {len(js)} chars (expected {len(content)})")
    if len(js) < len(content):
        print(f"  TRUNCATED! Missing {len(content) - len(js)} chars")
        print(f"  Last 80 chars of found JSON: ...{js[-80:]}")
        print(f"  Next 80 chars after found JSON: {content[end:end+80]}")
else:
    print("  FAILED (returned None)")
print()

# Test 2: _extract_tool_call on the found JSON
if js:
    print("=== Test 2: _extract_tool_call ===")
    tc = _extract_tool_call(js)
    print(f"  Result: {'OK' if tc else 'FAILED'}")
    if tc:
        print(f"  Tool: {tc['function']['name']}")
    print()

# Test 3: _find_bare_json_tool_call
print("=== Test 3: _find_bare_json_tool_call ===")
result = _find_bare_json_tool_call(content)
print(f"  Result: {'FOUND' if result else 'NOT FOUND'}")
if result:
    print(f"  Tool: {result[0]['function']['name']}")
print()

# Test 4: _parse_tool_calls
print("=== Test 4: _parse_tool_calls ===")
clean, calls = _parse_tool_calls(content)
print(f"  Result: {len(calls)} calls, clean_text={len(clean)} chars")
if not calls:
    print(f"  Clean text preview: {clean[:100]}...")
print()

# Test 5: Debug - try each strategy in _extract_tool_call manually
if js:
    print("=== Test 5: Debug _extract_tool_call strategies ===")
    import json

    # Strategy 1: raw
    try:
        json.loads(js)
        print("  S1 (raw): OK")
    except Exception as e:
        print(f"  S1 (raw): FAIL - {e}")

    # Strategy 2: fix invalid escapes
    fixed_esc = _fix_invalid_json_escapes(js)
    try:
        json.loads(fixed_esc)
        print("  S2 (fix escapes): OK")
    except Exception as e:
        print(f"  S2 (fix escapes): FAIL - {e}")

    # Strategy 3: escape raw newlines
    fixed_nl = _escape_raw_newlines_in_strings(fixed_esc)
    try:
        json.loads(fixed_nl)
        print("  S3 (escape newlines): OK")
    except Exception as e:
        print(f"  S3 (escape newlines): FAIL - {e}")

    # Strategy 4: fix unescaped quotes
    from proxy.toolcall import _fix_unescaped_quotes_in_json
    fixed_q = _fix_unescaped_quotes_in_json(fixed_nl)
    try:
        json.loads(fixed_q)
        print("  S4 (fix quotes): OK")
    except Exception as e:
        print(f"  S4 (fix quotes): FAIL - {e}")
