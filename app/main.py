"""Application entry point: `uvicorn app.main:app`."""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from . import routines
from .api import router
from .costs_api import router as costs_router
from .home_api import home_router, openai_router, tokens_router
from .db import init_db

logging.basicConfig(level=logging.INFO)
STATIC = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    routines.start()
    yield
    routines.stop()


app = FastAPI(title="TeamBots", lifespan=lifespan)
# Honour X-Forwarded-Proto / X-Forwarded-Host when sitting behind Caddy, Brev, cloudflared, etc.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
app.include_router(router)
app.include_router(costs_router)
app.include_router(tokens_router)
app.include_router(home_router)
app.include_router(openai_router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/sw.js")
def service_worker():
    """Served from the root so the service worker may control the whole app (installable PWA)."""
    return FileResponse(os.path.join(STATIC, "sw.js"), media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})
