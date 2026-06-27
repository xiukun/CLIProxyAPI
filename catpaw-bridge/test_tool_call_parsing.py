#!/usr/bin/env python3
"""
Regression tests for tool call parsing in catpaw_reverse_proxy.py.

Covers the 5 bug classes identified in the eng review:
  1. Balanced JSON with nested objects/arrays
  2. Missing closing tag (stream interrupted)
  3. </think> used as closing tag instead of </tool_call>
  4. Multiple tool calls in one response
  5. Markdown code block format fallback

Also tests _strip_agent_xml and _find_balanced_json helpers.
"""

import json
import sys
import os
import unittest

# Add the bridge directory to sys.path so we can import the module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import the proxy module directly.
# The module does config loading + RSA key extraction on import, but both
# are wrapped in try/except and have defaults, so import should succeed
# even without a real environment.
# Import the toolcall module directly from the proxy package.
# The package does config loading + RSA key extraction on import, but both
# are wrapped in try/except and have defaults, so import should succeed
# even without a real environment.
from proxy.toolcall import (
    _find_balanced_json,
    _extract_tool_call,
    _find_tag_tool_calls,
    _parse_tool_calls,
    _strip_agent_xml,
)


class TestFindBalancedJson(unittest.TestCase):
    """Tests for _find_balanced_json — the foundation of tool call parsing."""

    def test_simple_object(self):
        text = '{"name": "test"}'
        result, end = _find_balanced_json(text, 0)
        self.assertEqual(result, '{"name": "test"}')
        self.assertEqual(end, len(text))

    def test_nested_object(self):
        text = '{"name": "test", "arguments": {"key": {"nested": true}}}'
        result, end = _find_balanced_json(text, 0)
        self.assertEqual(result, '{"name": "test", "arguments": {"key": {"nested": true}}}')
        self.assertEqual(end, len(text))

    def test_object_with_array(self):
        text = '{"name": "test", "args": [1, 2, {"x": 3}]}'
        result, end = _find_balanced_json(text, 0)
        self.assertEqual(result, '{"name": "test", "args": [1, 2, {"x": 3}]}')
        self.assertEqual(end, len(text))

    def test_string_with_braces(self):
        text = '{"name": "test", "code": "if (x) { return 1; }"}'
        result, end = _find_balanced_json(text, 0)
        self.assertEqual(result, '{"name": "test", "code": "if (x) { return 1; }"}')
        self.assertEqual(end, len(text))

    def test_string_with_escaped_quotes(self):
        text = r'{"name": "test", "path": "C:\\Users\\file.txt"}'
        result, end = _find_balanced_json(text, 0)
        self.assertEqual(result, r'{"name": "test", "path": "C:\\Users\\file.txt"}')

    def test_incomplete_object(self):
        text = '{"name": "test"'
        result, end = _find_balanced_json(text, 0)
        self.assertIsNone(result)
        self.assertEqual(end, 0)

    def test_start_not_brace(self):
        result, end = _find_balanced_json("hello", 0)
        self.assertIsNone(result)


class TestExtractToolCall(unittest.TestCase):
    """Tests for _extract_tool_call."""

    def test_basic_tool_call(self):
        json_str = '{"name": "read_file", "arguments": {"path": "/tmp/test.txt"}}'
        tc = _extract_tool_call(json_str)
        self.assertIsNotNone(tc)
        self.assertEqual(tc["function"]["name"], "read_file")
        args = json.loads(tc["function"]["arguments"])
        self.assertEqual(args["path"], "/tmp/test.txt")
        self.assertTrue(tc["id"].startswith("call_"))
        self.assertEqual(tc["type"], "function")

    def test_nested_arguments(self):
        json_str = '{"name": "execute", "arguments": {"cmd": "ls", "opts": {"flag": "-la", "dir": "/tmp"}}}'
        tc = _extract_tool_call(json_str)
        self.assertIsNotNone(tc)
        self.assertEqual(tc["function"]["name"], "execute")
        args = json.loads(tc["function"]["arguments"])
        self.assertEqual(args["opts"]["flag"], "-la")

    def test_no_name_returns_none(self):
        json_str = '{"arguments": {"key": "value"}}'
        tc = _extract_tool_call(json_str)
        self.assertIsNone(tc)

    def test_invalid_json_returns_none(self):
        tc = _extract_tool_call("not json at all")
        self.assertIsNone(tc)

    def test_arguments_as_string(self):
        json_str = '{"name": "test", "arguments": "already-a-string"}'
        tc = _extract_tool_call(json_str)
        self.assertIsNotNone(tc)
        self.assertEqual(tc["function"]["arguments"], "already-a-string")

    def test_arguments_as_number(self):
        json_str = '{"name": "test", "arguments": 42}'
        tc = _extract_tool_call(json_str)
        self.assertIsNotNone(tc)
        # Number gets JSON-encoded
        self.assertEqual(json.loads(tc["function"]["arguments"]), 42)


class TestFindTagToolCalls(unittest.TestCase):
    """Tests for _find_tag_tool_calls — covers Bug #1-3."""

    def test_simple_tool_call_tag(self):
        """Bug: basic <tool_call>...</tool_call> parsing."""
        content = '<tool_call>{"name": "test", "arguments": {}}</tool_call>'
        results = _find_tag_tool_calls(content, "tool_call")
        self.assertEqual(len(results), 1)
        start, end, tc = results[0]
        self.assertEqual(tc["function"]["name"], "test")
        self.assertEqual(end, len(content))

    def test_think_as_closing_tag(self):
        """Bug #3: model outputs </think> instead of </tool_call> as closing tag."""
        content = '<tool_call>{"name": "test", "arguments": {}}</think>'
        results = _find_tag_tool_calls(content, "tool_call")
        self.assertEqual(len(results), 1)
        start, end, tc = results[0]
        self.assertEqual(tc["function"]["name"], "test")
        self.assertEqual(end, len(content))

    def test_missing_closing_tag(self):
        """Bug #2: stream interrupted, no closing tag at all."""
        content = '<tool_call>{"name": "test", "arguments": {}}'
        results = _find_tag_tool_calls(content, "tool_call")
        self.assertEqual(len(results), 1)
        start, end, tc = results[0]
        self.assertEqual(tc["function"]["name"], "test")
        # end_pos should be at the JSON end (no closing tag consumed)
        self.assertEqual(end, len(content))

    def test_closing_tag_with_whitespace_gap(self):
        """Bug #5 (relaxed): whitespace between JSON and closing tag."""
        content = '<tool_call>{"name": "test", "arguments": {}}\n\n</tool_call>'
        results = _find_tag_tool_calls(content, "tool_call")
        self.assertEqual(len(results), 1)
        start, end, tc = results[0]
        self.assertEqual(tc["function"]["name"], "test")
        self.assertEqual(end, len(content))

    def test_nested_json_in_arguments(self):
        """Bug #1: nested objects in arguments should be parsed correctly."""
        content = '<tool_call>{"name": "edit", "arguments": {"file": "test.py", "changes": {"old": "foo", "new": "bar"}}}</tool_call>'
        results = _find_tag_tool_calls(content, "tool_call")
        self.assertEqual(len(results), 1)
        _, _, tc = results[0]
        args = json.loads(tc["function"]["arguments"])
        self.assertEqual(args["changes"]["old"], "foo")
        self.assertEqual(args["changes"]["new"], "bar")

    def test_multiple_tool_calls(self):
        """Bug #4: multiple <tool_call> blocks in one response."""
        content = (
            '<tool_call>{"name": "read_file", "arguments": {"path": "/a"}}</tool_call>'
            ' some text between '
            '<tool_call>{"name": "write_file", "arguments": {"path": "/b"}}</tool_call>'
        )
        results = _find_tag_tool_calls(content, "tool_call")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0][2]["function"]["name"], "read_file")
        self.assertEqual(results[1][2]["function"]["name"], "write_file")

    def test_text_before_tool_call(self):
        """Text before the tool call tag should not interfere."""
        content = 'I will read the file now.\n<tool_call>{"name": "read", "arguments": {}}</tool_call>'
        results = _find_tag_tool_calls(content, "tool_call")
        self.assertEqual(len(results), 1)
        start, _, tc = results[0]
        self.assertEqual(content[start:start + 11], "<tool_call>")
        self.assertEqual(tc["function"]["name"], "read")

    def test_tool_use_tag(self):
        """Legacy <tool_use> tag format."""
        content = '<tool_use>{"name": "test", "arguments": {}}</tool_use>'
        results = _find_tag_tool_calls(content, "tool_use")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][2]["function"]["name"], "test")


class TestParseToolCalls(unittest.TestCase):
    """Tests for _parse_tool_calls — the top-level parser covering all formats."""

    def test_format1_tool_call_tag(self):
        """Format 1: <tool_call>...</tool_call>"""
        content = '<tool_call>{"name": "test_tool", "arguments": {"key": "value"}}</tool_call>'
        text, tcs = _parse_tool_calls(content)
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["function"]["name"], "test_tool")
        self.assertEqual(text, "")  # no remaining text

    def test_format1_with_preamble_text(self):
        """Text before tool call should be preserved."""
        content = 'Let me check that.\n<tool_call>{"name": "test", "arguments": {}}</tool_call>'
        text, tcs = _parse_tool_calls(content)
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["function"]["name"], "test")
        self.assertIn("Let me check that", text)

    def test_format2_tool_use_tag(self):
        """Format 2: <tool_use>...</tool_use>"""
        content = '<tool_use>{"name": "test", "arguments": {}}</tool_use>'
        text, tcs = _parse_tool_calls(content)
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["function"]["name"], "test")

    def test_format3_function_calls(self):
        """Format 3: <function_calls><invoke name="..."><parameter>...</parameter></invoke></function_calls>"""
        content = '<function_calls><invoke name="read_file"><parameter name="path">/tmp/test.txt</parameter></invoke></function_calls>'
        text, tcs = _parse_tool_calls(content)
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["function"]["name"], "read_file")
        args = json.loads(tcs[0]["function"]["arguments"])
        self.assertEqual(args["path"], "/tmp/test.txt")

    def test_format4_markdown_code_block(self):
        """Format 4: ```json {...} ``` code blocks."""
        content = '```json\n{"name": "test", "arguments": {"key": "value"}}\n```'
        text, tcs = _parse_tool_calls(content)
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["function"]["name"], "test")

    def test_no_tool_calls(self):
        """Plain text with no tool calls."""
        content = "This is just a normal response with no tool calls."
        text, tcs = _parse_tool_calls(content)
        self.assertEqual(len(tcs), 0)
        self.assertEqual(text, content)

    def test_empty_content(self):
        """Empty string input."""
        text, tcs = _parse_tool_calls("")
        self.assertEqual(text, "")
        self.assertEqual(len(tcs), 0)

    def test_think_closing_tag(self):
        """Bug #3: model uses </think> as closing tag."""
        content = '<tool_call>{"name": "test", "arguments": {}}</think>'
        text, tcs = _parse_tool_calls(content)
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["function"]["name"], "test")

    def test_multiple_tool_calls_format1(self):
        """Multiple tool calls — only the FIRST is returned (sequential execution)."""
        content = (
            '<tool_call>{"name": "tool_a", "arguments": {"x": 1}}</tool_call>\n'
            '<tool_call>{"name": "tool_b", "arguments": {"y": 2}}</tool_call>'
        )
        text, tcs = _parse_tool_calls(content)
        # Only the first tool call is returned (model should do one at a time)
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["function"]["name"], "tool_a")
        # Remaining <tool_call> tags should be stripped from text
        self.assertNotIn("<tool_call>", text)

    def test_nested_json_arguments(self):
        """Bug #1: deeply nested arguments."""
        content = '<tool_call>{"name": "complex_tool", "arguments": {"config": {"nested": {"deep": {"value": [1, 2, 3]}}}}}</tool_call>'
        text, tcs = _parse_tool_calls(content)
        self.assertEqual(len(tcs), 1)
        args = json.loads(tcs[0]["function"]["arguments"])
        self.assertEqual(args["config"]["nested"]["deep"]["value"], [1, 2, 3])

    def test_string_with_special_chars(self):
        """Arguments containing braces in string values."""
        content = '<tool_call>{"name": "search", "arguments": {"pattern": "function() { return true; }"}}</tool_call>'
        text, tcs = _parse_tool_calls(content)
        self.assertEqual(len(tcs), 1)
        args = json.loads(tcs[0]["function"]["arguments"])
        self.assertEqual(args["pattern"], "function() { return true; }")

    def test_missing_closing_tag_no_crash(self):
        """Bug #2: missing closing tag should not crash."""
        content = '<tool_call>{"name": "test", "arguments": {}}'
        text, tcs = _parse_tool_calls(content)
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["function"]["name"], "test")


class TestStripAgentXml(unittest.TestCase):
    """Tests for _strip_agent_xml."""

    def test_strip_function_calls(self):
        content = '<function_calls><invoke name="test"><parameter name="x">1</parameter></invoke></function_calls>Result here.'
        result = _strip_agent_xml(content)
        self.assertNotIn("<function_calls>", result)
        self.assertIn("Result here", result)

    def test_strip_antThinking(self):
        content = '<antThinking>Let me think about this...</antThinking>Final answer.'
        result = _strip_agent_xml(content)
        self.assertNotIn("<antThinking>", result)
        self.assertIn("Final answer", result)

    def test_strip_plan(self):
        content = '<plan>Step 1: Do X</plan>Executing...'
        result = _strip_agent_xml(content)
        self.assertNotIn("<plan>", result)
        self.assertIn("Executing...", result)

    def test_orphaned_tags(self):
        content = '<function_calls>Some text</function_calls>'
        result = _strip_agent_xml(content)
        self.assertNotIn("<function_calls>", result)
        self.assertNotIn("</function_calls>", result)

    def test_empty_content(self):
        self.assertEqual(_strip_agent_xml(""), "")
        self.assertEqual(_strip_agent_xml(None), None)

    def test_no_tags(self):
        content = "Just normal text with no XML tags."
        self.assertEqual(_strip_agent_xml(content), content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
