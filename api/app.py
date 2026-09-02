"""Application entry point for the Penelusuran Banjir WebGIS.

The visible shell is served before reference/routing data are initialized so the
map can paint quickly while the API core warms on its first request.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT_DIR / "static"
TEMPLATES_DIR = ROOT_DIR / "templates"
SHELL_VERSION = "1.0.1.0"


def _load_project_dotenv_lightweight() -> None:
    """Load ROOT/.env without importing the GIS runtime or third-party dotenv."""
    path = ROOT_DIR / ".env"
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_project_dotenv_lightweight()

shell = FastAPI(title="Penelusuran Banjir Web Shell", version=SHELL_VERSION)
shell.add_middleware(GZipMiddleware, minimum_size=1000)
shell.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@shell.middleware("http")
async def shell_cache_headers(request: Request, call_next):
    """Cache and compress the lightweight first-paint path too.

    Root/static requests intentionally never enter ``api.core``, so the cache
    middleware declared there cannot affect the assets that matter most to the
    initial render.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    elif request.url.path == "/":
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@shell.get("/")
def index(request: Request):
    # Production map display assets are public R2 objects, so the map can draw
    # basin/river layers before the reference/routing engine has finished warming.
    return templates.TemplateResponse(
        request=request,
        name="spatial.html",
        context={
            "map_assets_public_base": os.getenv("R2_MAP_ASSETS_PUBLIC_BASE", "").strip().rstrip("/"),
            # A deployment-level value can be supplied explicitly. If omitted,
            # cache validation is left to the public object/CDN headers.
            "map_assets_version": os.getenv("R2_MAP_ASSETS_VERSION", "").strip() or SHELL_VERSION,
        },
    )


_core_app: Any | None = None
_core_error: BaseException | None = None
_core_error_at = 0.0
_core_load_lock = threading.Lock()


def _load_core_app_sync():
    """Import the GIS application exactly once, outside the first-paint path."""
    global _core_app, _core_error, _core_error_at
    if _core_app is not None:
        return _core_app
    # Avoid a thundering herd immediately after a failed R2/network startup, but
    # allow the next request to retry instead of poisoning the worker forever.
    if _core_error is not None and (time.monotonic() - _core_error_at) < 2.0:
        raise _core_error
    with _core_load_lock:
        if _core_app is not None:
            return _core_app
        if _core_error is not None and (time.monotonic() - _core_error_at) < 2.0:
            raise _core_error
        _core_error = None
        try:
            module = importlib.import_module("api.core")
            _core_app = module.app
            return _core_app
        except BaseException as exc:
            _core_error = exc
            _core_error_at = time.monotonic()
            raise


class LazyCoreDispatcher:
    """Serve the lightweight shell first; lazily delegate API traffic to core."""

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") not in {"http", "websocket"}:
            await shell(scope, receive, send)
            return

        # The root page and static files never need the reference/routing runtime.
        if path == "/" or path.startswith("/static/"):
            await shell(scope, receive, send)
            return

        # API/docs/other application routes initialize the engine in a worker
        # thread so the event loop can continue serving already-open shell assets.
        try:
            core_app = await asyncio.to_thread(_load_core_app_sync)
        except BaseException as exc:
            if scope.get("type") != "http":
                raise
            payload = (
                '{"detail":"Engine penelusuran banjir belum dapat diinisialisasi.",'
                f'"error":"{type(exc).__name__}"}}'
            ).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                    (b"retry-after", b"2"),
                ],
            })
            await send({"type": "http.response.body", "body": payload})
            return
        await core_app(scope, receive, send)


app = LazyCoreDispatcher()


if __name__ == "__main__":
    import uvicorn

    # Run the already-created ASGI callable directly. This keeps `python api/app.py`
    # working even when the repository root is not on Python's import path.
    host = os.getenv("FLOOD_HOST", "127.0.0.5").strip() or "127.0.0.5"
    try:
        port = int(os.getenv("FLOOD_PORT", "8000"))
    except ValueError:
        port = 8000
    uvicorn.run(app, host=host, port=port, reload=False)
