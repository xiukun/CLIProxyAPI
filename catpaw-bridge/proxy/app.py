"""App creation + main entry point."""

import sys

from aiohttp import web

from proxy.config import (
    LISTEN_HOST,
    LISTEN_PORT,
    CATPAW_API_BASE,
    CATPAW_DATA_DIR,
    MODEL_NAME,
    MODEL_TYPE_CODE,
    VERBOSE,
    STRIP_TOOL_DEFINITIONS,
)
from proxy.crypto import RSA_PUBLIC_KEY_PEM
from proxy.auth import get_catpaw_auth
from proxy.handlers import (
    handle_chat_completions,
    handle_models,
    handle_health,
    close_session,
)


def create_app() -> web.Application:
    app = web.Application(client_max_size=100 * 1024 * 1024)

    # Health check
    app.router.add_get("/health", handle_health)

    # Model list (for CLIProxyAPI discovery)
    app.router.add_get("/v1/models", handle_models)

    # Chat completions (both streaming and non-streaming)
    app.router.add_post("/v1/chat/completions", handle_chat_completions)

    # Cleanup on shutdown
    async def _on_cleanup(app):
        await close_session()
    app.on_cleanup.append(_on_cleanup)

    return app


def main():
    print(f"[CatPawProxy] Starting on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    print(f"[CatPawProxy] Upstream: {CATPAW_API_BASE}", flush=True)
    print(f"[CatPawProxy] Data dir: {CATPAW_DATA_DIR}", flush=True)
    print(f"[CatPawProxy] Encryption: {'enabled' if RSA_PUBLIC_KEY_PEM else 'disabled'}", flush=True)
    print(f"[CatPawProxy] Model: {MODEL_NAME} (type_code={MODEL_TYPE_CODE})", flush=True)
    print(f"[CatPawProxy] Strip tool definitions: {STRIP_TOOL_DEFINITIONS}", flush=True)

    # Pre-load auth
    try:
        auth = get_catpaw_auth()
        print(f"[CatPawProxy] Auth OK: mis_id={auth['mis_id']}", flush=True)
    except Exception as e:
        print(f"[CatPawProxy] WARNING: Auth pre-load failed: {e}", flush=True, file=sys.stderr)
        print("[CatPawProxy] Make sure CatPawAI IDE is logged in.", flush=True, file=sys.stderr)

    app = create_app()
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT, print=None)
