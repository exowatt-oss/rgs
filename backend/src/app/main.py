"""FastAPI app factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

import structlog
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from forwarded_auth import get_settings as get_auth_settings
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.deps import get_current_user
from app.models.user import User
from app.routers import auth as auth_router
from app.routers import health as health_router
from app.routers import live as live_router
from app.routers import live_audio as live_audio_router
from app.routers import live_ondevice as live_ondevice_router


class SPAStaticFiles(StaticFiles):
    """StaticFiles that falls back to index.html on 404 so client-side
    routes (anything react-router renders below `/`) survive a hard
    reload or pasted URL. Without this, hitting a non-root SPA path
    returns {"detail":"Not Found"} from FastAPI before the router gets
    a chance to handle it.

    Two cases deliberately do NOT fall back, so they keep their honest 404:

    1. ``api/...`` — anything under the API prefix. A typo'd endpoint
       should surface as a JSON 404, not silently serve HTML; otherwise
       a fetch() call gets a parse error instead of a clear "no such
       endpoint" signal.
    2. Paths whose final segment looks like a file (contains a dot —
       ``foo.js``, ``logo.png``, ``manifest.webmanifest``, …). A missing
       asset should stay missing so build / deploy regressions are
       visible, not papered over with the SPA shell.
    """

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            # Don't paper over API 404s or missing-asset 404s. The path
            # passed here is relative to the mount (no leading slash).
            if path.startswith("api/") or "." in path.rsplit("/", 1)[-1]:
                raise
            return await super().get_response("index.html", scope)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        stream=sys.stdout,
        format="%(message)s",
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    log = structlog.get_logger("app")
    settings = get_settings()
    log.info("startup", environment=settings.environment, app_env=settings.app_env)
    from app.live.runtime import manager as live_manager
    from app.live.settings import get_live_settings

    # Resume the idle/stuck-session sweeper so a restart mid-session still
    # finalizes (audio reconnects recreate runtimes on demand).
    if settings.is_prod and get_live_settings().live_record_enabled:
        live_manager.ensure_sweeper()
    # No background recap deliveries in the standalone default configuration.
    # A fresh MCP sub-app per lifespan: its session manager may only run once
    # per instance, and the test suite enters the same app's lifespan many
    # times. A mounted sub-app's lifespan does not run on its own; without
    # this every /mcp request 500s ("Task group is not initialized").
    from forwarded_auth import get_settings as get_auth_settings

    from app.mcp_server import build_mcp_app

    auth = get_auth_settings()
    if settings.is_prod:
        if auth.mode == "none":
            raise RuntimeError("FORWARDED_AUTH_MODE=none is forbidden in production")
        if not auth.require_jwt or auth.trust_proxy_headers:
            raise RuntimeError("Unsigned forwarded headers are forbidden in production")
    auth.validate()
    sub = build_mcp_app()
    app.state.mcp_app = sub
    try:
        async with sub.router.lifespan_context(sub):
            yield
    finally:
        app.state.mcp_app = None
        with contextlib.suppress(Exception):
            await asyncio.wait_for(live_manager.shutdown(), timeout=20)
        log.info("shutdown")


# Static public preview, with no transcript data or authenticated API calls.
_PREVIEW_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>RGS</title>
  <style>
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: linear-gradient(135deg, #f1f5f9 0%, #ffffff 100%);
      color: #0f172a;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .card {
      max-width: 560px;
      padding: 56px 48px;
      text-align: center;
    }
    h1 { font-size: 36px; margin: 0 0 12px; letter-spacing: -0.02em; }
    p  { font-size: 18px; margin: 0; color: #475569; }
  </style>
</head>
<body>
  <div class="card">
    <h1>RGS</h1>
    <p>Your agent in every call.</p>
  </div>
</body>
</html>
"""


class _MCPProxy:
    """ASGI shim: forwards /mcp to ``app.state.mcp_app`` (built per lifespan)."""

    def __init__(self, parent: FastAPI) -> None:
        self.parent = parent

    async def __call__(self, scope, receive, send) -> None:
        sub = getattr(self.parent.state, "mcp_app", None)
        if sub is None:
            response = JSONResponse({"detail": "MCP surface is starting"}, status_code=503)
            await response(scope, receive, send)
            return
        await sub(scope, receive, send)


class LocalOnlyGuard:
    """Block DNS rebinding and non-loopback clients in explicit demo auth mode.

    Covers HTTP, WebSocket and the mounted MCP ASGI app. Does not replace
    verified identity in production; none mode is never allowed there.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in {"http", "websocket"} and get_auth_settings().mode == "none":
            headers = dict(scope.get("headers", []))
            host = headers.get(b"host", b"").decode("ascii", "ignore").lower()
            name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            client = (scope.get("client") or ("",))[0]
            try:
                loopback = ip_address(client).is_loopback
            except ValueError:
                loopback = False
            local_host = name in {"localhost", "127.0.0.1", "[::1]"}
            # Starlette's in-process test client uses testserver/testclient only.
            if get_settings().app_env == "test" and get_settings().environment == "test":
                loopback = loopback or client == "testclient"
                local_host = local_host or name == "testserver"
            # A local dev reverse proxy may rewrite Host/client to loopback.
            # Refuse forwarding headers and browser origins/referers outside
            # loopback so a Vite dev server bound to the LAN cannot relay
            # unsuspecting browsers into the fixed demo identity.
            forwarded = any(
                key in headers for key in (b"forwarded", b"x-forwarded-for", b"x-real-ip")
            )
            remote_browser = False
            for key in (b"origin", b"referer"):
                if raw := headers.get(key):
                    parsed = urlsplit(raw.decode("ascii", "ignore"))
                    remote_browser = remote_browser or parsed.hostname not in {
                        "localhost", "127.0.0.1", "::1"
                    }
            if not (loopback and local_host) or forwarded or remote_browser:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4403})
                else:
                    await JSONResponse({"detail": "Demo auth is loopback-only"}, status_code=403)(
                        scope, receive, send
                    )
                return
        await self.app(scope, receive, send)


class OriginGuard:
    """Reject cross-site browser writes even when a proxy injects a bearer.

    Requests with no browser origin remain valid for authenticated CLI agents.
    A separately hosted frontend must be explicitly listed in CORS_ORIGINS.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("method") in {"POST", "PUT", "PATCH", "DELETE"}:
            headers = dict(scope.get("headers", []))
            host = headers.get(b"host", b"").decode("ascii", "ignore").lower()
            settings = get_settings()
            dev = {"http://localhost:5173", "http://127.0.0.1:5173"}
            configured = set(settings.cors_origins) or (dev if not settings.is_prod else set())
            allowed = {f"https://{host}", f"http://{host}"} | configured if host else configured
            origin = headers.get(b"origin", b"").decode("ascii", "ignore").lower()
            referer = headers.get(b"referer", b"").decode("ascii", "ignore").lower()
            ref_origin = (
                f"{urlsplit(referer).scheme}://{urlsplit(referer).netloc}" if referer else ""
            )
            cross_site = headers.get(b"sec-fetch-site", b"").lower() == b"cross-site"
            if (origin and origin not in allowed) or (ref_origin and ref_origin not in allowed) \
                    or (cross_site and origin not in allowed):
                await JSONResponse({"detail": "Cross-site write denied"}, status_code=403)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def create_app() -> FastAPI:
    settings = get_settings()
    _configure_logging(settings.log_level)

    app = FastAPI(
        title="rgs",
        version="0.1.0",
        # Docs/schema stay behind the same verified identity as the API.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )

    app.add_middleware(LocalOnlyGuard)
    app.add_middleware(OriginGuard)

    # The OpenAPI schema is available only to authenticated users.
    @app.get("/api/openapi.json", include_in_schema=False)
    async def openapi_schema(_user: User = Depends(get_current_user)) -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/api/docs", include_in_schema=False)
    async def swagger_docs(_user: User = Depends(get_current_user)) -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url="/api/openapi.json", title=f"{app.title} - Swagger UI"
        )

    # In production the frontend is served from the same origin (mounted at /),
    # so CORS isn't needed and exposing it would only widen the surface.
    if not settings.is_prod:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins
            or ["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(auth_router.router, prefix="/api")
    app.include_router(health_router.router, prefix="/api")
    app.include_router(live_router.router, prefix="/api")
    app.include_router(live_ondevice_router.router, prefix="/api")
    app.include_router(live_audio_router.router, prefix="/api")

    # MCP surface for agents. Mounted
    # BEFORE the SPA so its 404 fallback cannot swallow /mcp; the proxy
    # forwards to the sub-app the current lifespan built.
    app.state.mcp_app = None
    app.mount("/mcp", _MCPProxy(app))

    # App-to-bot integrations belong in a private integration overlay.

    # Public static preview, mounted before SPA fallback.
    @app.get("/preview", response_class=HTMLResponse)
    async def preview() -> str:
        return _PREVIEW_HTML

    if settings.is_prod:
        # backend/src/app/main.py → ../../../frontend/dist
        frontend_dist = Path(__file__).resolve().parents[3] / "frontend" / "dist"
        if frontend_dist.is_dir():
            app.mount("/", SPAStaticFiles(directory=str(frontend_dist), html=True), name="static")

    return app


app = create_app()
