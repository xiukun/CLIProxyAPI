"""Tool Call Support: inject tool definitions, parse model output for tool calls.

Since CatPawAI API doesn't support function calling natively, we:
  1. Inject tool definitions as a text system prompt
  2. Convert tool-related messages (tool role, assistant tool_calls) to text
  3. Parse model output for <tool_call> tags containing JSON

Supported output formats (checked in priority order):
  1. <tool_call>{"name": "...", "arguments": {...}}</tool_call>
     - Also handles </think> as closing tag (model's actual behavior)
     - Also handles missing closing tag
  2. <tool_use>{"name": "...", "arguments": {...}}</tool_use> (legacy)
  3. <function_calls><invoke name="..."><parameter>...</parameter></invoke></function_calls>
  4. Markdown JSON code blocks containing tool call objects
  5. Markdown code blocks with filename (```lang:filepath) → Write calls
  6. Bare JSON: {"name":"ToolName","arguments":{...}} (no tags at all)
  7. ToolName<parameters>{"key":"value"}</parameters> (bare, no <tool_call> wrapping)
  8. <tool_call>ToolName<parameters>{...}</parameters></tool_call> (parameters inside tool_call)
  9. <tool_call>ToolName{"key":"value"}</tool_call> (name-JSON syntax)
  10. <tool_call>ToolName param="value"</tool_call> (space-separated syntax)
"""

import json
import re
import uuid

from proxy.config import VERBOSE
from proxy.utils import _extract_text_content


# ---------------------------------------------------------------------------
# Precompiled regex patterns
# ---------------------------------------------------------------------------

# Format 3: <function_calls><invoke name="..."><parameter>...</parameter></invoke></function_calls>
_RE_FUNCTION_CALLS = re.compile(r'<function_calls>\s*(.*?)\s*</function_calls>', re.DOTALL)
_RE_INVOKE = re.compile(r'<invoke\s+name="([^"]+)">(.*?)</invoke>', re.DOTALL)
_RE_PARAMETER = re.compile(r'<parameter\s+name="([^"]+)">(.*?)</parameter>', re.DOTALL)

# Format 5: <tool_call>FunctionName(param="value", param2="value2")</tool_call>
# The model sometimes outputs function-call syntax instead of JSON
_RE_FUNC_CALL_SYNTAX = re.compile(
    r'(\w+)\s*\((.*)\)\s*$',
    re.DOTALL
)
# KV pair regex that handles escaped quotes inside double-quoted strings
_RE_KV_PAIR = re.compile(r'(\w+)\s*=\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^,]+)')

# Format 6: <tool_call>ToolName param="value" param2="value2"</tool_call>
# Space-separated key=value pairs (no parentheses)
# Handles escaped quotes inside double-quoted strings
_RE_SPACE_KV = re.compile(r'(\w+)\s*=\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\S+)')

# Format 7: <tool_call>ToolName,"key":"value","key2":"value2"</tool_call>
# Comma-separated JSON fragments without braces (tool name as first element)
_RE_COMMA_JSON_HEAD = re.compile(r'^(\w[\w.\-]*)\s*,\s*(.*)', re.DOTALL)

# Format 8: <tool_call>ToolName{"key":"value"}</tool_call>
# Tool name directly followed by a JSON object (no comma, no space)
_RE_NAME_JSON_HEAD = re.compile(r'^(\w[\w.\-]*)\s*(\{.*)', re.DOTALL)

# Format 9: <tool_call>ToolName","arguments":{"key":"value"}}</tool_call>
# Truncated JSON: model meant {"name":"ToolName","arguments":{...}} but
# omitted the opening {"name":" prefix. Result: ToolName","arguments":{...}}
_RE_TRUNCATED_JSON = re.compile(r'^(\w[\w.\-]*)","arguments"\s*:\s*(.*)', re.DOTALL)

# Format 10: ToolName<parameters>{...}</parameters>
# The model (glm-5.2) sometimes outputs this format instead of <tool_call> tags:
#   exec_command<parameters>{"cmd":"git status","workdir":"/path"}</parameters>
#   shell<parameters>{"command":"ls -la"}</parameters>
# Also handles <parameter name="key">value</parameter> inside <parameters>:
#   exec_command<parameters><parameter name="cmd">git status</parameter></parameters>
_RE_PARAMETERS_BLOCK = re.compile(r'<parameters>(.*?)</parameters>', re.DOTALL)
_RE_PARAMETER_NAMED_INNER = re.compile(r'<parameter\s+name="([^"]+)">(.*?)</parameter>', re.DOTALL)
# Bare ToolName<parameters>...</parameters> (without <tool_call> wrapping)
_RE_BARE_PARAMETERS = re.compile(r'(\w[\w.\-]*)\s*<parameters>(.*?)</parameters>', re.DOTALL)

# Stray XML tags that leak into tool_call content
_RE_STRAY_XML = re.compile(r'</?arg_value>|</?parameter>|</?invoke>|</?function_calls>')

# Precompiled patterns for _strip_agent_xml (avoid recompiling on every SSE chunk)
_RE_AGENT_PATTERNS = [
    re.compile(r'<function_calls>.*?</function_calls>', re.DOTALL),
    re.compile(r'<invoke\s+name="[^"]*">.*?</invoke>', re.DOTALL),
    re.compile(r'<parameter\s+name="[^"]*">.*?</parameter>', re.DOTALL),
    re.compile(r'<parameters>.*?</parameters>', re.DOTALL),
    re.compile(r'<antThinking>.*?</antThinking>', re.DOTALL),
    re.compile(r'<plan>.*?</plan>', re.DOTALL),
    re.compile(r'<think>.*?</think>', re.DOTALL),
    re.compile(r'<arg_value>.*?</arg_value>', re.DOTALL),
]
_AGENT_ORPHAN_TAGS = [
    '<function_calls>', '</function_calls>',
    '<invoke>', '</invoke>',
    '<parameter>', '</parameter>',
    '<parameters>', '</parameters>',
    '<antThinking>', '</antThinking>',
    '<plan>', '</plan>',
    '<think>', '</think>',
    '<arg_value>', '</arg_value>',
]

# Agent-mode status artifacts that leak into output
# Matches: ◯ Goal not yet met… continuing, ◯ Cooked for 3s, etc.
# Also matches other circle characters: ○ ● ⭕ ⚪ 🔴
_RE_AGENT_STATUS = re.compile(r'[◯○●⭕⚪🔴]\s*[^\n]*\n?', re.DOTALL)

# ---------------------------------------------------------------------------
# Codex CLI tool result metadata patterns
# ---------------------------------------------------------------------------
# Codex CLI wraps tool results with internal debugging metadata:
#   Chunk ID: 382ad3
#   Wall time:: 0.0000 seconds
#   Process failed (exit code 1)
#   Original token comm: 0
#   Output:
#   <actual content>
# This metadata leaks into model context and causes the model to echo it
# in its output, creating display pollution.

# Individual Codex CLI metadata line patterns (matched at start of line)
_RE_CODEX_METADATA_LINES = re.compile(
    r'^(?:Chunk ID:\s*\w+'
    r'|Wall time::?\s*[\d.]+\s*seconds'
    r'|Process\s+(?:failed|exited)\s+(?:\(exit code\s*\d+\)|with code\s*\d+)'
    r'|Original token\w*::?\s*\d+)'
    r'\s*$\n?',
    re.MULTILINE
)

# "Output:" separator line — content after this is the actual tool output
_RE_CODEX_OUTPUT_SEPARATOR = re.compile(r'^Output:\s*\n', re.MULTILINE)

# Simulated "Tool Result:" at start of a line — the model hallucinates tool
# execution output instead of waiting for the actual result.
# Match at start of line to avoid matching in-sentence references like
# "The Tool Result shows that..."
_RE_SIMULATED_TOOL_RESULT = re.compile(
    r'(?:^|\n)[ \t]*Tool Result:.*',
    re.DOTALL
)

# Max chars to scan after JSON for a closing tag (replaces magic numbers 50 and 10)
_CLOSE_TAG_SCAN_WINDOW = 100


# ---------------------------------------------------------------------------
# Balanced JSON extraction
# ---------------------------------------------------------------------------

def _find_balanced_json(text: str, start: int) -> tuple:
    """Find a balanced JSON object starting at position 'start' in text.

    Assumes text[start] == '{'. Returns (json_str, end_pos) where end_pos
    is the index AFTER the closing '}'. Returns (None, start) if no valid
    JSON object is found.

    Handles:
    - Nested objects and arrays
    - String literals (with escape sequences)
    - Whitespace
    """
    if start >= len(text) or text[start] != '{':
        return None, start

    depth = 0
    in_string = False
    escape = False
    i = start

    while i < len(text):
        ch = text[i]

        if escape:
            escape = False
            i += 1
            continue

        if ch == '\\' and in_string:
            escape = True
            i += 1
            continue

        if ch == '"':
            in_string = not in_string
            i += 1
            continue

        if in_string:
            i += 1
            continue

        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1

        i += 1

    return None, start


def _clean_json_string(s: str) -> str:
    """Clean up malformed JSON that the model outputs.

    Fixes common issues:
    - Extra spaces/quotes around key names: {"name":"Read"," "arguments":...}
      → {"name":"Read","arguments":...}
    - Trailing punctuation after closing brace: {...}}. → {...}}
    - Extra spaces in key-value separators

    CRITICAL: This function is JSON-STRING-AWARE. It only applies regexes
    to text OUTSIDE of JSON string values. Without this, patterns like
    " : " inside old_string values (e.g. Edit tool calls containing JSON
    code) would be corrupted to ":", causing old_string to not match
    the actual file content.
    """
    # Walk through the string, tracking JSON string state.
    # Apply regexes only to text OUTSIDE of string values.
    result = []
    outside_buffer = []  # accumulates text outside strings
    in_string = False
    escape = False

    def _flush_outside():
        """Apply cleaning regexes to accumulated outside-string text."""
        if not outside_buffer:
            return ""
        text = "".join(outside_buffer)
        outside_buffer.clear()
        if not text:
            return ""
        # Fix patterns like: ," "arguments": → ,"arguments":
        text = re.sub(r',"\s*"(\w+)":', r',"\1":', text)
        # Fix patterns like: {" "key": → {"key":
        text = re.sub(r'\{"\s*"(\w+)":', r'{"\1":', text)
        # Normalize key-value separator: "key" : "value" → "key":"value"
        text = re.sub(r'"\s*:\s*"', '":"', text)
        # Normalize comma separator: , "key" → ,"key"
        text = re.sub(r',\s*"', ',"', text)
        return text

    for ch in s:
        if escape:
            escape = False
            if in_string:
                result.append(ch)
            else:
                outside_buffer.append(ch)
            continue

        if ch == '\\' and in_string:
            escape = True
            result.append(ch)
            continue

        if ch == '"':
            if not in_string:
                # Opening a string — flush outside buffer first
                result.append(_flush_outside())
                result.append(ch)
                in_string = True
            else:
                # Closing a string
                result.append(ch)
                in_string = False
            continue

        if in_string:
            result.append(ch)
        else:
            outside_buffer.append(ch)

    # Flush any remaining outside-string text
    result.append(_flush_outside())

    cleaned = "".join(result)
    # Remove trailing punctuation after closing brace (safe to do on full string)
    cleaned = re.sub(r'\}\s*[.,;]+\s*$', '}', cleaned)
    return cleaned


def _fix_unescaped_quotes_in_json(s: str) -> str:
    """Fix unescaped double quotes inside JSON string values.

    Models often output JSON like:
      {"command":"find . -name "*.ts" | grep foo"}
    where the " around *.ts are NOT escaped, breaking json.loads.

    Strategy: walk through the string tracking JSON structure.
    When inside a string value and we encounter a " that is followed by
    characters that clearly aren't a JSON delimiter (not followed by , or } or :),
    escape it as \".
    """
    result = []
    in_string = False
    escape = False
    i = 0

    while i < len(s):
        ch = s[i]

        if escape:
            escape = False
            result.append(ch)
            i += 1
            continue

        if ch == '\\' and in_string:
            escape = True
            result.append(ch)
            i += 1
            continue

        if ch == '"':
            if not in_string:
                # Opening a string
                in_string = True
                result.append(ch)
                i += 1
                continue
            else:
                # We're inside a string and hit a quote.
                # Check if this is the real end of the string value.
                # If the next non-whitespace char is : , } ] then it's a delimiter.
                # Otherwise, it's an unescaped quote inside the value.
                j = i + 1
                while j < len(s) and s[j] in ' \t':
                    j += 1

                if j >= len(s):
                    # End of input — this is the closing quote
                    in_string = False
                    result.append(ch)
                    i += 1
                    continue

                next_ch = s[j]
                if next_ch in ':,}]':
                    # This looks like the real end of the string value
                    in_string = False
                    result.append(ch)
                    i += 1
                    continue
                else:
                    # This is an unescaped quote inside the string value.
                    # Escape it.
                    result.append('\\"')
                    i += 1
                    continue

        result.append(ch)
        i += 1

    return ''.join(result)


def _escape_raw_newlines_in_strings(s: str) -> str:
    """Escape raw newlines and tabs inside JSON string values.

    Models often output JSON with actual newline characters inside string
    values (especially for multi-line file content in Write/Edit calls).
    This is invalid JSON — json.loads fails with "Invalid control character".

    This function scans the string, tracks whether we're inside a JSON string
    (between unescaped quotes), and replaces raw \\n, \\r, \\t with their
    escape sequences \\n, \\r, \\t.
    """
    result = []
    in_string = False
    escape = False

    for ch in s:
        if escape:
            escape = False
            result.append(ch)
            continue

        if ch == '\\' and in_string:
            escape = True
            result.append(ch)
            continue

        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue

        if in_string:
            if ch == '\n':
                result.append('\\n')
            elif ch == '\r':
                result.append('\\r')
            elif ch == '\t':
                result.append('\\t')
            else:
                result.append(ch)
        else:
            result.append(ch)

    return ''.join(result)


def _fix_invalid_json_escapes(s: str) -> str:
    r"""Fix invalid JSON escape sequences.

    JSON only allows these escape sequences: \" \\ \/ \b \f \n \r \t \uXXXX
    Models (or Codex CLI history) sometimes output invalid escapes like:
      \*  -> should be just * (remove the backslash)
      \'  -> should be just ' (remove the backslash)
      \!  -> should be just ! (remove the backslash)
      etc.

    This function walks through the string tracking JSON string state.
    Inside string values, it removes backslashes before characters that
    are NOT valid JSON escape characters.
    """
    # Valid JSON escape characters after backslash
    _VALID_ESCAPES = frozenset('"\\/bfnrtu')

    result = []
    in_string = False
    escape = False

    for ch in s:
        if escape:
            escape = False
            if ch in _VALID_ESCAPES:
                # Valid escape — keep as-is
                result.append('\\')
                result.append(ch)
            else:
                # Invalid escape (e.g. \*, \', \!) — remove the backslash
                # Keep the character as-is (it's inside a JSON string)
                result.append(ch)
            continue

        if ch == '\\' and in_string:
            escape = True
            continue

        if ch == '"':
            in_string = not in_string

        result.append(ch)

    return ''.join(result)


def _regex_extract_tool_call(text: str) -> dict | None:
    """Last-resort extraction using regex for badly malformed JSON.

    Handles cases where the model outputs JSON with:
    - Missing closing quotes in string values
    - Extra closing braces (}}})
    - No closing </tool_call> tag
    - Truncated JSON

    Example input:
      {"name":"exec_command","arguments":{"cmd": "cat file | grep 'pattern'}}}

    This function bypasses json.loads entirely and uses regex to extract
    the tool name and argument key-value pairs.
    """
    # Extract tool name (this part is usually well-formed)
    name_match = re.search(r'"name"\s*:\s*"([^"]*)"', text)
    if not name_match:
        return None
    tool_name = name_match.group(1)

    # Extract arguments
    args = {}

    # Find the arguments block
    args_match = re.search(r'"arguments"\s*:\s*\{', text)
    if args_match:
        args_text = text[args_match.end():]  # everything after "arguments": {

        # Extract key-value pairs using regex
        # Pattern: "key": "value" or "key": 'value' or "key": value
        kv_pattern = re.compile(r'"(\w+)"\s*:\s*')
        for m in kv_pattern.finditer(args_text):
            key = m.group(1)
            val_start = m.end()

            if val_start >= len(args_text):
                continue

            # Determine value type and extract
            if args_text[val_start] == '"':
                # Double-quoted string value
                close_quote = args_text.find('"', val_start + 1)
                if close_quote != -1:
                    val = args_text[val_start + 1:close_quote]
                else:
                    # No closing quote — take everything up to } or end
                    # Strip trailing } characters
                    val = args_text[val_start + 1:].rstrip('}').rstrip()
                args[key] = val
            elif args_text[val_start] == "'":
                # Single-quoted string value
                close_quote = args_text.find("'", val_start + 1)
                if close_quote != -1:
                    val = args_text[val_start + 1:close_quote]
                else:
                    val = args_text[val_start + 1:].rstrip('}').rstrip()
                args[key] = val
            else:
                # Non-string value (number, boolean, null, array, object)
                # Take up to , or } (respecting nesting)
                depth = 0
                end = val_start
                while end < len(args_text):
                    ch = args_text[end]
                    if ch == '{' or ch == '[':
                        depth += 1
                    elif ch == '}' or ch == ']':
                        if depth == 0:
                            break
                        depth -= 1
                    elif ch == ',' and depth == 0:
                        break
                    end += 1
                val = args_text[val_start:end].strip()
                # Try to parse as JSON for non-string types
                try:
                    args[key] = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    args[key] = val

    if not tool_name:
        return None

    if VERBOSE:
        print(f"[CatPawProxy] Regex extraction: name={tool_name}, args={list(args.keys())}", flush=True)

    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": json.dumps(args, ensure_ascii=False),
        }
    }


def _extract_tool_call(json_str: str) -> dict | None:
    """Extract a single tool call from JSON string.

    Uses a progressive strategy — tries the least invasive fix first,
    only applying more aggressive cleaning if simpler methods fail.
    This prevents _clean_json_string from corrupting valid JSON that
    happens to contain patterns like " : " inside string values.
    """
    # Strategy 1: Try parsing as-is (handles well-formed JSON)
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, AttributeError):
        # Strategy 2: Fix invalid JSON escapes (\*, \', etc. from Codex history)
        fixed_esc = _fix_invalid_json_escapes(json_str)
        try:
            data = json.loads(fixed_esc)
        except (json.JSONDecodeError, AttributeError):
            # Strategy 3: Escape raw newlines/tabs (models output them unescaped)
            fixed = _escape_raw_newlines_in_strings(fixed_esc)
            try:
                data = json.loads(fixed)
            except (json.JSONDecodeError, AttributeError):
                # Strategy 4: Fix unescaped quotes (common in Bash: -name "*.ts")
                fixed2 = _fix_unescaped_quotes_in_json(fixed)
                try:
                    data = json.loads(fixed2)
                except (json.JSONDecodeError, AttributeError):
                    # Strategy 5: Full cleaning (last resort — may modify content)
                    fixed3 = _clean_json_string(fixed)
                    fixed3 = _escape_raw_newlines_in_strings(fixed3)
                    try:
                        data = json.loads(fixed3)
                    except (json.JSONDecodeError, AttributeError) as e:
                        # Strategy 6: Clean + quote fix (ultimate last resort)
                        fixed4 = _fix_unescaped_quotes_in_json(fixed3)
                        try:
                            data = json.loads(fixed4)
                        except (json.JSONDecodeError, AttributeError) as e2:
                            # Strategy 7: Regex extraction (handles missing closing
                            # quotes, extra braces, truncated JSON — bypasses
                            # json.loads entirely)
                            tc = _regex_extract_tool_call(json_str)
                            if tc:
                                if VERBOSE:
                                    print(f"[CatPawProxy] Regex extraction succeeded after all JSON parsing failed", flush=True)
                                return tc
                            if VERBOSE:
                                print(f"[CatPawProxy] Tool call parse error: {e} (also tried quote fix: {e2})", flush=True)
                            return None
    tc_name = data.get("name", "")
    if not tc_name:
        return None
    tc_args = data.get("arguments", {})
    if isinstance(tc_args, dict):
        tc_args = json.dumps(tc_args, ensure_ascii=False)
    elif not isinstance(tc_args, str):
        tc_args = json.dumps(tc_args, ensure_ascii=False)
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": tc_name,
            "arguments": tc_args,
        }
    }


def _unescape_string_value(pval: str) -> str:
    r"""Unescape a quoted string value from func-call or space-separated syntax.

    Handles:
    - Double-quoted strings: \" -> ", \\ -> \, \n -> newline, \t -> tab
    - Single-quoted strings: just strip quotes (no escape processing)
    - Unquoted values: return as-is
    """
    pval = pval.strip()
    if len(pval) >= 2:
        if pval.startswith('"') and pval.endswith('"'):
            # Try JSON unescaping for double-quoted strings
            try:
                return json.loads(pval)
            except json.JSONDecodeError:
                # Fallback: simple strip + manual unescape
                inner = pval[1:-1]
                inner = inner.replace('\\"', '"').replace('\\\\', '\\')
                inner = inner.replace('\\n', '\n').replace('\\t', '\t')
                return inner
        elif pval.startswith("'") and pval.endswith("'"):
            return pval[1:-1]
    return pval


def _parse_func_call_syntax(text: str) -> dict | None:
    """Parse function-call syntax: FunctionName(param="value", param2="value2").

    The model sometimes outputs this instead of JSON:
        Read(file_path="/path/to/file")
        Write(file_path="/path", content="text")

    Returns a tool_call dict or None.
    """
    text = text.strip()
    m = _RE_FUNC_CALL_SYNTAX.match(text)
    if not m:
        return None

    func_name = m.group(1)
    params_str = m.group(2).strip()

    args = {}
    if params_str:
        for kv_match in _RE_KV_PAIR.finditer(params_str):
            pname = kv_match.group(1)
            pval = kv_match.group(2).strip()
            pval = _unescape_string_value(pval)
            args[pname] = pval

    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": func_name,
            "arguments": json.dumps(args, ensure_ascii=False),
        }
    }


def _extract_kv_pairs(text: str) -> dict:
    """Extract "key":"value" pairs from text as a fallback JSON parser.

    Used by _parse_name_json_syntax, _parse_truncated_json_syntax, and
    _parse_comma_json_syntax when json.loads fails on the extracted fragment.
    Handles quoted strings (via json.loads) and unquoted values (as-is).
    """
    args = {}
    for kv_match in re.finditer(r'"(\w+)"\s*:\s*("(?:[^"\\]|\\.)*"|[^,}]+)', text):
        pname = kv_match.group(1)
        pval = kv_match.group(2).strip()
        if pval.startswith('"') and pval.endswith('"'):
            try:
                pval = json.loads(pval)
            except json.JSONDecodeError:
                pval = pval[1:-1]
        args[pname] = pval
    return args


def _parse_name_json_syntax(text: str) -> dict | None:
    """Parse ToolName{"key":"value"} syntax.

    The model outputs this format — tool name directly followed by a JSON object:
        Read{"file_path":"/path/to/file"}
        mcp__tool__name{"detail_level":"medium","repo_root":"/path"}

    Returns a tool_call dict or None.
    """
    text = _RE_STRAY_XML.sub('', text).strip()
    # Strip trailing punctuation that breaks JSON parsing
    text = text.rstrip('.,;')
    if not text:
        return None

    m = _RE_NAME_JSON_HEAD.match(text)
    if not m:
        return None

    func_name = m.group(1).strip()
    json_str = m.group(2).strip()

    # Must look like a tool name
    if not func_name or not re.match(r'^[\w.\-]+$', func_name):
        return None

    # Find the balanced JSON object
    balanced_json, _ = _find_balanced_json(json_str, 0)
    if not balanced_json:
        return None

    try:
        args = json.loads(balanced_json)
        if isinstance(args, dict) and args:
            return {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": func_name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                }
            }
    except json.JSONDecodeError:
        pass

    # Fallback: extract "key":"value" pairs manually
    args = _extract_kv_pairs(balanced_json or json_str)

    if not args:
        return None

    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": func_name,
            "arguments": json.dumps(args, ensure_ascii=False),
        }
    }


def _parse_truncated_json_syntax(text: str) -> dict | None:
    """Parse truncated JSON: ToolName","arguments":{"key":"value"}}.

    The model meant to output {"name":"ToolName","arguments":{...}} but
    omitted the opening {"name":" prefix. Result:
        Read","arguments":{"file_path":"/path"}}
        mcp__tool","arguments":{"repo_root":"/path","detail_level":"high"}}

    Returns a tool_call dict or None.
    """
    text = _RE_STRAY_XML.sub('', text).strip()
    # Strip trailing punctuation
    text = text.rstrip('.,;')
    if not text:
        return None

    m = _RE_TRUNCATED_JSON.match(text)
    if not m:
        return None

    func_name = m.group(1).strip()
    args_part = m.group(2).strip()

    # Must look like a tool name
    if not func_name or not re.match(r'^[\w.\-]+$', func_name):
        return None

    # args_part should be like {"file_path":"/path"}}  (with trailing })
    # Remove one trailing } to get the arguments JSON object
    if args_part.endswith('}}'):
        args_json_str = args_part[:-1]  # Remove one trailing }
    elif args_part.endswith('}'):
        args_json_str = args_part
    else:
        args_json_str = args_part

    # Find balanced JSON
    brace_start = args_json_str.find('{')
    if brace_start == -1:
        return None

    balanced_json, _ = _find_balanced_json(args_json_str, brace_start)
    if not balanced_json:
        return None

    try:
        args = json.loads(balanced_json)
        if isinstance(args, dict):
            return {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": func_name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                }
            }
    except json.JSONDecodeError:
        pass

    # Fallback: extract "key":"value" pairs manually
    args = _extract_kv_pairs(balanced_json or args_json_str)

    if not args:
        return None

    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": func_name,
            "arguments": json.dumps(args, ensure_ascii=False),
        }
    }


def _parse_comma_json_syntax(text: str) -> dict | None:
    """Parse comma-JSON syntax: ToolName,"key":"value","key2":"value2".

    The model outputs this format — like JSON but without braces, with the
    tool name as the first comma-separated element:
        Read,"file_path":"/path/to/file"
        mcp__tool__name, "detail_level": "medium", "repo_root": "/path"

    Returns a tool_call dict or None.
    """
    text = _RE_STRAY_XML.sub('', text).strip()
    # Strip trailing punctuation that breaks JSON parsing
    text = text.rstrip('.,;')
    if not text:
        return None

    m = _RE_COMMA_JSON_HEAD.match(text)
    if not m:
        return None

    func_name = m.group(1).strip()
    args_str = m.group(2).strip()

    # Must look like a tool name
    if not func_name or not re.match(r'^[\w.\-]+$', func_name):
        return None

    if not args_str:
        return None

    # Try wrapping in braces and parsing as JSON
    json_str = '{' + args_str + '}'
    try:
        args = json.loads(json_str)
        if isinstance(args, dict) and args:
            return {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": func_name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                }
            }
    except json.JSONDecodeError:
        pass

    # Fallback: extract "key":"value" pairs manually
    args = _extract_kv_pairs(args_str)

    if not args:
        return None

    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": func_name,
            "arguments": json.dumps(args, ensure_ascii=False),
        }
    }


def _parse_space_separated_syntax(text: str) -> dict | None:
    """Parse space-separated syntax: ToolName param="value" param2="value2".

    The model outputs this format frequently:
        Read file_path="/path/to/file"
        mcp__tool__name detail_level="medium" repo_root="/path"
        Bash command="ls -la" description="List files"

    Also handles concatenated format (no separator between tool name and params):
        Readfile_path="/path/to/file"
        Skillargs="{}"skill="name"

    Returns a tool_call dict or None.
    """
    text = _RE_STRAY_XML.sub('', text).strip()
    if not text:
        return None

    # Find all key=value pairs
    kv_matches = list(_RE_SPACE_KV.finditer(text))
    if not kv_matches:
        return None

    # Tool name is the text before the first key=value pair
    first_kv_start = kv_matches[0].start()
    func_name = text[:first_kv_start].strip()

    # If func_name is empty, the tool name is concatenated with the first param name.
    # e.g. "Readfile_path=..." → "Read" + "file_path=..."
    # "Skillargs=..." → "Skill" + "args=..."
    if not func_name and kv_matches:
        first_key = kv_matches[0].group(1)
        # Strategy 1: Try matching known tool name prefixes
        _KNOWN_TOOLS = [
            'Read', 'Write', 'Edit', 'MultiEdit', 'Bash', 'Skill', 'Grep',
            'Glob', 'List', 'Search', 'WebFetch', 'WebSearch', 'TodoWrite',
            'NotebookEdit', 'Task', 'LS', 'View', 'Ask', 'Nushell',
        ]
        for tool in _KNOWN_TOOLS:
            if first_key.startswith(tool) and len(first_key) > len(tool):
                candidate_param = first_key[len(tool):]
                if re.match(r'^\w+$', candidate_param):
                    func_name = tool
                    # Rebuild args with the split param name
                    args = {}
                    pval = _unescape_string_value(kv_matches[0].group(2))
                    args[candidate_param] = pval
                    for kv_match in kv_matches[1:]:
                        pname = kv_match.group(1)
                        pval = _unescape_string_value(kv_match.group(2))
                        args[pname] = pval
                    if args:
                        return {
                            "id": f"call_{uuid.uuid4().hex[:24]}",
                            "type": "function",
                            "function": {
                                "name": func_name,
                                "arguments": json.dumps(args, ensure_ascii=False),
                            }
                        }
        # Strategy 2: Try all splits, prefer param names with underscores
        best_split = None
        for i in range(2, len(first_key)):
            candidate_tool = first_key[:i]
            candidate_param = first_key[i:]
            if not re.match(r'^[\w.\-]+$', candidate_tool):
                continue
            if not re.match(r'^\w+$', candidate_param):
                continue
            # Prefer splits where param starts with lowercase and contains underscore
            score = 0
            if candidate_param[0].islower():
                score += 1
            if '_' in candidate_param:
                score += 2
            if candidate_tool[0].isupper():
                score += 1
            if best_split is None or score > best_split[0]:
                best_split = (score, i, candidate_tool, candidate_param)
        if best_split and best_split[0] >= 2:
            func_name = best_split[2]
            args = {}
            pval = _unescape_string_value(kv_matches[0].group(2))
            args[best_split[3]] = pval
            for kv_match in kv_matches[1:]:
                pname = kv_match.group(1)
                pval = _unescape_string_value(kv_match.group(2))
                args[pname] = pval
            if args:
                return {
                    "id": f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": func_name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    }
                }
        return None

    # Must look like a tool name (alphanumeric + underscores + dots + hyphens)
    if not func_name or not re.match(r'^[\w.\-]+$', func_name):
        return None

    args = {}
    for kv_match in kv_matches:
        pname = kv_match.group(1)
        pval = _unescape_string_value(kv_match.group(2))
        args[pname] = pval

    if not args:
        return None

    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": func_name,
            "arguments": json.dumps(args, ensure_ascii=False),
        }
    }


def _parse_parameters_tag_syntax(text: str) -> dict | None:
    """Parse ToolName<parameters>{...}</parameters> syntax.

    The model (glm-5.2) sometimes outputs this format instead of standard
    <tool_call> tags, especially when working with Codex CLI tools:
        exec_command<parameters>{"cmd":"git status","workdir":"/path"}</parameters>
        shell<parameters>{"command":"ls -la"}</parameters>

    Also handles <parameter name="key">value</parameter> inside <parameters>:
        exec_command<parameters>
        <parameter name="cmd">git status</parameter>
        <parameter name="workdir">/path</parameter>
        </parameters>

    Returns a tool_call dict or None.
    """
    text = text.strip()
    if not text:
        return None

    param_match = _RE_PARAMETERS_BLOCK.search(text)
    if not param_match:
        return None

    # Tool name is text before <parameters>
    func_name = text[:param_match.start()].strip()
    # Clean up stray XML tags from tool name (but NOT <parameters> plural)
    func_name = _RE_STRAY_XML.sub('', func_name).strip()
    # Remove any trailing punctuation/newlines
    func_name = func_name.rstrip('.,;:\n\r\t ')

    if not func_name or not re.match(r'^[\w.\-]+$', func_name):
        return None

    param_content = param_match.group(1).strip()
    args = {}

    # Strategy 1: Try parsing as JSON object
    if param_content.startswith('{'):
        balanced_json, _ = _find_balanced_json(param_content, 0)
        if balanced_json:
            try:
                parsed = json.loads(balanced_json)
                if isinstance(parsed, dict):
                    args = parsed
            except json.JSONDecodeError:
                # Try with newline escaping
                fixed = _escape_raw_newlines_in_strings(balanced_json)
                try:
                    parsed = json.loads(fixed)
                    if isinstance(parsed, dict):
                        args = parsed
                except json.JSONDecodeError:
                    # Try with quote fixing
                    fixed2 = _fix_unescaped_quotes_in_json(fixed)
                    try:
                        parsed = json.loads(fixed2)
                        if isinstance(parsed, dict):
                            args = parsed
                    except json.JSONDecodeError:
                        pass

    # Strategy 2: Try <parameter name="key">value</parameter> format
    if not args:
        for param_inner in _RE_PARAMETER_NAMED_INNER.finditer(param_content):
            pname = param_inner.group(1)
            pval = param_inner.group(2).strip()
            args[pname] = pval

    # Strategy 3: Try KV pair extraction as last resort
    if not args:
        args = _extract_kv_pairs(param_content)

    if not args:
        return None

    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": func_name,
            "arguments": json.dumps(args, ensure_ascii=False),
        }
    }


def _find_closing_tag_outside_json(content: str, close_tags: list) -> tuple:
    """Find the earliest closing tag that is NOT inside a JSON string.

    Args:
        content: text to search (content after <tool_call>)
        close_tags: list of closing tag strings to look for

    Returns: (close_pos, close_len) or (-1, 0) if not found.
    close_pos is the index of the closing tag in content.
    """
    # Track JSON string state to avoid matching tags inside string values
    in_string = False
    escape = False
    i = 0

    while i < len(content):
        ch = content[i]

        if escape:
            escape = False
            i += 1
            continue

        if ch == '\\' and in_string:
            escape = True
            i += 1
            continue

        if ch == '"':
            in_string = not in_string
            i += 1
            continue

        if in_string:
            i += 1
            continue

        # Not inside a string — check for closing tags
        for ct in close_tags:
            if content[i:i + len(ct)] == ct:
                return i, len(ct)

        i += 1

    return -1, 0


# Known tool names for bare-JSON matching (must match the tools we inject)
_KNOWN_TOOL_NAMES = frozenset([
    'Read', 'Write', 'Edit', 'MultiEdit', 'Bash', 'Skill', 'Grep',
    'Glob', 'List', 'Search', 'WebFetch', 'WebSearch', 'TodoWrite',
    'NotebookEdit', 'Task', 'LS', 'View', 'Ask', 'Nushell',
    'delete_file', 'read_file', 'write_file', 'edit_file',
    'list_dir', 'codebase_search', 'web_search', 'run_terminal_cmd',
    'todo_write', 'string_replace', 'MultiEdit',
    # Codex CLI tool names
    'shell', 'exec_command', 'container_exec', 'apply_patch',
    'create_file', 'delete_file', 'read_file', 'write_file',
])


def _find_bare_json_tool_call(content: str) -> tuple | None:
    """Find a bare JSON tool call in content (no <tool_call> tags).

    The model sometimes outputs:
        {"name":"Write","arguments":{"file_path":"...","content":"..."}}

    directly in the text, without any wrapping tags.

    Returns (tool_call_dict, start_pos, end_pos) or None.

    Strategy:
    - Scan for JSON objects that have "name" and "arguments" keys
    - The "name" value must look like a tool name
    - Skip JSON inside markdown code blocks (handled by Format 4/5)
    - Only take the FIRST match (sequential execution)
    """
    # Quick check: if there's no {"name" pattern, skip entirely
    if '{"name"' not in content and '{ "name"' not in content:
        return None

    # Find all potential JSON object starts
    search_pos = 0
    while search_pos < len(content):
        # Find the next '{' that might start a tool call JSON
        brace_pos = content.find('{', search_pos)
        if brace_pos == -1:
            break

        # Check if this { is inside a markdown code block
        # (look backwards for ``` markers)
        prefix = content[:brace_pos]
        backtick_count = prefix.count('```')
        if backtick_count % 2 == 1:
            # Inside a code block — skip
            search_pos = brace_pos + 1
            continue

        # Try to extract a balanced JSON object starting here
        json_str, end_pos = _find_balanced_json(content, brace_pos)
        if not json_str:
            # _find_balanced_json failed (e.g., missing closing " in string
            # value). Try regex extraction as a fallback.
            tc = _regex_extract_tool_call(content[brace_pos:])
            if tc:
                tc_name = tc.get("function", {}).get("name", "")
                if tc_name in _KNOWN_TOOL_NAMES or (
                    re.match(r'^[\w.\-]+$', tc_name)
                    and (tc_name[0].isupper() or '__' in tc_name)
                    and len(tc_name) >= 2
                ):
                    # Find end position (last } in content)
                    last_brace = content.rfind('}')
                    return tc, brace_pos, last_brace + 1 if last_brace != -1 else len(content)
            search_pos = brace_pos + 1
            continue

        # Check if this JSON has "name" and "arguments" keys
        # Use _extract_tool_call which handles cleaning + parsing
        tc = _extract_tool_call(json_str)
        if tc:
            tc_name = tc.get("function", {}).get("name", "")
            # Validate: name must be a known tool or look like a tool name
            # (alphanumeric + underscores + dots + hyphens, starts with uppercase
            # or contains __ for MCP tools)
            if tc_name in _KNOWN_TOOL_NAMES or (
                re.match(r'^[\w.\-]+$', tc_name)
                and (tc_name[0].isupper() or '__' in tc_name)
                and len(tc_name) >= 2
            ):
                return tc, brace_pos, end_pos

        # Not a tool call JSON — continue searching after this position
        search_pos = end_pos

    return None


def _find_tag_tool_calls(content: str, tag_name: str) -> list:
    """Find all tool calls wrapped in <tag_name>...</tag_name> (or similar).

    Uses balanced brace matching to handle nested JSON objects.
    Also handles function-call syntax: <tool_call>Read(file_path="...")</tool_call>
    Handles cases where the closing tag is missing or different (e.g. </think>).

    Returns list of (start_pos, end_pos, tool_call_dict) tuples.
    """
    results = []
    open_tag = f"<{tag_name}>"
    search_pos = 0

    while True:
        tag_pos = content.find(open_tag, search_pos)
        if tag_pos == -1:
            break

        content_after_tag = content[tag_pos + len(open_tag):]
        # Find the closing tag (or use </think> as alternative)
        close_tags = [f"</{tag_name}>", "</think>", "</tool_call>", "</tool_use>"]

        # Use JSON-aware closing tag search to avoid matching tags inside string values
        close_pos, close_len = _find_closing_tag_outside_json(content_after_tag, close_tags)

        if close_pos == -1:
            # No closing tag found — look for next open tag to bound this one
            next_tag_pos = content_after_tag.find(open_tag)
            if next_tag_pos != -1:
                # Next <tool_call> found — content ends before it
                inner = content_after_tag[:next_tag_pos].strip()
                end_pos = tag_pos + len(open_tag) + next_tag_pos
            else:
                # No next tag — take rest of content
                inner = content_after_tag.strip()
                end_pos = len(content)
        else:
            inner = content_after_tag[:close_pos].strip()
            end_pos = tag_pos + len(open_tag) + close_pos + close_len

        # Try truncated JSON first: ToolName","arguments":{"key":"value"}}
        # This must be checked BEFORE regular JSON, because _find_balanced_json
        # would find the inner {"file_path":"..."} object and miss the tool name.
        tc = _parse_truncated_json_syntax(inner)
        if tc:
            if VERBOSE:
                print(f"[CatPawProxy] Parsed truncated-JSON syntax: {tc['function']['name']}", flush=True)
            results.append((tag_pos, end_pos, tc))
            search_pos = end_pos
            continue

        # Try JSON format: {"name": "...", "arguments": {...}}
        # Also try ToolName{"key":"value"} where JSON is arguments, not a wrapper
        # NOTE: Do NOT pre-clean with _clean_json_string here — _extract_tool_call
        # now uses a progressive strategy that tries parsing without cleaning first.
        # Pre-cleaning could corrupt string values containing " : " patterns.
        json_start = inner.find('{')
        if json_start != -1:
            json_str, json_end = _find_balanced_json(inner, json_start)
            if json_str:
                tc = _extract_tool_call(json_str)
                if tc:
                    results.append((tag_pos, end_pos, tc))
                    search_pos = end_pos
                    continue

                # JSON doesn't have name/arguments — try ToolName{json} format
                tc = _parse_name_json_syntax(inner)
                if tc:
                    if VERBOSE:
                        print(f"[CatPawProxy] Parsed name-JSON syntax: {tc['function']['name']}", flush=True)
                    results.append((tag_pos, end_pos, tc))
                    search_pos = end_pos
                    continue
            else:
                # _find_balanced_json failed (e.g., missing closing " in a
                # string value). Try regex extraction as a fallback — this
                # bypasses brace matching entirely and uses pattern matching
                # to extract name and arguments from malformed JSON.
                tc = _regex_extract_tool_call(inner[json_start:])
                if tc:
                    if VERBOSE:
                        print(f"[CatPawProxy] Parsed malformed-JSON (regex fallback): {tc['function']['name']}", flush=True)
                    results.append((tag_pos, end_pos, tc))
                    search_pos = end_pos
                    continue

        # Try function-call syntax: FunctionName(param="value")
        tc = _parse_func_call_syntax(inner)
        if tc:
            if VERBOSE:
                print(f"[CatPawProxy] Parsed func-call syntax: {tc['function']['name']}", flush=True)
            results.append((tag_pos, end_pos, tc))
            search_pos = end_pos
            continue

        # Try space-separated syntax: ToolName param="value" param2="value2"
        tc = _parse_space_separated_syntax(inner)
        if tc:
            if VERBOSE:
                print(f"[CatPawProxy] Parsed space-separated syntax: {tc['function']['name']}", flush=True)
            results.append((tag_pos, end_pos, tc))
            search_pos = end_pos
            continue

        # Try comma-JSON syntax: ToolName,"key":"value","key2":"value2"
        tc = _parse_comma_json_syntax(inner)
        if tc:
            if VERBOSE:
                print(f"[CatPawProxy] Parsed comma-JSON syntax: {tc['function']['name']}", flush=True)
            results.append((tag_pos, end_pos, tc))
            search_pos = end_pos
            continue

        # Try parameters-tag syntax: ToolName<parameters>{...}</parameters>
        tc = _parse_parameters_tag_syntax(inner)
        if tc:
            if VERBOSE:
                print(f"[CatPawProxy] Parsed parameters-tag syntax: {tc['function']['name']}", flush=True)
            results.append((tag_pos, end_pos, tc))
            search_pos = end_pos
            continue

        # Neither format matched — skip this tag
        search_pos = tag_pos + len(open_tag)

    return results


def _parse_tool_calls(content: str) -> tuple:
    """Parse model output for tool calls.

    Returns: (text_without_tool_calls, list_of_tool_call_dicts)
    Each tool_call_dict has: {id, type, function: {name, arguments}}
    """
    if not content:
        return content, []

    # Format 1 & 2: <tool_call> and <tool_use> tags with balanced JSON extraction
    found = _find_tag_tool_calls(content, "tool_call")
    if not found:
        found = _find_tag_tool_calls(content, "tool_use")

    if found:
        tool_calls = []
        text_parts = []
        last_end = 0

        for start_pos, end_pos, tc in found:
            text_parts.append(content[last_end:start_pos])
            tool_calls.append(tc)
            last_end = end_pos
            # CRITICAL: Only take the FIRST tool call.
            # The model should output one at a time, but sometimes it outputs
            # multiple. Taking only the first ensures sequential execution
            # and prevents Claude Code from running tools in parallel.
            break

        # Include the rest of the content after the first tool call
        if found:
            text_parts.append(content[found[0][1]:])
        else:
            text_parts.append(content[last_end:])
        clean_text = "".join(text_parts).strip()
        # Strip agent status artifacts (e.g. "◯ Goal not yet met…")
        clean_text = _strip_agent_xml(clean_text)
        # Also strip any remaining <tool_call> tags from clean_text
        clean_text = re.sub(r'</?tool_call>', '', clean_text).strip()
        clean_text = _clean_model_output_text(clean_text)
        return clean_text, tool_calls

    # Format 3: <function_calls><invoke name="..."><parameter>...</parameter></invoke></function_calls>
    fc_match = _RE_FUNCTION_CALLS.search(content)
    if fc_match:
        tool_calls = []
        text_parts = [content[:fc_match.start()]]
        invoke_text = fc_match.group(1)

        for invoke_match in _RE_INVOKE.finditer(invoke_text):
            tool_name = invoke_match.group(1)
            params_text = invoke_match.group(2)

            args = {}
            for param_match in _RE_PARAMETER.finditer(params_text):
                pname = param_match.group(1)
                pval = param_match.group(2).strip()
                args[pname] = pval

            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                }
            })

        if tool_calls:
            text_parts.append(content[fc_match.end():])
            clean_text = "".join(text_parts).strip()
            clean_text = _clean_model_output_text(clean_text)
            return clean_text, tool_calls

    # Format 4: Markdown JSON code blocks with tool call objects
    # Look for ```json {...} ``` patterns and try to parse as tool calls
    md_pattern = re.compile(r'```(?:json)?\s*(\{.*?\})\s*```', re.DOTALL)
    tool_calls = []
    text_parts = []
    last_end = 0

    for match in md_pattern.finditer(content):
        json_str = match.group(1)
        # Only treat as tool call if it has "name" and "arguments"
        if '"name"' in json_str and '"arguments"' in json_str:
            tc = _extract_tool_call(json_str)
            if tc:
                text_parts.append(content[last_end:match.start()])
                tool_calls.append(tc)
                last_end = match.end()

    if tool_calls:
        text_parts.append(content[last_end:])
        clean_text = "".join(text_parts).strip()
        clean_text = _clean_model_output_text(clean_text)
        return clean_text, tool_calls

    # Format 5: Markdown code blocks with filename (model's common Write pattern)
    # Pattern: ```language:filepath\n<content>\n```
    # The model outputs this instead of <tool_call> when it wants to create/edit files.
    # We convert these to Write tool calls.
    md_file_pattern = re.compile(
        r'```(\w+):([^\n]+)\n(.*?)```',
        re.DOTALL
    )
    tool_calls = []
    text_parts = []
    last_end = 0

    for match in md_file_pattern.finditer(content):
        lang = match.group(1).strip()
        filepath = match.group(2).strip()
        file_content = match.group(3)

        # Validate that it looks like a filepath (must contain / or .)
        if '/' not in filepath and '.' not in filepath:
            continue
        # Skip if it looks like just a language name (no path separator)
        if filepath.lower() in ('json', 'javascript', 'typescript', 'python', 'bash', 'sh', 'yaml', 'xml', 'html', 'css', 'go', 'rust', 'java', 'c', 'cpp', 'sql', 'text', 'md'):
            continue

        # Remove trailing newline from content
        if file_content.endswith('\n'):
            file_content = file_content[:-1]

        tc = {
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": "Write",
                "arguments": json.dumps({
                    "file_path": filepath,
                    "content": file_content,
                }, ensure_ascii=False),
            }
        }
        text_parts.append(content[last_end:match.start()])
        tool_calls.append(tc)
        last_end = match.end()

    if tool_calls:
        text_parts.append(content[last_end:])
        clean_text = "".join(text_parts).strip()
        clean_text = _strip_agent_xml(clean_text)
        if VERBOSE:
            print(f"[CatPawProxy] Parsed {len(tool_calls)} markdown-file-block(s) as Write calls: {[tc['function']['arguments'][:60] for tc in tool_calls]}", flush=True)
        clean_text = _clean_model_output_text(clean_text)
        return clean_text, tool_calls

    # Format 6: Bare JSON tool call (no tags at all)
    # Model outputs: {"name":"Write","arguments":{"file_path":"...","content":"..."}}
    # directly in the text without any wrapping tags.
    # We scan for JSON objects that have "name" and "arguments" keys.
    bare_tc = _find_bare_json_tool_call(content)
    if bare_tc:
        tc, start, end = bare_tc
        clean_text = (content[:start] + content[end:]).strip()
        clean_text = _strip_agent_xml(clean_text)
        clean_text = re.sub(r'</?tool_call>', '', clean_text).strip()
        clean_text = _RE_AGENT_STATUS.sub('', clean_text).strip()
        if VERBOSE:
            print(f"[CatPawProxy] Parsed bare-JSON tool call: {tc['function']['name']}", flush=True)
        clean_text = _clean_model_output_text(clean_text)
        return clean_text, [tc]

    # Format 7: Bare ToolName<parameters>{...}</parameters> (no <tool_call> wrapping)
    # The model (glm-5.2) sometimes outputs this format directly without <tool_call> tags:
    #   exec_command<parameters>{"cmd":"git status","workdir":"/path"}</parameters>
    # This is common when working with Codex CLI tools.
    bare_param_match = _RE_BARE_PARAMETERS.search(content)
    if bare_param_match:
        tc = _parse_parameters_tag_syntax(
            bare_param_match.group(1) + '<parameters>' + bare_param_match.group(2) + '</parameters>'
        )
        if tc:
            # Remove the matched region from content
            clean_text = (content[:bare_param_match.start()] + content[bare_param_match.end():]).strip()
            clean_text = _strip_agent_xml(clean_text)
            clean_text = re.sub(r'</?tool_call>', '', clean_text).strip()
            clean_text = _RE_AGENT_STATUS.sub('', clean_text).strip()
        if VERBOSE:
            print(f"[CatPawProxy] Parsed bare parameters-tag tool call: {tc['function']['name']}", flush=True)
        clean_text = _clean_model_output_text(clean_text)
        return clean_text, [tc]

    # No tool calls found
    # Also strip <tool_call> tags that failed to parse
    content = _strip_agent_xml(content)
    content = re.sub(r'</?tool_call>', '', content)
    # Strip any remaining ◯ status lines
    content = _RE_AGENT_STATUS.sub('', content)
    content = _clean_model_output_text(content)
    return content.strip(), []


# ---------------------------------------------------------------------------
# Tool prompt injection + message conversion
# ---------------------------------------------------------------------------

def _inject_tools_prompt(tools: list) -> str:
    """Convert OpenAI tools array to a COMPACT text system prompt.

    Uses a condensed one-line-per-tool format to minimize request body size.
    CatPawAI upstream has a body size limit (~256KB encrypted), so we must
    keep tool definitions as short as possible while retaining enough info
    for the model to call tools correctly.
    """
    if not tools:
        return ""

    lines = [
        "## Tools",
        'Output: <tool_call>{"name":"ToolName","arguments":{"param":"value"}}</tool_call>',
        "ONE tool call per response. Wait for result before continuing.",
        "",
    ]

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        func = tool.get("function", tool)
        name = func.get("name", "")
        if not name:
            continue
        # Truncate description to first sentence, max 120 chars
        desc = func.get("description", "")
        # Take only the first sentence or first 120 chars, whichever is shorter
        first_period = desc.find(". ")
        if first_period > 0 and first_period < 120:
            desc = desc[:first_period + 1]
        elif len(desc) > 120:
            desc = desc[:117] + "..."

        params = func.get("parameters", {})
        param_str = ""
        if params and isinstance(params, dict):
            props = params.get("properties", {})
            required = params.get("required", [])
            param_parts = []
            for pname, pinfo in props.items():
                req_mark = "!" if pname in required else ""
                param_parts.append(f"{pname}{req_mark}")
            param_str = ",".join(param_parts)

        if param_str:
            lines.append(f"- {name}({param_str}): {desc}")
        else:
            lines.append(f"- {name}: {desc}")

    lines.append("")
    return "\n".join(lines)


def _normalize_assistant_content(content: str) -> str:
    """Normalize bare JSON tool calls in assistant content to <tool_call> format.

    When the model previously output bare JSON (without <tool_call> tags), Codex CLI
    parsed and executed it, then stored the result in the same assistant message.
    This creates polluted conversation history like:
        {"name":"exec_command","arguments":{"cmd":"git status"}}\\n\\nTool Result: ...

    This function detects and wraps bare JSON tool calls in <tool_call> tags so the
    model sees consistent formatting in conversation history.
    """
    if not content or len(content) < 20:
        return content

    # Quick check: if there's no {"name" pattern, skip entirely
    if '{"name"' not in content and '{ "name"' not in content:
        return content

    # Also skip if content already has <tool_call> tags (already normalized)
    if '<tool_call>' in content:
        return content

    # Try to find bare JSON tool calls
    bare_tc = _find_bare_json_tool_call(content)
    if bare_tc:
        tc, start, end = bare_tc
        # Wrap the bare JSON in <tool_call> tags
        tc_json = json.dumps({
            "name": tc["function"]["name"],
            "arguments": json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
        }, ensure_ascii=False)
        normalized = content[:start] + f'<tool_call>{tc_json}</tool_call>' + content[end:]
        if VERBOSE:
            print(f"[CatPawProxy] Normalized bare JSON in assistant history: {tc['function']['name']}", flush=True)
        return normalized

    # Fallback: _find_bare_json_tool_call may fail due to invalid JSON escapes
    # (e.g. \\* in Codex history). Try a heuristic approach:
    # 1. Find {"name" in content
    # 2. Find the closing }} after it
    # 3. Extract, fix escapes, and try to parse
    name_idx = content.find('{"name"')
    if name_idx == -1:
        name_idx = content.find('{ "name"')
    if name_idx != -1:
        # Find closing }} — look for double closing brace
        search_from = name_idx
        while True:
            brace_idx = content.find('}}', search_from)
            if brace_idx == -1:
                break
            # Extract candidate JSON
            candidate = content[name_idx:brace_idx + 2]
            # Fix invalid escapes and try to parse
            fixed = _fix_invalid_json_escapes(candidate)
            tc = _extract_tool_call(fixed)
            if tc:
                tc_json = json.dumps({
                    "name": tc["function"]["name"],
                    "arguments": json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
                }, ensure_ascii=False)
                normalized = content[:name_idx] + f'<tool_call>{tc_json}</tool_call>' + content[brace_idx + 2:]
                if VERBOSE:
                    print(f"[CatPawProxy] Normalized bare JSON (heuristic fallback): {tc['function']['name']}", flush=True)
                return normalized
            search_from = brace_idx + 2

    # Fallback 2: Truncated JSON — the compactor may have cut off the end of
    # a bare JSON tool call, leaving it without closing " and }}. Try to
    # reconstruct the JSON by finding {"name" and adding missing closers.
    if name_idx != -1:
        remainder = content[name_idx:]
        # Check if this looks like a truncated tool call JSON
        # (has "name" and "arguments" but no proper closing)
        if '"name"' in remainder and '"arguments"' in remainder:
            # Try progressively adding missing closing characters
            for suffix in ['"', '"}', '"}}', '"}}', '"}', '"}}}', '":{}}', '"}]}']:
                candidate = remainder.rstrip() + suffix
                fixed = _fix_invalid_json_escapes(candidate)
                tc = _extract_tool_call(fixed)
                if tc:
                    tc_json = json.dumps({
                        "name": tc["function"]["name"],
                        "arguments": json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
                    }, ensure_ascii=False)
                    normalized = content[:name_idx] + f'<tool_call>{tc_json}</tool_call>'
                    if VERBOSE:
                        print(f"[CatPawProxy] Normalized truncated bare JSON (reconstructed): {tc['function']['name']}", flush=True)
                    return normalized

    return content


def _convert_messages_with_tools(messages: list) -> str:
    """Convert OpenAI messages (including tool_calls and tool role) to text.

    Handles:
    - system role: pass through as context
    - user role: prefix with 'Human:'
    - assistant role with tool_calls: convert to text showing what tools were called
    - tool role: convert to 'Tool Result:' text
    """
    parts = []
    for msg in messages:
        role = msg.get("role", "user")

        if role == "system":
            content = _extract_text_content(msg.get("content", ""))
            if content:
                parts.append(content)
            continue

        if role == "tool":
            # Tool result message
            content = _extract_text_content(msg.get("content", ""))
            # Sanitize Codex CLI internal metadata (Chunk ID, Wall time, etc.)
            content = _sanitize_tool_result_content(content)
            parts.append(f"Tool Result: {content}")
            continue

        if role == "assistant":
            content = _extract_text_content(msg.get("content", ""))
            tool_calls = msg.get("tool_calls", [])

            # Normalize bare JSON tool calls in content — ALWAYS call, even when
            # tool_calls exist. Codex CLI sometimes stores the original bare JSON
            # text in content alongside the parsed tool_calls, causing the model
            # to see both formats and get confused.
            if content:
                content = _normalize_assistant_content(content)

            # After normalization, if content still has bare JSON that couldn't
            # be parsed (e.g. invalid escapes), strip it to prevent pollution.
            # Only keep content that has actual text (not just bare JSON).
            if content and tool_calls:
                stripped = content.strip()
                # Drop content entirely if it's ONLY a tool call (bare JSON or
                # <tool_call> tag) — the tool_calls array already captures it.
                if (stripped.startswith('{"name"') and stripped.endswith('}}')) or \
                   (stripped.startswith('<tool_call>') and stripped.endswith('</tool_call>')):
                    content = ""
                else:
                    # Content has text + tool call. Remove <tool_call> blocks
                    # from content since tool_calls array already has them.
                    # This prevents duplicate tool calls in the conversation.
                    content = re.sub(
                        r'<tool_call>.*?</tool_call>', '', content, flags=re.DOTALL
                    ).strip()

            if content:
                parts.append(f"Assistant: {content}")

            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    tc_name = func.get("name", "")
                    tc_args = func.get("arguments", "{}")
                    parts.append(f'<tool_call>{{"name":"{tc_name}","arguments":{tc_args}}}</tool_call>')
            continue

        # Default: user or other roles
        content = _extract_text_content(msg.get("content", ""))
        if content:
            parts.append(f"Human: {content}")

    return "\n\n".join(parts)


def _strip_agent_xml(content: str) -> str:
    """Strip agent-mode XML artifacts from model output.

    Removes:
      - <function_calls>, <invoke>, <parameter> blocks
      - <antThinking>, <plan>, <think> blocks
      - Orphaned tags (opening or closing without match)
      - Agent status artifacts like "◯ Goal not yet met…"
    """
    if not content:
        return content

    # Remove common agent XML tags and their content (precompiled patterns)
    for pattern in _RE_AGENT_PATTERNS:
        content = pattern.sub('', content)

    # Also remove orphaned opening/closing tags
    for tag in _AGENT_ORPHAN_TAGS:
        content = content.replace(tag, '')

    # Remove agent status artifacts
    content = _RE_AGENT_STATUS.sub('', content)

    return content


# ---------------------------------------------------------------------------
# Tool result sanitization — strip Codex CLI internal metadata
# ---------------------------------------------------------------------------

def _sanitize_tool_result_content(content: str) -> str:
    """Strip Codex CLI internal debugging metadata from tool result content.

    Codex CLI wraps tool results with internal metadata:
        Chunk ID: 382ad3
        Wall time:: 0.0000 seconds
        Process failed (exit code 1)
        Original token comm: 0
        Output:
        <actual content>

    This metadata leaks into model context and causes the model to echo it
    in its output, creating display pollution. We strip it to keep only
    the actual content.

    Returns the cleaned content, or a placeholder if no actual content
    remains after stripping.
    """
    if not content or len(content) < 20:
        return content

    # Quick check: if no metadata markers, skip entirely
    if 'Chunk ID:' not in content and 'Wall time:' not in content:
        return content

    original_len = len(content)

    # Strategy 1: Find "Output:" separator and take everything after it
    # This is the cleanest approach — Output: marks the start of actual content
    output_match = _RE_CODEX_OUTPUT_SEPARATOR.search(content)
    if output_match:
        actual = content[output_match.end():]
        if actual.strip():
            if VERBOSE:
                print(f"[CatPawProxy] Sanitized tool result: stripped {original_len - len(actual)} chars of Codex metadata", flush=True)
            return actual.strip()
        # Output: exists but nothing after it — process failed with no output
        if VERBOSE:
            print(f"[CatPawProxy] Sanitized tool result: no output after metadata", flush=True)
        return "[Process completed, no output]"

    # Strategy 2: Strip individual metadata lines
    cleaned = _RE_CODEX_METADATA_LINES.sub('', content)
    cleaned = cleaned.strip()

    if not cleaned:
        if VERBOSE:
            print(f"[CatPawProxy] Sanitized tool result: all content was metadata", flush=True)
        return "[Process completed, no output]"

    if len(cleaned) < original_len and VERBOSE:
        print(f"[CatPawProxy] Sanitized tool result: stripped {original_len - len(cleaned)} chars of Codex metadata", flush=True)

    return cleaned


# ---------------------------------------------------------------------------
# Model output cleaning — strip simulated tool results and residual bare JSON
# ---------------------------------------------------------------------------

def _clean_model_output_text(text: str) -> str:
    """Clean model output text by removing simulated tool results and bare JSON.

    After _parse_tool_calls extracts the first tool call, the remaining text
    (clean_text) may contain:
    1. Simulated "Tool Result:" blocks — the model hallucinates tool execution
       output instead of waiting for the actual result
    2. Residual bare JSON tool calls — additional {"name":"...","arguments":...}
       objects that weren't the first tool call
    3. Status narratives like "已读取 N 个文件" that add noise

    This function strips all of these to produce clean display text.
    """
    if not text:
        return text

    # Strip <tool_call>...</tool_call> blocks first (entire block including
    # content). This must happen BEFORE bare JSON detection so that JSON
    # inside remaining <tool_call> tags doesn't survive as bare JSON.
    text = re.sub(r'<tool_call>.*?</tool_call>', '', text, flags=re.DOTALL)
    # Also strip orphaned <tool_call> or </tool_call> tags (no matching pair)
    text = re.sub(r'</?tool_call>', '', text).strip()

    # Strip residual bare JSON tool calls (any that were inside <tool_call>
    # tags are now bare after tag removal above)
    bare_tc = _find_bare_json_tool_call(text)
    while bare_tc:
        tc, start, end = bare_tc
        text = (text[:start] + text[end:]).strip()
        bare_tc = _find_bare_json_tool_call(text)

    # Strip simulated "Tool Result:" blocks — everything from "Tool Result:"
    # at start of a line to the end of text. The model should NEVER output
    # "Tool Result:" — it should wait for the actual result from the CLI.
    tool_result_match = _RE_SIMULATED_TOOL_RESULT.search(text)
    if tool_result_match:
        text = text[:tool_result_match.start()].rstrip()

    # Strip status narratives (Chinese: "已读取 N 个文件")
    text = re.sub(r'^已读取\s*\d+\s*个文件\s*\n?', '', text, flags=re.MULTILINE)

    # Clean up excessive whitespace left by removals
    text = re.sub(r'\n{3,}', '\n\n', text).strip()

    return text
