"""SSO Auth: read accessToken from CatPawAI's state.vscdb (SQLite).

The token is cached for _AUTH_CACHE_TTL seconds. When a 401 is received
upstream, invalidate_auth_cache() forces a re-read on the next call.

Concurrency: _auth_cache is protected by _auth_lock (threading.Lock).
We use threading.Lock instead of asyncio.Lock because get_catpaw_auth()
does blocking I/O (SQLite read) and is called from both sync (app startup)
and async (request handlers) contexts. The critical section is very short
(cache check + SQLite read only on cache miss, which happens once per minute).
"""

import json
import os
import sqlite3
import threading
import time

from proxy.config import (
    CATPAW_DATA_DIR,
    SSO_CLIENT_ID,
    SSO_CLIENT_ID_2,
    TENANT_ID,
    NEED_PASSPORT_ID,
    VERBOSE,
)

# Auth cache
_auth_cache = {"access_token": None, "mis_id": None, "user_info_id": None, "ts": 0}
_auth_lock = threading.Lock()
_AUTH_CACHE_TTL = 60  # 1 minute (was 5 min - too long, token may expire)


def invalidate_auth_cache():
    """Invalidate the auth cache, forcing next get_catpaw_auth() to re-read from state.vscdb."""
    with _auth_lock:
        _auth_cache["access_token"] = None
        _auth_cache["ts"] = 0
    if VERBOSE:
        print("[CatPawProxy] Auth cache invalidated", flush=True)


def get_catpaw_auth():
    """Read SSO accessToken and user info from CatPawAI's state.vscdb.

    Thread-safe via _auth_lock — prevents concurrent requests from both
    triggering SQLite reads when the cache expires simultaneously.
    """
    with _auth_lock:
        now = time.time()
        if _auth_cache["access_token"] and (now - _auth_cache["ts"]) < _AUTH_CACHE_TTL:
            return dict(_auth_cache)  # return a copy to prevent external mutation

        vscdb_path = os.path.join(CATPAW_DATA_DIR, "User", "globalStorage", "state.vscdb")
        if not os.path.exists(vscdb_path):
            raise RuntimeError(f"CatPawAI state.vscdb not found: {vscdb_path}")

        conn = sqlite3.connect(vscdb_path)
        try:
            cursor = conn.execute(
                "SELECT value FROM ItemTable WHERE key = 'catpaw.mt-authentication'"
            )
            row = cursor.fetchone()
            if not row:
                raise RuntimeError("catpaw.mt-authentication not found in state.vscdb")

            auth_data = json.loads(row[0])
            mt_auth = json.loads(auth_data["mt.auth"])

            sessions = mt_auth.get("sessions", [])
            if not sessions:
                raise RuntimeError("No SSO sessions found")

            session = sessions[0]
            access_token = session["accessToken"]
            account = session.get("account", {})

            mis_id = account.get("label") or account.get("id", "")
            user_info_id = account.get("userInfoId", "")

            _auth_cache["access_token"] = access_token
            _auth_cache["mis_id"] = mis_id
            _auth_cache["user_info_id"] = user_info_id
            _auth_cache["ts"] = now

            if VERBOSE:
                print(f"[CatPawProxy] Auth loaded: mis_id={mis_id}, user_info_id={user_info_id}", flush=True)

            return dict(_auth_cache)  # return a copy to prevent external mutation
        finally:
            conn.close()


def build_catpaw_headers(auth, content_type="application/json"):
    """Build the headers CatPawAI API expects.

    Cookie format (from extension.js function Gf):
        Cookie: <client_id1>_passportid=<token>; <client_id2>_ssoid=<token>
        (when needPassportId=true for tenant 5282fa6645)

    Also sets Catpaw-Auth header to the raw token.
    """
    access_token = auth["access_token"]
    mis_id = auth["mis_id"]

    # Build Cookie: two entries with the same token
    if NEED_PASSPORT_ID:
        cookie1 = f"{SSO_CLIENT_ID}_passportid={access_token}"
    else:
        cookie1 = f"{SSO_CLIENT_ID}_ssoid={access_token}"
    cookie2 = f"{SSO_CLIENT_ID_2}_ssoid={access_token}"
    cookie_str = f"{cookie1}; {cookie2}"

    headers = {
        "Content-Type": content_type,
        "ide-type": "CatPaw IDE",
        "client-type": "CatPaw IDE",
        "ide-version": "2026.2.2",
        "plugin-id": "mt-idekit.mt-idekit-code",
        "plugin-version": "2026.2.3",
        "client-env": "production",
        "user-mis-id": mis_id,
        "user-uid": mis_id,
        "mis-id": mis_id,
        "tenant": TENANT_ID,
        "platform-info": "darwin-arm64",
        "Cookie": cookie_str,
        "Catpaw-Auth": access_token,
    }

    return headers
