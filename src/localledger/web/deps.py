"""Shared web helpers: templates, DB session dependency, auth."""

from __future__ import annotations

import html
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import markdown as md
from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlmodel import Session

from localledger.compliance import disclosures
from localledger.config import get_settings
from localledger.db import get_engine

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _md(text: str | None) -> Markup:
    # Escape raw HTML first: generated text is never trusted as markup.
    return Markup(md.markdown(html.escape(text or ""), extensions=["tables"]))


def _money(value: float | None) -> str:
    if value is None:
        return "-"
    if abs(value) >= 1e9:
        return f"${value / 1e9:,.2f}B"
    if abs(value) >= 1e6:
        return f"${value / 1e6:,.1f}M"
    return f"${value:,.0f}"


def _num(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "-"
    return f"{value:,.{digits}f}"


def _fromjson(value: str | None):
    try:
        return json.loads(value or "null")
    except json.JSONDecodeError:
        return None


TEMPLATES.env.filters.update(md=_md, money=_money, num=_num, fromjson=_fromjson)
TEMPLATES.env.globals.update(
    brand=lambda: get_settings().brand,
    disclosures=disclosures,
    contact_email=lambda: get_settings().contact_email,
    year=lambda: datetime.now(UTC).year,
)


def get_session() -> Iterator[Session]:
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session


def current_user(request: Request) -> dict | None:
    user = request.session.get("user")
    return user if isinstance(user, dict) and user.get("name") else None


class LoginRequired(HTTPException):
    def __init__(self, next_url: str) -> None:
        super().__init__(status_code=303, headers={"Location": f"/login?next={next_url}"})


def require_pilot(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise LoginRequired(request.url.path)
    return user


def require_admin(request: Request) -> dict:
    user = require_pilot(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin passcode required")
    return user


def render(request: Request, name: str, **ctx):
    ctx.setdefault("user", current_user(request))
    return TEMPLATES.TemplateResponse(request, name, ctx)
