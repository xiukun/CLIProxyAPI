"""HTTP handlers: chat/completions, models, health.

handle_chat_completions is the core endpoint that:
  1. Reads the OpenAI request body
  2. Converts to CatPawAI format
  3. Encrypts + sends upstream with 401 auto-retry
  4. Streams or buffers the response
  5. Parses tool calls when tools were provided
"""

import asyncio
import json
import re
import sys
import time
import uuid

import aiohttp
from aiohttp import web

# ---------------------------------------------------------------------------
# Global connection pool (reused across requests for performance)
# ---------------------------------------------------------------------------
# Creating a new ClientSession per request is expensive (DNS lookup, TLS
# handshake, connection pool init). We create ONE session at startup and
# reuse it for all upstream requests. The session is lazily initialized
# on first use (aiohttp requires an event loop to exist).
_global_session: aiohttp.ClientSession | None = None

async def get_session() -> aiohttp.ClientSession:
    """Get or create the global aiohttp ClientSession."""
    global _global_session
    if _global_session is None or _global_session.closed:
        timeout = aiohttp.ClientTimeout(total=600, connect=15, sock_read=120)
        connector = aiohttp.TCPConnector(
            limit=20,           # max concurrent connections
            limit_per_host=10,  # max per host
            ttl_dns_cache=300,  # DNS cache TTL (5 min)
            enable_cleanup_closed=True,
        )
        _global_session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
        )
    return _global_session


async def close_session():
    """Close the global session on shutdown."""
    global _global_session
    if _global_session and not _global_session.closed:
        await _global_session.close()
        _global_session = None

from proxy.config import (
    CATPAW_API_BASE,
    MODEL_NAME,
    VERBOSE,
    MAX_ENCRYPTED_BODY,
    STRIP_TOOL_DEFINITIONS,
)
from proxy.crypto import (
    encrypt_request,
    decrypt_response_data,
    get_rsa_public_key,
    invalidate_rsa_cache,
)
from proxy.auth import (
    get_catpaw_auth,
    invalidate_auth_cache,
    build_catpaw_headers,
)
from proxy.translator import (
    openai_to_catpaw_request,
    catpaw_sse_to_openai_sse,
    _extract_content_from_catpaw,
)
from proxy.toolcall import _parse_tool_calls
from proxy.sse import (
    _create_sse_stream_response,
    _send_keepalive,
)


async def _iter_catpaw_sse(upstream_resp, resp_encrypted_key: str, on_idle=None):
    """Iterate CatPawAI SSE stream, yielding parsed JSON dicts.

    Shared line-reading + decryption + JSON parsing for both stream and
    buffer modes. Eliminates the duplicated SSE loop that previously existed
    in handle_chat_completions.

    Args:
        upstream_resp: aiohttp ClientResponse (already opened)
        resp_encrypted_key: encrypted-key header value for decryption ("" if none)
        on_idle: optional async callback invoked when a non-data line is
                 received (empty line, non-data: prefix). Used by buffer
                 mode to send keepalives.

    Yields: parsed catpaw_data dicts (dict)
    Returns when: ': ping', '[DONE]', or 'lastOne' is encountered.
    """
    async for line in upstream_resp.content:
        line_str = line.decode("utf-8", errors="replace").strip()

        if not line_str:
            if on_idle:
                await on_idle()
            continue

        # ': ping' is CatPawAI's end-of-stream signal
        if line_str.startswith(": ping"):
            return

        if not line_str.startswith("data:"):
            if on_idle:
                await on_idle()
            continue

        data_content = line_str[5:].strip()
        if data_content == "[DONE]":
            return

        # Decrypt if needed
        if resp_encrypted_key:
            try:
                decrypted = decrypt_response_data(data_content, resp_encrypted_key)
                catpaw_data = json.loads(decrypted)
            except Exception as e:
                if VERBOSE:
                    print(f"[CatPawProxy] Decrypt error: {e}", flush=True, file=sys.stderr)
                continue
        else:
            try:
                catpaw_data = json.loads(data_content)
            except json.JSONDecodeError:
                continue

        # Check for lastOne (end signal)
        if catpaw_data.get("lastOne", False):
            return

        yield catpaw_data


async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    """Handle /v1/chat/completions - main chat endpoint."""
    try:
        auth = get_catpaw_auth()
    except Exception as e:
        return web.json_response(
            {"error": {"message": f"Auth failed: {e}", "type": "auth_error"}},
            status=503,
        )

    # Read OpenAI request body
    body = await request.read()
    try:
        openai_body = json.loads(body)
    except json.JSONDecodeError:
        return web.json_response(
            {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
            status=400,
        )

    model = openai_body.get("model", MODEL_NAME)
    is_stream = openai_body.get("stream", False)
    has_tools = bool(openai_body.get("tools"))

    # Convert to CatPawAI format
    catpaw_request = await openai_to_catpaw_request(openai_body)
    catpaw_body_str = json.dumps(catpaw_request, ensure_ascii=False)

    if VERBOSE:
        print(f"[CatPawProxy] Model: {model}, Stream: {is_stream}, Tools: {has_tools}", flush=True)
        print(f"[CatPawProxy] Unencrypted body: {len(catpaw_body_str)} bytes ({len(catpaw_body_str)/1024:.1f} KB)", flush=True)
        if has_tools and STRIP_TOOL_DEFINITIONS:
            print(f"[CatPawProxy] strip_tool_definitions=true: tool definitions NOT injected (parsing still active)", flush=True)
        print(f"[CatPawAI] CatPawAI request: {catpaw_body_str[:200]}...", flush=True)
        if has_tools:
            print(f"[CatPawProxy] Tool-aware mode: response will be buffered for tool call parsing", flush=True)

    # Build headers with SSO auth
    headers = build_catpaw_headers(auth)

    # Encrypt request body
    encrypted_body = encrypt_request(catpaw_body_str, headers)

    # For encrypted requests, the body is a base64 string, not JSON
    headers["Accept"] = "text/event-stream"
    headers["Cache-Control"] = "no-cache"
    headers["Connection"] = "keep-alive"

    if VERBOSE:
        # Log all headers being sent (redact token)
        safe_headers = {k: (v[:20] + "..." if k.lower() in ("cookie", "catpaw-auth", "encrypted-key") and len(v) > 20 else v) for k, v in headers.items()}
        print(f"[CatPawProxy] Headers: {json.dumps(safe_headers, ensure_ascii=False)}", flush=True)
        print(f"[CatPawProxy] Encrypted body preview: {encrypted_body[:80]}...", flush=True)

    # Strip headers that should not be forwarded
    for key in ["Authorization", "Host", "Content-Length"]:
        headers.pop(key, None)

    # Target URL
    target_url = f"{CATPAW_API_BASE}/api/gpt/openai/stream"

    if VERBOSE:
        print(f"[CatPawProxy] POST -> {target_url}", flush=True)
        print(f"[CatPawProxy] Encrypted: {len(encrypted_body)} bytes", flush=True)

    # Warn on large request bodies — CatPawAI upstream may reject them
    if len(encrypted_body) > MAX_ENCRYPTED_BODY:
        print(f"[CatPawProxy] WARNING: Large request body ({len(encrypted_body)} bytes > {MAX_ENCRYPTED_BODY} limit), upstream may close connection", flush=True, file=sys.stderr)
    elif VERBOSE:
        print(f"[CatPawProxy] Encrypted body size: {len(encrypted_body)} bytes ({len(encrypted_body)/1024:.1f} KB)", flush=True)

    # ------------------------------------------------------------------
    # Upstream request with 401 auto-retry
    # ------------------------------------------------------------------
    # CRITICAL: All response reading must happen INSIDE the async with block.
    # If we exit the block, aiohttp closes the connection and we get
    # "ClientConnectionError: Connection closed" when trying to read content.
    #
    # PERF: We reuse a global ClientSession instead of creating one per
    # request. This avoids DNS lookup + TLS handshake overhead (~200ms saved
    # per request) and enables HTTP keep-alive connection reuse.
    prepared_stream = None

    try:
        session = await get_session()
        retry_attempted = False

        while True:
            async with session.post(
                url=target_url,
                headers=headers,
                data=encrypted_body,
                ssl=False,
            ) as upstream_resp:

                    # ---- 401 auto-retry logic ----
                    if upstream_resp.status == 401 and not retry_attempted:
                        error_body = await upstream_resp.read()
                        error_msg = error_body.decode("utf-8", errors="replace")[:200]
                        print(f"[CatPawProxy] Got 401 from upstream: {error_msg}", flush=True, file=sys.stderr)
                        print(f"[CatPawProxy] Invalidating auth cache, retrying with fresh token...", flush=True, file=sys.stderr)
                        invalidate_auth_cache()
                        invalidate_rsa_cache()  # CatPawAI may have rotated RSA keys

                        try:
                            auth = get_catpaw_auth()
                        except Exception as e:
                            print(f"[CatPawProxy] Retry failed - cannot re-read auth: {e}", flush=True, file=sys.stderr)
                            return web.json_response(
                                {"error": {"message": f"Auth expired and refresh failed: {e}", "type": "auth_error"}},
                                status=502,
                            )

                        # Rebuild headers and re-encrypt with new token
                        headers = build_catpaw_headers(auth)
                        encrypted_body = encrypt_request(catpaw_body_str, headers)
                        headers["Accept"] = "text/event-stream"
                        headers["Cache-Control"] = "no-cache"
                        headers["Connection"] = "keep-alive"
                        for key in ["Authorization", "Host", "Content-Length"]:
                            headers.pop(key, None)

                        retry_attempted = True
                        if VERBOSE:
                            print(f"[CatPawProxy] Retrying with refreshed token", flush=True)
                        continue  # retry the request

                    if upstream_resp.status == 401 and retry_attempted:
                        error_body = await upstream_resp.read()
                        error_msg = error_body.decode("utf-8", errors="replace")[:200]
                        print(f"[CatPawProxy] 401 after retry, giving up: {error_msg}", flush=True, file=sys.stderr)
                        return web.json_response(
                            {"error": {"message": "Auth expired, token refresh did not help. Please re-login in CatPawAI IDE.", "type": "auth_error", "code": 401}},
                            status=502,
                        )

                    if upstream_resp.status != 200:
                        error_body = await upstream_resp.read()
                        error_msg = error_body.decode("utf-8", errors="replace")[:500]
                        resp_headers = dict(upstream_resp.headers)
                        print(f"[CatPawProxy] Upstream error {upstream_resp.status}: {error_msg}", flush=True, file=sys.stderr)
                        print(f"[CatPawProxy] Response headers: {json.dumps(resp_headers, ensure_ascii=False)}", flush=True, file=sys.stderr)
                        return web.json_response(
                            {"error": {"message": f"Upstream error: {error_msg}", "type": "upstream_error", "code": upstream_resp.status}},
                            status=502 if upstream_resp.status == 401 else upstream_resp.status,
                        )

                    # ---- Response processing (MUST be inside async with!) ----
                    resp_encrypted_key = upstream_resp.headers.get("encrypted-key", "")

                    if is_stream and not has_tools:
                        # ===== Stream mode (no tools): pass through as SSE =====
                        stream_response = _create_sse_stream_response()
                        await stream_response.prepare(request)
                        prepared_stream = stream_response

                        chunk_count = 0
                        prev_content = ""  # Track accumulated content for delta computation
                        async for catpaw_data in _iter_catpaw_sse(upstream_resp, resp_encrypted_key):
                            # Convert to OpenAI format and send (with delta computation)
                            openai_chunk = catpaw_sse_to_openai_sse(catpaw_data, model, is_last=False, prev_content=prev_content)
                            # Update prev_content for next delta
                            new_content = _extract_content_from_catpaw(catpaw_data)
                            if new_content:
                                prev_content = new_content
                            await stream_response.write(f"data: {json.dumps(openai_chunk, ensure_ascii=False)}\n\n".encode())
                            chunk_count += 1

                        # Stream ended (: ping, [DONE], or lastOne) — send final chunk + [DONE]
                        if VERBOSE:
                            print(f"[CatPawProxy] Stream ended, chunks sent: {chunk_count}", flush=True)
                        final_chunk = {
                            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        }
                        await stream_response.write(f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode())
                        await stream_response.write(b"data: [DONE]\n\n")
                        await stream_response.write_eof()
                        return stream_response

                    else:
                        # ===== Buffer mode (has_tools or non-stream): collect all SSE =====
                        keepalive_stream = None
                        if is_stream and has_tools:
                            keepalive_stream = _create_sse_stream_response()
                            await keepalive_stream.prepare(request)
                            prepared_stream = keepalive_stream

                        # CatPawAI's content field contains FULL accumulated text.
                        # We only need the last value, not all values appended.
                        last_content = ""
                        last_keepalive = [time.time()]

                        async def _on_idle():
                            await _send_keepalive(keepalive_stream, last_keepalive)

                        async for catpaw_data in _iter_catpaw_sse(upstream_resp, resp_encrypted_key, on_idle=_on_idle):
                            # Extract content (CatPawAI sends accumulated text, keep last)
                            content = _extract_content_from_catpaw(catpaw_data)
                            if content:
                                last_content = content
                            await _send_keepalive(keepalive_stream, last_keepalive)

                        full_content = last_content

                        # Strip null bytes and other control characters that may
                        # leak from upstream (CatPawAI sometimes sends null bytes
                        # in the SSE stream, especially after large requests).
                        # These corrupt JSON parsing and tool call detection.
                        if full_content:
                            full_content = full_content.replace("\x00", "")
                            # Also strip other non-printable control chars except
                            # newline, tab, and carriage return
                            full_content = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', full_content)

                        # Log response content for debugging when tools are present
                        if has_tools and VERBOSE:
                            content_len = len(full_content)
                            preview_start = full_content[:300].replace("\n", "\\n")
                            preview_end = full_content[-300:].replace("\n", "\\n") if content_len > 300 else ""
                            print(f"[CatPawProxy] Response content ({content_len} chars):", flush=True)
                            print(f"[CatPawProxy]   START: {preview_start}", flush=True)
                            if preview_end:
                                print(f"[CatPawProxy]   END:   {preview_end}", flush=True)

                        # Parse for tool calls when tools were provided
                        if has_tools:
                            clean_text, tool_calls = _parse_tool_calls(full_content)

                            if tool_calls:
                                if VERBOSE:
                                    print(f"[CatPawProxy] Parsed {len(tool_calls)} tool call(s): {[tc['function']['name'] for tc in tool_calls]}", flush=True)

                                if is_stream:
                                    sr = keepalive_stream if keepalive_stream else _create_sse_stream_response()
                                    if not keepalive_stream:
                                        await sr.prepare(request)

                                    resp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                                    resp_created = int(time.time())

                                    # Emit text content first (if any)
                                    if clean_text:
                                        text_chunk = {
                                            "id": resp_id,
                                            "object": "chat.completion.chunk",
                                            "created": resp_created,
                                            "model": model,
                                            "choices": [{"index": 0, "delta": {"content": clean_text}, "finish_reason": None}],
                                        }
                                        await sr.write(f"data: {json.dumps(text_chunk, ensure_ascii=False)}\n\n".encode())

                                    # Emit each tool call as a separate chunk
                                    for i, tc in enumerate(tool_calls):
                                        tc_chunk = {
                                            "id": resp_id,
                                            "object": "chat.completion.chunk",
                                            "created": resp_created,
                                            "model": model,
                                            "choices": [{
                                                "index": 0,
                                                "delta": {
                                                    "tool_calls": [{
                                                        "index": i,
                                                        "id": tc["id"],
                                                        "type": "function",
                                                        "function": {
                                                            "name": tc["function"]["name"],
                                                            "arguments": tc["function"]["arguments"],
                                                        },
                                                    }]
                                                },
                                                "finish_reason": None,
                                            }],
                                        }
                                        await sr.write(f"data: {json.dumps(tc_chunk, ensure_ascii=False)}\n\n".encode())

                                    # Emit final chunk with finish_reason
                                    final_chunk = {
                                        "id": resp_id,
                                        "object": "chat.completion.chunk",
                                        "created": resp_created,
                                        "model": model,
                                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                                    }
                                    await sr.write(f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode())
                                    await sr.write(b"data: [DONE]\n\n")
                                    await sr.write_eof()

                                    if VERBOSE:
                                        print(f"[CatPawProxy] Streamed {len(tool_calls)} tool_calls as SSE chunks", flush=True)
                                    return sr
                                else:
                                    # Non-stream: return JSON response with tool_calls
                                    message = {"role": "assistant", "content": clean_text if clean_text else None}
                                    message["tool_calls"] = tool_calls
                                    openai_response = {
                                        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                                        "object": "chat.completion",
                                        "created": int(time.time()),
                                        "model": model,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "message": message,
                                                "finish_reason": "tool_calls",
                                            }
                                        ],
                                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                    }
                                    return web.json_response(openai_response)
                            else:
                                if VERBOSE:
                                    preview = full_content[:200].replace("\n", "\\n")
                                    print(f"[CatPawProxy] No tool calls found in response ({len(full_content)} chars), returning as text", flush=True)
                                    print(f"[CatPawProxy]   Preview: {preview}", flush=True)

                        # No tool calls or no tools: return as normal text response
                        # Use clean_text (agent XML already stripped by _parse_tool_calls)
                        display_text = clean_text if has_tools else full_content
                        if is_stream and has_tools:
                            sr = keepalive_stream
                            resp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                            resp_created = int(time.time())

                            if display_text:
                                text_chunk = {
                                    "id": resp_id,
                                    "object": "chat.completion.chunk",
                                    "created": resp_created,
                                    "model": model,
                                    "choices": [{"index": 0, "delta": {"content": display_text}, "finish_reason": None}],
                                }
                                await sr.write(f"data: {json.dumps(text_chunk, ensure_ascii=False)}\n\n".encode())

                            final_chunk = {
                                "id": resp_id,
                                "object": "chat.completion.chunk",
                                "created": resp_created,
                                "model": model,
                                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            }
                            await sr.write(f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode())
                            await sr.write(b"data: [DONE]\n\n")
                            await sr.write_eof()
                            return sr

                        # Non-stream response
                        openai_response = {
                            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": display_text},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                        }
                        return web.json_response(openai_response)

            # End of async with session.post() — if we reach here without
            # returning, break out of the while loop
            break

    except aiohttp.ClientError as e:
        # ClientConnectionResetError happens when Claude Code closes the
        # connection before we finish sending. This is normal (e.g. when
        # the user presses Ctrl+C or the response is too long). Don't crash.
        if "ConnectionReset" in type(e).__name__ or "Cannot write to closing transport" in str(e):
            print(f"[CatPawProxy] Client closed connection early (normal): {e}", flush=True, file=sys.stderr)
            if prepared_stream is not None:
                return prepared_stream
            return web.Response(status=499)  # Client Closed Request
        print(f"[CatPawProxy] Upstream error: {type(e).__name__}: {e}", flush=True, file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        if prepared_stream is not None:
            try:
                error_chunk = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                await prepared_stream.write(f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode())
                await prepared_stream.write(b"data: [DONE]\n\n")
                await prepared_stream.write_eof()
            except Exception:
                pass
            return prepared_stream
        return web.json_response(
            {"error": {"message": f"Upstream error: {e}", "type": "upstream_error"}},
            status=502,
        )
    except ConnectionResetError as e:
        # Python builtin ConnectionResetError (different from aiohttp's)
        print(f"[CatPawProxy] Connection reset by client (normal): {e}", flush=True, file=sys.stderr)
        if prepared_stream is not None:
            return prepared_stream
        return web.Response(status=499)
    except asyncio.TimeoutError:
        print(f"[CatPawProxy] Upstream timeout", flush=True, file=sys.stderr)
        if prepared_stream is not None:
            try:
                error_chunk = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                await prepared_stream.write(f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode())
                await prepared_stream.write(b"data: [DONE]\n\n")
                await prepared_stream.write_eof()
            except Exception:
                pass
            return prepared_stream
        return web.json_response(
            {"error": {"message": "Upstream timeout", "type": "timeout_error"}},
            status=504,
        )


async def handle_models(request: web.Request) -> web.Response:
    """Return a fake model list with glm-5.2 for CLIProxyAPI."""
    models = {
        "object": "list",
        "data": [
            {
                "id": MODEL_NAME,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "catpaw",
            },
        ],
    }
    return web.json_response(models)


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    try:
        auth = get_catpaw_auth()
        encryption_enabled = get_rsa_public_key() is not None
        return web.json_response({
            "status": "ok",
            "auth": {"mis_id": auth["mis_id"], "token_age": int(time.time() - auth["ts"])},
            "encryption": {"enabled": encryption_enabled},
            "upstream": CATPAW_API_BASE,
        })
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=503)
