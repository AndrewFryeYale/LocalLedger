"""FastAPI app: public marketing pages + gated pilot app + war room + admin. Server-rendered (Jinja2 + HTMX)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from localledger.config import get_settings
from localledger.db import init_db

WEB_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    from localledger.web import routes_admin, routes_pilot, routes_public, routes_warroom

    settings = get_settings()
    application = FastAPI(title=settings.brand, lifespan=lifespan, docs_url=None, redoc_url=None)
    application.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        session_cookie="ll_session",
        max_age=60 * 60 * 24 * 30,
    )
    application.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    application.include_router(routes_public.router)
    application.include_router(routes_pilot.router)
    application.include_router(routes_warroom.router)
    application.include_router(routes_admin.router)
    return application


app = create_app()
