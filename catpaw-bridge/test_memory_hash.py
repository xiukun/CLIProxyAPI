#!/usr/bin/env python3
"""Regression test for memory hash consistency (Finding 1 from eng review).

Tests that save_memory and load_memory use compatible hashes, even when
compaction modifies user message content between save and load.

Bug: save_memory computed _conv_hash on post-compaction messages (where
Phase 2 had summarized old user messages to 200 chars), while load_memory
computed _conv_hash on pre-compaction messages. The hashes never matched,
so memory was never loaded.

Also tests the two-phase hashing strategy (exclude_last):
  - save_memory uses exclude_last=False (all user messages) for STORAGE
  - load_memory uses exclude_last=True (drop last user msg) for LOOKUP
  This allows request N+1 to find the memory saved by request N.
"""

import json
import os
import sys
import tempfile
import shutil

sys.path.insert(0, ".")

# Patch the memory directory to a temp dir
import proxy.memory as mem_mod

_TEMP_DIR = tempfile.mkdtemp(prefix="catpaw_test_memory_")
mem_mod._MEMORY_DIR = type(mem_mod._MEMORY_DIR)(_TEMP_DIR)  # Path object
# Re-derive _MEMORY_DIR as a Path
from pathlib import Path
mem_mod._MEMORY_DIR = Path(_TEMP_DIR)


def _make_messages(n_user: int, prefix: str = "Hello") -> list:
    """Build a message list with n_user user messages (alternating with assistant)."""
    msgs = []
    for i in range(n_user):
        msgs.append({"role": "user", "content": f"{prefix} user message {i} " + "x" * 300})
        msgs.append({"role": "assistant", "content": f"Assistant reply {i}"})
    return msgs


def test_basic_save_load_roundtrip():
    """Test that memory saved on request N can be loaded on request N+1."""
    os.environ["CATPAW_PROXY_VERBOSE"] = "0"
    conv_id = "test-conv-001"

    # Request N: 35 messages (> _SUMMARY_THRESHOLD=30)
    msgs_n = _make_messages(18)  # 18 user + 18 assistant = 36 messages
    assert len(msgs_n) >= 30, f"Need >= 30 messages, got {len(msgs_n)}"

    # Compute hash as translator.py does (before compaction)
    pre_hash = mem_mod._conv_hash(msgs_n, conv_id, exclude_last=False)

    # Save with pre-computed hash
    mem_mod.save_memory(msgs_n, conv_id, conv_hash=pre_hash)

    # Verify memory file exists
    from proxy.memory import _memory_path
    path = _memory_path(pre_hash)
    assert path.exists(), f"Memory file not created at {path}"

    # Request N+1: add one more user message
    msgs_n1 = msgs_n + [{"role": "user", "content": "New user message"}]

    # Load should find the memory (exclude_last drops the new message)
    loaded = mem_mod.load_memory(msgs_n1, conv_id)
    assert loaded is not None, "Memory should have been loaded on N+1 request"
    assert "summary" in loaded, "Loaded memory should contain summary"
    assert loaded["msg_count"] > 0, "Summary should cover messages"

    print("  PASS: Basic save/load roundtrip works")


def test_hash_changes_without_exclude_last():
    """Verify that without exclude_last, adding a message changes the hash."""
    conv_id = "test-conv-002"
    msgs_5 = _make_messages(5)
    msgs_6 = msgs_5 + [{"role": "user", "content": "Sixth message"}]

    h5 = mem_mod._conv_hash(msgs_5, conv_id, exclude_last=False)
    h6_all = mem_mod._conv_hash(msgs_6, conv_id, exclude_last=False)
    h6_excl = mem_mod._conv_hash(msgs_6, conv_id, exclude_last=True)

    assert h5 != h6_all, "Hash should change when a message is added"
    assert h5 == h6_excl, "exclude_last should make N+1 lookup hash == N storage hash"

    print("  PASS: Two-phase hashing (exclude_last) works correctly")


def test_compaction_doesnt_break_hash():
    """Test that compaction modifying user messages doesn't break load."""
    conv_id = "test-conv-003"

    # 40 messages with large user content (will be compacted)
    msgs = _make_messages(20, prefix="LongMessage")

    # Simulate translator.py flow:
    # 1. Compute hash BEFORE compaction
    pre_hash = mem_mod._conv_hash(msgs, conv_id, exclude_last=False)

    # 2. Save with pre-compaction hash and original messages
    mem_mod.save_memory(msgs, conv_id, conv_hash=pre_hash)

    # 3. Simulate compaction: Phase 2 summarizes old user messages
    #    The real compactor adds "[user] " prefix + first 200 chars + suffix,
    #    which changes content[:200] and thus the hash.
    compacted = []
    for i, msg in enumerate(msgs):
        msg_copy = dict(msg)
        if msg_copy.get("role") == "user":
            content = msg_copy["content"]
            if len(content) > 200:
                # Match compactor.py Phase 2 format exactly
                summary = content[:200].rstrip()
                suffix = f"... [summarized, original {len(content)} chars]"
                msg_copy["content"] = f"[user] {summary}{suffix}"
        compacted.append(msg_copy)

    # 4. Hash of compacted messages should be DIFFERENT from pre-compaction hash
    post_hash = mem_mod._conv_hash(compacted, conv_id, exclude_last=False)
    assert post_hash != pre_hash, "Compaction should change the hash"

    # 5. But load_memory on N+1 (with original messages + 1 new) should still find it
    msgs_n1 = msgs + [{"role": "user", "content": "Next message"}]
    loaded = mem_mod.load_memory(msgs_n1, conv_id)
    assert loaded is not None, "Memory should load despite compaction modifying messages"

    print("  PASS: Compaction doesn't break hash consistency")


def test_save_with_conv_hash_uses_provided_hash():
    """Test that save_memory uses the provided conv_hash instead of computing its own."""
    conv_id = "test-conv-004"
    msgs = _make_messages(20)

    custom_hash = "custom_hash_12345"
    mem_mod.save_memory(msgs, conv_id, conv_hash=custom_hash)

    from proxy.memory import _memory_path
    path = _memory_path(custom_hash)
    assert path.exists(), "Memory file should use the provided hash"

    # The default hash should NOT match
    default_hash = mem_mod._conv_hash(msgs, conv_id, exclude_last=False)
    assert default_hash != custom_hash, "Custom hash should differ from default"

    default_path = _memory_path(default_hash)
    assert not default_path.exists(), "Default hash path should not have a file"

    print("  PASS: save_memory uses provided conv_hash")


def test_short_conversation_no_memory():
    """Test that short conversations (< 30 messages) don't save/load memory."""
    conv_id = "test-conv-005"
    short_msgs = _make_messages(5)  # 10 messages, < 30

    result = mem_mod.load_memory(short_msgs, conv_id)
    assert result is None, "Short conversation should not load memory"

    mem_mod.save_memory(short_msgs, conv_id)
    # No file should be created
    files = list(Path(_TEMP_DIR).glob("*.json"))
    assert len(files) == 0 or all(
        f.stat().st_size == 0 for f in files
    ), "Short conversation should not save memory"

    print("  PASS: Short conversations skip memory")


def test_memory_summary_content():
    """Test that the saved summary contains meaningful content."""
    conv_id = "test-conv-006"
    msgs = _make_messages(20, prefix="DetailedTask")

    pre_hash = mem_mod._conv_hash(msgs, conv_id, exclude_last=False)
    mem_mod.save_memory(msgs, conv_id, conv_hash=pre_hash)

    from proxy.memory import _memory_path
    path = _memory_path(pre_hash)
    with open(path) as f:
        data = json.load(f)

    assert data["msg_count"] > 0, "Summary should cover messages"
    assert len(data["summary"]) > 0, "Summary should have content"
    assert "DetailedTask" in data["summary"], "Summary should contain user message content"
    assert data["conversation_id"] == conv_id

    print("  PASS: Memory summary contains meaningful content")


# ---- Cleanup ----
def cleanup():
    shutil.rmtree(_TEMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    print("Testing memory hash consistency (Finding 1 fix)...")
    print()

    tests = [
        test_basic_save_load_roundtrip,
        test_hash_changes_without_exclude_last,
        test_compaction_doesnt_break_hash,
        test_save_with_conv_hash_uses_provided_hash,
        test_short_conversation_no_memory,
        test_memory_summary_content,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            # Clean memory dir before each test
            for f in Path(_TEMP_DIR).glob("*.json"):
                f.unlink()
            test()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    cleanup()

    print()
    print(f"{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
    if failed:
        sys.exit(1)
    print("All tests passed!")
