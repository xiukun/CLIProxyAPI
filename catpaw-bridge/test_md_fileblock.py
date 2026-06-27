#!/usr/bin/env python3
"""Test markdown code block with filename parsing."""

import json
import sys
sys.path.insert(0, ".")
from proxy.toolcall import _parse_tool_calls


def test_markdown_file_block():
    """Model outputs ```lang:filepath\n<content>``` instead of <tool_call>."""
    content = (
        '让我直接开始创建文件。\n\n'
        '```typescript:packages/api/src/procedures/platform-models.ts\n'
        "import { eq, like, or } from '@maita-orag/db'\n"
        "import { z } from 'zod'\n\n"
        'export const platformModelRouter = {\n'
        '  list: {},\n'
        '  create: {},\n'
        '}\n'
        '```\n\n'
        '接下来注册路由。'
    )
    clean, tcs = _parse_tool_calls(content)
    assert len(tcs) == 1, f"Expected 1 tool call, got {len(tcs)}"
    tc = tcs[0]
    assert tc["function"]["name"] == "Write", f"Expected Write, got {tc['function']['name']}"
    args = json.loads(tc["function"]["arguments"])
    assert args["file_path"] == "packages/api/src/procedures/platform-models.ts"
    assert "import { eq, like, or }" in args["content"]
    assert "接下来注册路由。" in clean
    print("PASS: markdown file block parsed as Write call")


def test_regular_code_block_not_parsed():
    """Regular ```python without filepath should NOT be parsed as tool call."""
    content = 'Here is an example:\n\n```python\nprint("hello")\n```\n\nDone.'
    clean, tcs = _parse_tool_calls(content)
    assert len(tcs) == 0, f"Expected 0 tool calls, got {len(tcs)}"
    print("PASS: regular code block not parsed as tool call")


def test_multiple_file_blocks():
    """Multiple ```lang:filepath blocks should all be parsed."""
    content = (
        'Creating two files:\n\n'
        '```typescript:src/index.ts\n'
        'console.log("hello");\n'
        '```\n\n'
        '```python:scripts/setup.py\n'
        'print("setup")\n'
        '```\n'
    )
    clean, tcs = _parse_tool_calls(content)
    assert len(tcs) == 2, f"Expected 2 tool calls, got {len(tcs)}"
    paths = []
    for tc in tcs:
        assert tc["function"]["name"] == "Write"
        args = json.loads(tc["function"]["arguments"])
        paths.append(args["file_path"])
    assert "src/index.ts" in paths
    assert "scripts/setup.py" in paths
    print("PASS: multiple file blocks parsed correctly")


def test_file_block_with_spaces_in_path():
    """Filepath with spaces should work."""
    content = (
        '```text:docs/My Plan.md\n'
        '# My Plan\n\n'
        'Step 1: Do something\n'
        '```\n'
    )
    clean, tcs = _parse_tool_calls(content)
    assert len(tcs) == 1, f"Expected 1 tool call, got {len(tcs)}"
    args = json.loads(tcs[0]["function"]["arguments"])
    assert args["file_path"] == "docs/My Plan.md"
    assert "# My Plan" in args["content"]
    print("PASS: filepath with spaces")


if __name__ == "__main__":
    test_markdown_file_block()
    test_regular_code_block_not_parsed()
    test_multiple_file_blocks()
    test_file_block_with_spaces_in_path()
    print("\n=== All tests passed ===")
