#!/usr/bin/env python3
"""
Tests for display sanitization — stripping Codex CLI internal metadata
from tool results and cleaning simulated tool results from model output.

Covers three layers of the 4-layer defense:
  1. _sanitize_tool_result_content — strips Codex CLI metadata (Chunk ID,
     Wall time, Process failed, Original token count, Output:)
  2. _clean_model_output_text — strips simulated "Tool Result:" blocks and
     residual bare JSON from model output
  3. _parse_tool_calls integration — verifies clean_text doesn't contain
     simulated tool results or bare JSON after parsing
"""

import json
import sys
import os
import unittest

# Add the bridge directory to sys.path so we can import the module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxy.toolcall import (
    _sanitize_tool_result_content,
    _clean_model_output_text,
    _parse_tool_calls,
)


class TestSanitizeToolResultContent(unittest.TestCase):
    """Tests for _sanitize_tool_result_content — Codex CLI metadata stripping."""

    def test_full_metadata_with_output(self):
        """Complete Codex CLI metadata block with actual output after 'Output:'."""
        content = (
            "Chunk ID: 382ad3\n"
            "Wall time:: 0.0000 seconds\n"
            "Process failed (exit code 1)\n"
            "Original token comm: 0\n"
            "Output:\n"
            "# CCG 规范系统\n\n> 本目录存储项目开发中沉淀的可复用经验。"
        )
        result = _sanitize_tool_result_content(content)
        self.assertEqual(result, "# CCG 规范系统\n\n> 本目录存储项目开发中沉淀的可复用经验。")

    def test_metadata_exited_with_code(self):
        """Variant: 'Process exited with code 0' instead of 'Process failed'."""
        content = (
            "Chunk ID: bac5a6\n"
            "Wall time: 0.0267 seconds\n"
            "Process exited with code 0\n"
            "Original token count: 451\n"
            "Output:\n"
            "commit c97ed14ccb2d5f84cb22ad1ccfad67ee496cb591\n"
            "Author: Test <test@example.com>"
        )
        result = _sanitize_tool_result_content(content)
        self.assertIn("commit c97ed14", result)
        self.assertIn("Author: Test", result)
        self.assertNotIn("Chunk ID", result)
        self.assertNotIn("Wall time", result)

    def test_nil_input(self):
        """None input should return None unchanged."""
        self.assertIsNone(_sanitize_tool_result_content(None))

    def test_empty_string(self):
        """Empty string should return empty string unchanged."""
        self.assertEqual(_sanitize_tool_result_content(""), "")

    def test_no_metadata(self):
        """Plain text without any Codex CLI metadata markers should pass through."""
        content = "This is a normal tool result without any metadata."
        result = _sanitize_tool_result_content(content)
        self.assertEqual(result, content)

    def test_only_metadata_no_output(self):
        """Metadata block with 'Output:' but no actual content after it."""
        content = (
            "Chunk ID: 382ad3\n"
            "Wall time:: 0.0000 seconds\n"
            "Process failed (exit code 1)\n"
            "Original token comm: 0\n"
            "Output:\n"
        )
        result = _sanitize_tool_result_content(content)
        self.assertEqual(result, "[Process completed, no output]")

    def test_short_content(self):
        """Content shorter than 20 chars should pass through without processing."""
        content = "Short"
        result = _sanitize_tool_result_content(content)
        self.assertEqual(result, "Short")

    def test_partial_metadata(self):
        """Only some metadata fields present — should strip what's there."""
        content = (
            "Chunk ID: abc123\n"
            "Wall time: 1.5 seconds\n"
            "Output:\n"
            "actual result line 1\n"
            "actual result line 2"
        )
        result = _sanitize_tool_result_content(content)
        self.assertEqual(result, "actual result line 1\nactual result line 2")

    def test_preserves_content_with_chunk_id_substring(self):
        """Content that contains 'Chunk ID:' as part of actual output (not metadata)."""
        content = "The log shows Chunk ID: abc in the output"
        # This content is < 20 chars? No, it's longer. But it doesn't start with
        # the metadata pattern at a line start, so it should be returned as-is
        # because there's no "Output:" separator and _RE_CODEX_METADATA_LINES
        # won't match "The log shows Chunk ID: abc in the output" (not at start of line).
        # Actually wait, the quick check just looks for "Chunk ID:" substring.
        # So it will enter the cleaning logic. But _RE_CODEX_OUTPUT_SEPARATOR
        # won't find "Output:" at start of line. And _RE_CODEX_METADATA_LINES
        # won't match because "Chunk ID:" is not at start of line.
        # So cleaned will be same as content.
        result = _sanitize_tool_result_content(content)
        self.assertEqual(result, content)


class TestCleanModelOutputText(unittest.TestCase):
    """Tests for _clean_model_output_text — simulated tool result stripping."""

    def test_simulated_tool_result_block(self):
        """Model outputs 'Tool Result:' at start of line — should be stripped."""
        text = (
            "Tool Result: Chunk ID: 382ad3\n"
            "Wall time:: 0.0000 seconds\n"
            "Process failed (exit code 1)\n"
            "Original token comm: 0\n"
            "Output:\n"
            "\n"
            '{"name":"exec_command","arguments":{"cmd":"ls"}}'
        )
        result = _clean_model_output_text(text)
        self.assertEqual(result, "")

    def test_text_followed_by_simulated_block(self):
        """Legitimate text before 'Tool Result:' should be preserved."""
        text = (
            "让我用 Read 工具查看关键文件：\n\n"
            "Tool Result: Chunk ID: 382ad3\n"
            "Wall time:: 0.0000 seconds\n"
            "Process failed (exit code 1)\n"
            "Original token comm: 0\n"
            "Output:\n"
        )
        result = _clean_model_output_text(text)
        self.assertEqual(result, "让我用 Read 工具查看关键文件：")

    def test_nil_input(self):
        """None input should return None unchanged."""
        self.assertIsNone(_clean_model_output_text(None))

    def test_empty_string(self):
        """Empty string should return empty string unchanged."""
        self.assertEqual(_clean_model_output_text(""), "")

    def test_no_simulation(self):
        """Normal text without any simulated content should pass through."""
        text = "I'll read the file and then make the edit."
        result = _clean_model_output_text(text)
        self.assertEqual(result, "I'll read the file and then make the edit.")

    def test_legitimate_reference_preserved(self):
        """In-sentence 'Tool Result' reference should NOT be stripped."""
        text = "The Tool Result shows that the file contains 100 lines."
        result = _clean_model_output_text(text)
        self.assertEqual(result, "The Tool Result shows that the file contains 100 lines.")

    def test_bare_json_stripped(self):
        """Residual bare JSON tool call should be stripped."""
        text = (
            'Some explanatory text\n\n'
            '{"name":"exec_command","arguments":{"cmd":"git status"}}'
        )
        result = _clean_model_output_text(text)
        self.assertEqual(result, "Some explanatory text")

    def test_multiple_bare_json_stripped(self):
        """Multiple residual bare JSON objects should all be stripped."""
        text = (
            '{"name":"Read","arguments":{"file_path":"./a.py"}}\n'
            '{"name":"Read","arguments":{"file_path":"./b.py"}}'
        )
        result = _clean_model_output_text(text)
        self.assertEqual(result, "")

    def test_mixed_content(self):
        """Text + simulated Tool Result + bare JSON — only text survives."""
        text = (
            "Let me check the files.\n\n"
            "Tool Result: Chunk ID: abc\n"
            "Wall time: 0.1 seconds\n"
            "Output:\n"
            "file content here\n\n"
            '{"name":"Read","arguments":{"file_path":"./next.py"}}'
        )
        result = _clean_model_output_text(text)
        self.assertEqual(result, "Let me check the files.")

    def test_status_narrative_stripped(self):
        """Chinese status narrative '已读取 N 个文件' should be stripped."""
        text = "已读取 3 个文件\nNow let me analyze them."
        result = _clean_model_output_text(text)
        self.assertEqual(result, "Now let me analyze them.")

    def test_whitespace_cleanup(self):
        """Excessive newlines left by removals should be cleaned up."""
        text = "Text\n\n\n\n\nMore text"
        result = _clean_model_output_text(text)
        self.assertEqual(result, "Text\n\nMore text")

    def test_tool_call_tags_stripped(self):
        """Remaining <tool_call> blocks should be fully stripped (including content)."""
        text = "Here is what I'll do:\n<tool_call>some content</tool_call>\nDone."
        result = _clean_model_output_text(text)
        self.assertEqual(result, "Here is what I'll do:\n\nDone.")


class TestParseToolCallsIntegration(unittest.TestCase):
    """Integration tests — _parse_tool_calls should return clean clean_text."""

    def test_first_tool_call_with_simulated_result(self):
        """Model outputs tool call + simulated Tool Result + second tool call.

        Only the first tool call should be extracted. The clean_text should
        NOT contain the simulated 'Tool Result:' block or the second tool call's
        bare JSON.
        """
        content = (
            '<tool_call>{"name":"exec_command","arguments":{"cmd":"cat file1"}}</tool_call>\n\n'
            'Tool Result: Chunk ID: 382ad3\n'
            'Wall time:: 0.0000 seconds\n'
            'Process failed (exit code 1)\n'
            'Original token comm: 0\n'
            'Output:\n\n'
            '<tool_call>{"name":"exec_command","arguments":{"cmd":"cat file2"}}</tool_call>'
        )
        clean_text, tool_calls = _parse_tool_calls(content)

        # Should extract exactly 1 tool call
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["function"]["name"], "exec_command")

        # clean_text should NOT contain simulated Tool Result
        self.assertNotIn("Tool Result:", clean_text)
        self.assertNotIn("Chunk ID", clean_text)
        self.assertNotIn("Wall time", clean_text)

        # clean_text should NOT contain bare JSON from second tool call
        self.assertNotIn('"name":"exec_command"', clean_text)

    def test_intro_text_preserved_with_tool_call(self):
        """Text before the first tool call should be preserved in clean_text."""
        content = (
            '让我用 Read 工具查看关键文件：\n\n'
            '<tool_call>{"name":"exec_command","arguments":{"cmd":"cat file"}}</tool_call>'
        )
        clean_text, tool_calls = _parse_tool_calls(content)

        self.assertEqual(len(tool_calls), 1)
        self.assertIn("让我用 Read 工具查看关键文件", clean_text)
        self.assertNotIn("Tool Result:", clean_text)

    def test_bare_json_with_simulated_result(self):
        """Bare JSON tool call followed by simulated Tool Result."""
        content = (
            '{"name":"exec_command","arguments":{"cmd":"git status"}}\n\n'
            'Tool Result: Chunk ID: abc123\n'
            'Wall time: 0.5 seconds\n'
            'Process exited with code 0\n'
            'Original token count: 100\n'
            'Output:\n'
            'On branch main'
        )
        clean_text, tool_calls = _parse_tool_calls(content)

        # Should extract the bare JSON tool call
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["function"]["name"], "exec_command")

        # clean_text should NOT contain the simulated Tool Result
        self.assertNotIn("Tool Result:", clean_text)
        self.assertNotIn("Chunk ID", clean_text)
        self.assertNotIn("On branch main", clean_text)

    def test_no_tool_calls_strips_simulated_result(self):
        """When no tool calls are found, simulated Tool Result should still be stripped."""
        content = (
            "I'll check the file now.\n\n"
            "Tool Result: Chunk ID: fake\n"
            "Wall time: 0.0 seconds\n"
            "Output:\n"
            "fake content"
        )
        clean_text, tool_calls = _parse_tool_calls(content)

        self.assertEqual(len(tool_calls), 0)
        self.assertIn("I'll check the file now.", clean_text)
        self.assertNotIn("Tool Result:", clean_text)
        self.assertNotIn("Chunk ID", clean_text)


if __name__ == "__main__":
    unittest.main()
