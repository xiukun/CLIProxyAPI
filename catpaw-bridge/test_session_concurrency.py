#!/usr/bin/env python3
"""Test session concurrency safety (Finding 2 fix).

Tests that session.py:
  1. get_or_create_conversation_id is async (uses asyncio.Lock)
  2. Concurrent calls for the same conversation get the same ID
  3. Concurrent calls for different conversations get different IDs
  4. The lock doesn't deadlock under normal usage
"""

import asyncio
import sys

sys.path.insert(0, ".")

# Suppress verbose output during tests
import os
os.environ["CATPAW_PROXY_VERBOSE"] = "0"

import proxy.session as session_mod
from proxy.session import get_or_create_conversation_id


def _make_messages(n_user: int, prefix: str = "Hello") -> list:
    """Build a message list with n_user user messages (alternating with assistant)."""
    msgs = []
    for i in range(n_user):
        msgs.append({"role": "user", "content": f"{prefix} user message {i}"})
        msgs.append({"role": "assistant", "content": f"Assistant reply {i}"})
    return msgs


async def test_async_function():
    """Test that get_or_create_conversation_id is awaitable (async)."""
    msgs = _make_messages(3)
    result = await get_or_create_conversation_id(msgs)
    assert isinstance(result, tuple), "Should return a tuple"
    assert len(result) == 2, "Should return (conversationId, is_new)"
    assert isinstance(result[0], str), "conversationId should be a string"
    assert isinstance(result[1], bool), "is_new should be a bool"

    print("  PASS: Function is async and returns correct type")


async def test_same_conversation_same_id():
    """Test that a conversation continuation preserves the ID.

    Two-phase hashing: same messages called twice = MISS (by design).
    But Turn 2 (with one more message) should HIT Turn 1's storage hash.
    """
    session_mod._session_store.clear()

    # Turn 1: 3 user messages
    msgs_turn1 = _make_messages(3, prefix="TestA")
    id1, is_new1 = await get_or_create_conversation_id(msgs_turn1)
    assert is_new1, "First call should be new"

    # Turn 2: add one more user+assistant pair (continuation)
    msgs_turn2 = msgs_turn1 + [
        {"role": "user", "content": "TestA user message 3"},
        {"role": "assistant", "content": "Reply 3"},
    ]
    id2, is_new2 = await get_or_create_conversation_id(msgs_turn2)
    assert not is_new2, "Continuation should be cache HIT"
    assert id1 == id2, f"Continuation should preserve ID: {id1} != {id2}"

    print("  PASS: Conversation continuation preserves ID")


async def test_different_conversations_different_ids():
    """Test that different message sequences get different IDs."""
    session_mod._session_store.clear()

    msgs_a = _make_messages(3, prefix="ConvA")
    msgs_b = _make_messages(3, prefix="ConvB")

    id_a, _ = await get_or_create_conversation_id(msgs_a)
    id_b, _ = await get_or_create_conversation_id(msgs_b)

    assert id_a != id_b, "Different conversations should get different IDs"

    print("  PASS: Different conversations get different IDs")


async def test_concurrent_same_messages():
    """Test that concurrent continuation calls get the same ID.

    Two-phase hashing: same messages = always MISS (by design).
    But concurrent continuations of an existing conversation should all HIT.
    """
    session_mod._session_store.clear()

    # Turn 1: store the conversation
    msgs_turn1 = _make_messages(3, prefix="Concurrent")
    id1, _ = await get_or_create_conversation_id(msgs_turn1)

    # Turn 2: all 10 concurrent calls are continuations (one more message)
    msgs_turn2 = msgs_turn1 + [
        {"role": "user", "content": "Concurrent user message 3"},
        {"role": "assistant", "content": "Reply 3"},
    ]
    tasks = [get_or_create_conversation_id(msgs_turn2) for _ in range(10)]
    results = await asyncio.gather(*tasks)

    ids = set(r[0] for r in results)
    assert len(ids) == 1, f"Concurrent continuations should all get same ID, got {len(ids)}: {ids}"
    assert id1 in ids, "Continuation ID should match original"

    print("  PASS: Concurrent continuation calls get same ID (no race condition)")


async def test_concurrent_different_messages():
    """Test that concurrent continuations of different conversations don't interfere."""
    session_mod._session_store.clear()

    # Store two different conversations
    msgs_a_t1 = _make_messages(3, prefix="ParallelA")
    msgs_b_t1 = _make_messages(3, prefix="ParallelB")
    id_a, _ = await get_or_create_conversation_id(msgs_a_t1)
    id_b, _ = await get_or_create_conversation_id(msgs_b_t1)

    # Concurrent continuations of both
    msgs_a_t2 = msgs_a_t1 + [{"role": "user", "content": "ParallelA msg 3"}, {"role": "assistant", "content": "Reply"}]
    msgs_b_t2 = msgs_b_t1 + [{"role": "user", "content": "ParallelB msg 3"}, {"role": "assistant", "content": "Reply"}]

    tasks = [
        get_or_create_conversation_id(msgs_a_t2),
        get_or_create_conversation_id(msgs_b_t2),
        get_or_create_conversation_id(msgs_a_t2),  # repeat
        get_or_create_conversation_id(msgs_b_t2),  # repeat
    ]
    results = await asyncio.gather(*tasks)

    id_a2 = results[0][0]
    id_b2 = results[1][0]
    id_a3 = results[2][0]
    id_b3 = results[3][0]

    assert id_a == id_a2 == id_a3, "Conversation A continuations should match"
    assert id_b == id_b2 == id_b3, "Conversation B continuations should match"
    assert id_a != id_b, "Different conversations should have different IDs"

    print("  PASS: Concurrent different-conversation calls don't interfere")


async def test_conversation_continuation():
    """Test that adding a message to an existing conversation preserves the ID."""
    session_mod._session_store.clear()

    msgs_turn1 = _make_messages(3, prefix="Continue")
    id1, is_new1 = await get_or_create_conversation_id(msgs_turn1)
    assert is_new1

    # Turn 2: add one more user+assistant pair
    msgs_turn2 = msgs_turn1 + [
        {"role": "user", "content": "Continue user message 3"},
        {"role": "assistant", "content": "Continue reply 3"},
    ]
    id2, is_new2 = await get_or_create_conversation_id(msgs_turn2)
    assert not is_new2, "Should be a continuation (cache hit)"
    assert id1 == id2, "Continuation should preserve conversation ID"

    print("  PASS: Conversation continuation preserves ID")


async def test_ttl_expiry():
    """Test that expired sessions are cleaned up."""
    session_mod._session_store.clear()

    # Manually insert an expired session
    import time
    old_time = time.time() - session_mod._SESSION_TTL - 1
    session_mod._session_store["expired_hash"] = {
        "conversationId": "old-conv-id",
        "count": 5,
        "ts": old_time,
    }

    # Making a new request should trigger cleanup
    msgs = _make_messages(2, prefix="AfterExpiry")
    await get_or_create_conversation_id(msgs)

    # The expired session should be gone
    assert "expired_hash" not in session_mod._session_store, "Expired session should be cleaned up"

    print("  PASS: Expired sessions are cleaned up")


async def test_max_sessions_cleanup():
    """Test that old sessions are removed when exceeding _MAX_SESSIONS."""
    session_mod._session_store.clear()

    # Fill with many different conversations
    import time
    for i in range(session_mod._MAX_SESSIONS + 10):
        session_mod._session_store[f"hash_{i}"] = {
            "conversationId": f"conv_{i}",
            "count": 2,
            "ts": time.time() - i,  # older ones have smaller ts
        }

    # Next call should trigger cleanup
    msgs = _make_messages(2, prefix="MaxSessions")
    await get_or_create_conversation_id(msgs)

    assert len(session_mod._session_store) <= session_mod._MAX_SESSIONS, \
        f"Session store should be <= {session_mod._MAX_SESSIONS}, got {len(session_mod._session_store)}"

    print("  PASS: Old sessions removed when exceeding max")


async def test_no_deadlock():
    """Test that rapid sequential calls don't deadlock."""
    session_mod._session_store.clear()

    for i in range(50):
        msgs = _make_messages(2, prefix=f"NoDeadlock{i}")
        await get_or_create_conversation_id(msgs)

    # If we get here, no deadlock
    print("  PASS: No deadlock under rapid sequential calls")


def run_async_test(test_func):
    """Helper to run an async test."""
    asyncio.run(test_func())


if __name__ == "__main__":
    print("Testing session concurrency safety (Finding 2 fix)...")
    print()

    tests = [
        test_async_function,
        test_same_conversation_same_id,
        test_different_conversations_different_ids,
        test_concurrent_same_messages,
        test_concurrent_different_messages,
        test_conversation_continuation,
        test_ttl_expiry,
        test_max_sessions_cleanup,
        test_no_deadlock,
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
