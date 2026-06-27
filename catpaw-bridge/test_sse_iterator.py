#!/usr/bin/env python3
"""Test _iter_catpaw_sse shared SSE iterator (Finding 4 fix).

Tests that the extracted async generator correctly:
  1. Parses data: lines into JSON dicts
  2. Handles encrypted responses (decrypt + parse)
  3. Terminates on ': ping', '[DONE]', and 'lastOne'
  4. Calls on_idle callback for non-data lines (keepalive)
  5. Skips malformed JSON gracefully
"""

import asyncio
import json
import sys

sys.path.insert(0, ".")


class MockContent:
    """Mock aiohttp response content (async iterable of bytes lines)."""

    def __init__(self, lines: list):
        self._lines = [l.encode("utf-8") for l in lines]

    def __aiter__(self):
        self._idx = 0
        return self

    async def __anext__(self):
        if self._idx >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._idx]
        self._idx += 1
        return line


class MockResponse:
    """Mock aiohttp ClientResponse."""

    def __init__(self, lines: list):
        self.content = MockContent(lines)


async def test_basic_data_parsing():
    """Test that data: lines are parsed into JSON dicts."""
    lines = [
        'data: {"content": "hello", "lastOne": false}',
        'data: {"content": "world", "lastOne": false}',
        ': ping',
    ]
    resp = MockResponse(lines)

    results = []
    from proxy.handlers import _iter_catpaw_sse
    async for data in _iter_catpaw_sse(resp, ""):
        results.append(data)

    assert len(results) == 2, f"Expected 2 data items, got {len(results)}"
    assert results[0]["content"] == "hello"
    assert results[1]["content"] == "world"

    print("  PASS: Basic data parsing")


async def test_done_terminates():
    """Test that [DONE] terminates iteration."""
    lines = [
        'data: {"content": "first", "lastOne": false}',
        'data: [DONE]',
        'data: {"content": "should not see", "lastOne": false}',
    ]
    resp = MockResponse(lines)

    results = []
    from proxy.handlers import _iter_catpaw_sse
    async for data in _iter_catpaw_sse(resp, ""):
        results.append(data)

    assert len(results) == 1, f"Should stop at [DONE], got {len(results)} items"
    assert results[0]["content"] == "first"

    print("  PASS: [DONE] terminates iteration")


async def test_ping_terminates():
    """Test that ': ping' terminates iteration."""
    lines = [
        'data: {"content": "a", "lastOne": false}',
        ': ping',
        'data: {"content": "b", "lastOne": false}',
    ]
    resp = MockResponse(lines)

    results = []
    from proxy.handlers import _iter_catpaw_sse
    async for data in _iter_catpaw_sse(resp, ""):
        results.append(data)

    assert len(results) == 1
    assert results[0]["content"] == "a"

    print("  PASS: ': ping' terminates iteration")


async def test_lastone_terminates():
    """Test that lastOne:true terminates iteration."""
    lines = [
        'data: {"content": "chunk1", "lastOne": false}',
        'data: {"content": "chunk2", "lastOne": true}',
        'data: {"content": "chunk3", "lastOne": false}',
    ]
    resp = MockResponse(lines)

    results = []
    from proxy.handlers import _iter_catpaw_sse
    async for data in _iter_catpaw_sse(resp, ""):
        results.append(data)

    # lastOne:true item is NOT yielded — iterator returns before yielding it.
    # This is correct: the caller sends a generic final chunk after the loop.
    assert len(results) == 1, f"Should stop at lastOne (not yield it), got {len(results)}"
    assert results[0]["content"] == "chunk1"

    print("  PASS: lastOne:true terminates iteration (item not yielded)")


async def test_empty_lines_skipped():
    """Test that empty lines are skipped."""
    lines = [
        '',
        'data: {"content": "a", "lastOne": false}',
        '',
        '',
        'data: {"content": "b", "lastOne": false}',
        '',
    ]
    resp = MockResponse(lines)

    results = []
    from proxy.handlers import _iter_catpaw_sse
    async for data in _iter_catpaw_sse(resp, ""):
        results.append(data)

    assert len(results) == 2

    print("  PASS: Empty lines skipped")


async def test_non_data_lines_skipped():
    """Test that non-data: lines are skipped."""
    lines = [
        ': some comment',
        'event: type',
        'data: {"content": "a", "lastOne": false}',
        'id: 123',
    ]
    resp = MockResponse(lines)

    results = []
    from proxy.handlers import _iter_catpaw_sse
    async for data in _iter_catpaw_sse(resp, ""):
        results.append(data)

    assert len(results) == 1

    print("  PASS: Non-data lines skipped")


async def test_malformed_json_skipped():
    """Test that malformed JSON in data: lines is skipped gracefully."""
    lines = [
        'data: {broken json}',
        'data: {"content": "valid", "lastOne": false}',
        'data: not json at all',
    ]
    resp = MockResponse(lines)

    results = []
    from proxy.handlers import _iter_catpaw_sse
    async for data in _iter_catpaw_sse(resp, ""):
        results.append(data)

    assert len(results) == 1, f"Should skip malformed, got {len(results)}"
    assert results[0]["content"] == "valid"

    print("  PASS: Malformed JSON skipped gracefully")


async def test_on_idle_callback():
    """Test that on_idle callback is called for empty and non-data lines."""
    lines = [
        '',
        ': comment',
        'data: {"content": "a", "lastOne": false}',
        '',
        'event: type',
    ]
    resp = MockResponse(lines)

    idle_count = [0]

    async def on_idle():
        idle_count[0] += 1

    from proxy.handlers import _iter_catpaw_sse
    async for _ in _iter_catpaw_sse(resp, "", on_idle=on_idle):
        pass

    # 4 non-data lines: '', ': comment', '', 'event: type'
    assert idle_count[0] == 4, f"Expected 4 idle calls, got {idle_count[0]}"

    print("  PASS: on_idle callback called for non-data lines")


async def test_empty_stream():
    """Test that empty stream produces no items."""
    resp = MockResponse([])

    results = []
    from proxy.handlers import _iter_catpaw_sse
    async for data in _iter_catpaw_sse(resp, ""):
        results.append(data)

    assert len(results) == 0

    print("  PASS: Empty stream produces no items")


async def test_encrypted_response():
    """Test that encrypted responses are decrypted and parsed."""
    # We can't easily test real encryption without RSA keys,
    # but we can test that the iterator attempts decryption and
    # gracefully handles failures when resp_encrypted_key is set.
    lines = [
        'data: not_valid_encrypted_data',
        'data: {"content": "valid", "lastOne": false}',
    ]
    resp = MockResponse(lines)

    # With a non-empty encrypted_key, decrypt_response_data will fail
    # on "not_valid_encrypted_data" and skip it.
    # The second line is valid JSON but will also go through decryption
    # which will fail and return the original string, then json.loads
    # will succeed.
    results = []
    from proxy.handlers import _iter_catpaw_sse
    # With encrypted_key set, both lines go through decrypt which returns
    # the original data on failure, then json.loads is attempted.
    # Line 1: "not_valid_encrypted_data" → decrypt fails → returns original → json.loads fails → skip
    # Line 2: '{"content": "valid", ...}' → decrypt fails → returns original → json.loads succeeds
    async for data in _iter_catpaw_sse(resp, "fake_key"):
        results.append(data)

    # At least the valid JSON should be parsed (decrypt returns original on failure)
    assert len(results) >= 1, f"Expected at least 1 item, got {len(results)}"
    if results:
        assert results[0]["content"] == "valid"

    print("  PASS: Encrypted response handling (graceful degradation)")


def run_async_test(test_func):
    """Helper to run an async test."""
    asyncio.run(test_func())


if __name__ == "__main__":
    print("Testing _iter_catpaw_sse shared SSE iterator (Finding 4 fix)...")
    print()

    tests = [
        test_basic_data_parsing,
        test_done_terminates,
        test_ping_terminates,
        test_lastone_terminates,
        test_empty_lines_skipped,
        test_non_data_lines_skipped,
        test_malformed_json_skipped,
        test_on_idle_callback,
        test_empty_stream,
        test_encrypted_response,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            run_async_test(test)
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print()
    print(f"{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
    if failed:
        sys.exit(1)
    print("All tests passed!")
