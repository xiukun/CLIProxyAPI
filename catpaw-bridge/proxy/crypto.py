"""RSA key extraction + AES-128-ECB / RSA-OAEP-SHA1 encryption.

The CatPawAI API requires request bodies to be encrypted:
  1. Generate random 16-byte AES key
  2. Encrypt body with AES-128-ECB -> base64
  3. AES key -> base64 -> RSA-OAEP-SHA1 encrypt -> base64
  4. Set 'encrypted-key' header with the encrypted AES key

RSA keys are extracted from CatPawAI's extension.js (XOR-encrypted).

Keys are lazy-loaded on first use and cached. If CatPawAI updates its
extension.js (e.g. rotating RSA keys), call invalidate_rsa_cache()
to force re-extraction on the next encrypt/decrypt call.
"""

import base64
import os
import re
import secrets
import sys
import threading

from Crypto.Cipher import AES
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA1
from Crypto.Util.Padding import pad, unpad

from proxy.config import VERBOSE

# Lazy-loaded key cache (thread-safe)
_key_lock = threading.Lock()
_keys = {"public": None, "private": None, "loaded": False}


def _extract_rsa_keys():
    """Extract RSA public and private keys from CatPawAI extension.js.

    The keys are XOR-encrypted with key "ThisIsMyXorKey" and then base64-encoded.
    """
    xor_key = "ThisIsMyXorKey"

    def xor_decipher(encoded_str):
        decoded = base64.b64decode(encoded_str)
        result = bytearray()
        for i, b in enumerate(decoded):
            result.append(b ^ ord(xor_key[i % len(xor_key)]))
        return result.decode("utf-8")

    try:
        ext_path = os.environ.get(
            "CATPAW_EXTENSION_JS",
            "/Applications/CatPawAI.app/Contents/Resources/app/extensions/mt-idekit.mt-idekit-code/out/extension.js",
        )
        with open(ext_path, "r") as f:
            data = f.read()

        # Extract key1 (public key)
        m1 = re.search(r'this\.key1=this\.xorDecipher\("([^"]+)"', data)
        m2 = re.search(r'this\.key2=this\.xorDecipher\("([^"]+)"', data)

        if not m1 or not m2:
            raise RuntimeError("Could not find RSA keys in extension.js")

        key1_pem = xor_decipher(m1.group(1))
        key2_pem = xor_decipher(m2.group(1))

        # Clean up PEM (remove extra whitespace, fix line breaks)
        def clean_pem(pem):
            # The XOR decryption may produce the PEM with embedded newlines
            # that are actually part of the base64 content. We need to:
            # 1. Find the header and footer
            # 2. Extract the base64 content between them
            # 3. Clean and reformat

            # Try to find header/footer patterns
            # Match header like "-----BEGIN PUBLIC KEY-----" or "-----BEGINPRIVATEKEY-----"
            pub_match = re.search(r'-----BEGIN[\s]*PUBLIC[\s]*KEY-----', pem)
            priv_match = re.search(r'-----BEGIN[\s]*PRIVATE[\s]*KEY-----', pem)

            if pub_match:
                header = "-----BEGIN PUBLIC KEY-----"
                footer = "-----END PUBLIC KEY-----"
                start = pub_match.end()
                end_match = re.search(r'-----END[\s]*PUBLIC[\s]*KEY-----', pem[start:])
                if end_match:
                    end = start + end_match.start()
                else:
                    end = len(pem)
            elif priv_match:
                header = "-----BEGIN PRIVATE KEY-----"
                footer = "-----END PRIVATE KEY-----"
                start = priv_match.end()
                end_match = re.search(r'-----END[\s]*PRIVATE[\s]*KEY-----', pem[start:])
                if end_match:
                    end = start + end_match.start()
                else:
                    end = len(pem)
            else:
                # No PEM headers found, try to fix manually
                pem = pem.replace("\n", "").replace("\r", "").replace(" ", "")
                if "BEGINPUBLICKEY" in pem:
                    pem = pem.replace("BEGINPUBLICKEY", "BEGIN PUBLIC KEY")
                    pem = pem.replace("ENDPUBLICKEY", "END PUBLIC KEY")
                elif "BEGINPRIVATEKEY" in pem:
                    pem = pem.replace("BEGINPRIVATEKEY", "BEGIN PRIVATE KEY")
                    pem = pem.replace("ENDPRIVATEKEY", "END PRIVATE KEY")
                # Split into 64-char lines
                lines = []
                for i in range(0, len(pem), 64):
                    lines.append(pem[i:i + 64])
                return "\n".join(lines)

            # Extract base64 content and clean it
            b64_content = pem[start:end]
            b64_content = b64_content.replace("\n", "").replace("\r", "").replace(" ", "")

            # Build proper PEM
            lines = [header]
            for i in range(0, len(b64_content), 64):
                lines.append(b64_content[i:i + 64])
            lines.append(footer)
            return "\n".join(lines)

        key1_pem = clean_pem(key1_pem)
        key2_pem = clean_pem(key2_pem)

        # Verify keys can be imported
        try:
            RSA.importKey(key1_pem)
        except Exception as e:
            print(f"[CatPawProxy] WARNING: key1 import failed: {e}", flush=True, file=sys.stderr)
        try:
            RSA.importKey(key2_pem)
        except Exception as e:
            print(f"[CatPawProxy] WARNING: key2 import failed: {e}", flush=True, file=sys.stderr)

        if VERBOSE:
            print(f"[CatPawProxy] RSA keys extracted successfully", flush=True)
            print(f"[CatPawProxy] key1 (public) length: {len(key1_pem)}", flush=True)
            print(f"[CatPawProxy] key2 (private) length: {len(key2_pem)}", flush=True)

        return key1_pem, key2_pem
    except Exception as e:
        print(f"[CatPawProxy] WARNING: Could not extract RSA keys: {e}", flush=True, file=sys.stderr)
        print("[CatPawProxy] Encryption will be disabled. API calls may fail.", flush=True, file=sys.stderr)
        return None, None


# Extract keys at import time (same behavior as original monolith)
# RSA_PUBLIC_KEY_PEM, RSA_PRIVATE_KEY_PEM = _extract_rsa_keys()


def _ensure_keys_loaded():
    """Load RSA keys on first use (lazy loading).

    Thread-safe via _key_lock. If keys are already loaded, returns immediately.
    If keys failed to load on a previous attempt, retries on each call
    (CatPawAI might have been installed/updated since last attempt).
    """
    if _keys["loaded"] and _keys["public"]:
        return
    with _key_lock:
        # Double-check after acquiring lock
        if _keys["loaded"] and _keys["public"]:
            return
        pub, priv = _extract_rsa_keys()
        _keys["public"] = pub
        _keys["private"] = priv
        _keys["loaded"] = True


def invalidate_rsa_cache():
    """Invalidate the RSA key cache, forcing re-extraction on next use.

    Call this when the upstream returns 401 or decryption failures,
    which may indicate CatPawAI updated its extension.js with new RSA keys.
    """
    with _key_lock:
        _keys["public"] = None
        _keys["private"] = None
        _keys["loaded"] = False
    if VERBOSE:
        print("[CatPawProxy] RSA key cache invalidated", flush=True)


def get_rsa_public_key():
    """Get the RSA public key PEM string, loading if necessary."""
    _ensure_keys_loaded()
    return _keys["public"]


def get_rsa_private_key():
    """Get the RSA private key PEM string, loading if necessary."""
    _ensure_keys_loaded()
    return _keys["private"]


# Backward-compatible module-level access (evaluated lazily via property-like functions)
# Other modules should use get_rsa_public_key() / get_rsa_private_key() instead.
RSA_PUBLIC_KEY_PEM = None  # Deprecated: use get_rsa_public_key()
RSA_PRIVATE_KEY_PEM = None  # Deprecated: use get_rsa_private_key()


def encrypt_request(body_str: str, headers: dict) -> str:
    """Encrypt request body using AES-128-ECB + RSA-OAEP-SHA1.

    1. Generate random 16-byte AES key
    2. Encrypt body with AES-128-ECB -> base64
    3. AES key -> base64 -> RSA-OAEP-SHA1 encrypt -> base64
    4. Set 'encrypted-key' header
    5. Return encrypted body (base64 string)
    """
    pub_key = get_rsa_public_key()
    if not pub_key:
        return body_str

    try:
        # Generate random AES-128 key (16 bytes)
        aes_key = secrets.token_bytes(16)

        # Encrypt body with AES-128-ECB
        cipher = AES.new(aes_key, AES.MODE_ECB)
        body_bytes = body_str.encode("utf-8")
        encrypted_body = cipher.encrypt(pad(body_bytes, AES.block_size))
        encrypted_body_b64 = base64.b64encode(encrypted_body).decode("utf-8")

        # Encrypt AES key with RSA-OAEP-SHA1
        rsa_key = RSA.importKey(pub_key)
        cipher_rsa = PKCS1_OAEP.new(rsa_key, hashAlgo=SHA1)
        # The AES key is first converted to base64, then encrypted
        aes_key_b64 = base64.b64encode(aes_key).decode("utf-8")
        encrypted_aes_key = cipher_rsa.encrypt(aes_key_b64.encode("utf-8"))
        encrypted_aes_key_b64 = base64.b64encode(encrypted_aes_key).decode("utf-8")

        # Set header
        headers["encrypted-key"] = encrypted_aes_key_b64

        return encrypted_body_b64
    except Exception as e:
        print(f"[CatPawProxy] Encryption failed, sending plaintext: {e}", flush=True, file=sys.stderr)
        return body_str


def decrypt_response_data(encrypted_data: str, encrypted_key: str) -> str:
    """Decrypt response data using AES-128-ECB + RSA-OAEP-SHA1.

    1. RSA-OAEP-SHA1 decrypt the encrypted_key -> base64 string -> AES key
    2. AES-128-ECB decrypt the data
    """
    priv_key = get_rsa_private_key()
    if not priv_key or not encrypted_key:
        return encrypted_data

    try:
        # Decrypt AES key with RSA-OAEP-SHA1
        rsa_key = RSA.importKey(priv_key)
        cipher_rsa = PKCS1_OAEP.new(rsa_key, hashAlgo=SHA1)
        encrypted_key_bytes = base64.b64decode(encrypted_key)
        decrypted_aes_key_b64 = cipher_rsa.decrypt(encrypted_key_bytes)
        # The decrypted value is base64-encoded AES key
        aes_key = base64.b64decode(decrypted_aes_key_b64)

        # Decrypt data with AES-128-ECB
        cipher = AES.new(aes_key, AES.MODE_ECB)
        encrypted_data_bytes = base64.b64decode(encrypted_data)
        decrypted_data = unpad(cipher.decrypt(encrypted_data_bytes), AES.block_size)

        return decrypted_data.decode("utf-8")
    except Exception as e:
        print(f"[CatPawProxy] Decryption failed: {e}", flush=True, file=sys.stderr)
        return encrypted_data
