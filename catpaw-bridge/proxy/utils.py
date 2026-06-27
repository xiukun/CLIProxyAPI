"""Shared utility functions."""

from typing import Any


def _extract_text_content(content: Any) -> str:
    """Extract text from OpenAI message content (string or multimodal array)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(part.get("text", ""))
        return "\n".join(text_parts)
    return str(content) if content else ""
