"""Application entry point: `uvicorn app.main:app`."""
import hashlib
import logging
import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from . import config, routines
from .api import router
from .costs_api import router as costs_router
from .home_api import home_router, openai_router, tokens_router
from .db import init_db

logging.basicConfig(level=logging.INFO)
STATIC = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    from . import memory
    try:  # memory and skills moved from the database to a git-backed knowledge base per project
        memory.migrate_all()
    except Exception:  # noqa: BLE001 - e.g. git missing: knowledge bases are then created on first use
        logging.getLogger("teambots").exception("knowledge base migration failed")
    routines.start()
    yield
    routines.stop()


app = FastAPI(title="TeamBots", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
# Honour X-Forwarded-Proto / X-Forwarded-Host when sitting behind Caddy, Brev, cloudflared, etc.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")


@app.middleware("http")
async def remember_connecting_ip(request: Request, call_next):
    """Record the immediate peer before ProxyHeadersMiddleware rewrites request.client.

    Registered after that middleware so this hook runs first. Auth rate limiting
    then trusts X-Forwarded-For / CF-Connecting-IP only when this peer is local.
    """
    request.state.connecting_ip = request.client.host if request.client else ""
    return await call_next(request)


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    """Never serve a cached API response, and make the app shell revalidate so a new
    build shows up on the next load instead of a stale cached app.js / index.html."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    elif path == "/" or path.startswith("/static"):
        # no-cache = may store, but must revalidate (ETag) before use, so a change is picked up.
        response.headers["Cache-Control"] = "no-cache"
    return response


app.include_router(router)
app.include_router(costs_router)
app.include_router(tokens_router)
app.include_router(home_router)
app.include_router(openai_router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _asset_version() -> str:
    """A short hash of the shell assets, so index.html can cache-bust them automatically:
    every build gets a new app.js?v=... URL, which no CDN or browser can serve stale."""
    h = hashlib.sha1()
    for name in ("app.js", "app.css"):
        try:
            with open(os.path.join(STATIC, name), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()[:10]


def _docs_or_404():
    if not config.ENABLE_DOCS:
        raise HTTPException(404, "not found")


@app.get("/openapi.json", include_in_schema=False)
def openapi_spec():
    _docs_or_404()
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False)
def swagger_ui():
    _docs_or_404()
    return get_swagger_ui_html(openapi_url="/openapi.json", title="TeamBots")


@app.get("/redoc", include_in_schema=False)
def redoc_ui():
    _docs_or_404()
    return get_redoc_html(openapi_url="/openapi.json", title="TeamBots")


@app.get("/")
def index():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read()
    ver = _asset_version()
    html = re.sub(r'(/static/app\.(?:js|css))(?:\?v=[^"\']*)?', rf'\1?v={ver}', html)
    return HTMLResponse(html)


@app.get("/sw.js")
def service_worker():
    """Served from the root so the service worker may control the whole app (installable PWA)."""
    return FileResponse(os.path.join(STATIC, "sw.js"), media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})
