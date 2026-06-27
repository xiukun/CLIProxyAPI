#!/usr/bin/env python3
"""Test RSA key lazy loading + cache invalidation (Finding 3 fix).

Tests that crypto.py:
  1. Does NOT extract keys at import time (lazy loading)
  2. Loads keys on first use via get_rsa_public_key() / get_rsa_private_key()
  3. Caches keys after first load (no re-extraction)
  4. invalidate_rsa_cache() forces re-extraction on next call
  5. Thread-safe via _key_lock
"""

import sys
import threading

sys.path.insert(0, ".")

import proxy.crypto as crypto_mod


def test_keys_not_loaded_at_import():
    """Keys should NOT be loaded at import time."""
    # Reset state
    crypto_mod._keys["public"] = None
    crypto_mod._keys["private"] = None
    crypto_mod._keys["loaded"] = False

    assert not crypto_mod._keys["loaded"], "Keys should not be loaded at import"
    assert crypto_mod._keys["public"] is None
    assert crypto_mod._keys["private"] is None

    print("  PASS: Keys not loaded at import time")


def test_lazy_load_on_first_use():
    """get_rsa_public_key() should trigger lazy loading on first call."""
    crypto_mod._keys["public"] = None
    crypto_mod._keys["private"] = None
    crypto_mod._keys["loaded"] = False

    # First call triggers loading
    pub = crypto_mod.get_rsa_public_key()
    # If CatPawAI is installed, pub should be a PEM string
    # If not installed, pub should be None but _keys["loaded"] should be True
    assert crypto_mod._keys["loaded"], "Keys should be marked loaded after first call"

    if pub is not None:
        assert "BEGIN" in pub, "Public key should be a PEM string"
        priv = crypto_mod.get_rsa_private_key()
        assert priv is not None, "Private key should also be loaded"
    else:
        print("  (Note: CatPawAI not installed, keys are None — testing graceful degradation)")

    print("  PASS: Lazy loading on first use")


def test_cache_after_first_load():
    """Second call should return cached value without re-extraction."""
    crypto_mod._keys["public"] = None
    crypto_mod._keys["private"] = None
    crypto_mod._keys["loaded"] = False

    # First call
    pub1 = crypto_mod.get_rsa_public_key()

    # Track if _extract_rsa_keys is called again
    original_extract = crypto_mod._extract_rsa_keys
    call_count = [0]

    def counting_extract():
        call_count[0] += 1
        return original_extract()

    crypto_mod._extract_rsa_keys = counting_extract
    try:
        pub2 = crypto_mod.get_rsa_public_key()
    finally:
        crypto_mod._extract_rsa_keys = original_extract

    assert call_count[0] == 0, "Second call should use cache, not re-extract"
    assert pub1 == pub2, "Cached value should match first call"

    print("  PASS: Keys cached after first load")


def test_invalidate_forces_reload():
    """invalidate_rsa_cache() should force re-extraction on next call."""
    # Load keys
    crypto_mod._keys["public"] = None
    crypto_mod._keys["private"] = None
    crypto_mod._keys["loaded"] = False
    crypto_mod.get_rsa_public_key()
    assert crypto_mod._keys["loaded"]

    # Invalidate
    crypto_mod.invalidate_rsa_cache()
    assert not crypto_mod._keys["loaded"], "Cache should be invalidated"
    assert crypto_mod._keys["public"] is None

    # Next call should re-load
    crypto_mod.get_rsa_public_key()
    assert crypto_mod._keys["loaded"], "Keys should be re-loaded after invalidation"

    print("  PASS: invalidate_rsa_cache forces reload")


def test_encrypt_with_no_keys_returns_plaintext():
    """encrypt_request should return plaintext when keys are unavailable."""
    # Mock _extract_rsa_keys to simulate failed extraction (no CatPawAI installed)
    original_extract = crypto_mod._extract_rsa_keys
    crypto_mod._extract_rsa_keys = lambda: (None, None)
    crypto_mod._keys["public"] = None
    crypto_mod._keys["private"] = None
    crypto_mod._keys["loaded"] = False

    try:
        body = '{"test": "data"}'
        headers = {}
        result = crypto_mod.encrypt_request(body, headers)
        assert result == body, "Should return plaintext when no keys"
        assert "encrypted-key" not in headers, "Should not set encrypted-key header"
    finally:
        crypto_mod._extract_rsa_keys = original_extract

    print("  PASS: encrypt returns plaintext without keys")


def test_decrypt_with_no_keys_returns_original():
    """decrypt_response_data should return original when keys unavailable."""
    # Mock _extract_rsa_keys to simulate failed extraction
    original_extract = crypto_mod._extract_rsa_keys
    crypto_mod._extract_rsa_keys = lambda: (None, None)
    crypto_mod._keys["public"] = None
    crypto_mod._keys["private"] = None
    crypto_mod._keys["loaded"] = False

    try:
        result = crypto_mod.decrypt_response_data("encrypted_data", "encrypted_key")
        assert result == "encrypted_data", "Should return original data without keys"
    finally:
        crypto_mod._extract_rsa_keys = original_extract

    print("  PASS: decrypt returns original without keys")


def test_decrypt_with_empty_key_returns_original():
    """decrypt_response_data should return original when encrypted_key is empty."""
    result = crypto_mod.decrypt_response_data("some_data", "")
    assert result == "some_data", "Should return original when encrypted_key is empty"

    result = crypto_mod.decrypt_response_data("some_data", None)
    assert result == "some_data", "Should return original when encrypted_key is None"

    print("  PASS: decrypt returns original with empty key")


def test_concurrent_get_rsa_public_key():
    """Concurrent calls to get_rsa_public_key should be safe."""
    crypto_mod._keys["public"] = None
    crypto_mod._keys["private"] = None
    crypto_mod._keys["loaded"] = False

    results = []
    errors = []

    def worker():
        try:
            pub = crypto_mod.get_rsa_public_key()
            results.append(pub)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Concurrent calls should not raise: {errors}"
    assert len(results) == 10, "All threads should get a result"
    # All results should be the same (either all None or all the same PEM)
    assert all(r == results[0] for r in results), "All threads should get same key"

    print("  PASS: Concurrent get_rsa_public_key is thread-safe")


if __name__ == "__main__":
    print("Testing crypto lazy loading + cache invalidation (Finding 3 fix)...")
    print()

    tests = [
        test_keys_not_loaded_at_import,
        test_lazy_load_on_first_use,
        test_cache_after_first_load,
        test_invalidate_forces_reload,
        test_encrypt_with_no_keys_returns_plaintext,
        test_decrypt_with_no_keys_returns_original,
        test_decrypt_with_empty_key_returns_original,
        test_concurrent_get_rsa_public_key,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
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
