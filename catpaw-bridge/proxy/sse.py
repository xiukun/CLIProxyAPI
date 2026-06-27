"""SSE stream response helpers.

Shared utilities for creating SSE StreamResponse objects and sending
keepalive comments during buffering mode.
"""

import time

from aiohttp import web


# Keepalive interval for buffering mode (seconds between SSE keepalive comments)
_KEEPALIVE_INTERVAL = 5


def _create_sse_stream_response() -> web.StreamResponse:
    """Create a standard SSE StreamResponse with consistent headers."""
    return web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _send_keepalive(stream: web.StreamResponse, last_keepalive: list) -> None:
    """Send an SSE keepalive comment if enough time has elapsed.

    Uses a 1-element list as mutable container for last_keepalive timestamp,
    so the caller's state is updated in-place.
    """
    if stream and time.time() - last_keepalive[0] > _KEEPALIVE_INTERVAL:
        try:
            await stream.write(b": keepalive\n\n")
            last_keepalive[0] = time.time()
        except Exception:
            pass
