"""Test harness: every test gets a fresh SQLite DB and temp raw/out/cache dirs, offline HTTP and the mock LLM."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]

os.environ["LLM_PROVIDER"] = "mock"
os.environ["LL_OFFLINE"] = "1"


class FakeHttp:
    """Maps URLs to fixture files. Keys ending in '*' match by prefix. Unknown URLs return 404."""

    def __init__(self, routes: dict[str, Path | bytes | tuple[int, bytes]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def _resolve(self, url: str):
        if url in self.routes:
            return self.routes[url]
        for key, value in self.routes.items():
            if key.endswith("*") and url.startswith(key[:-1]):
                return value
        return None

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        params = kwargs.get("params")
        full = str(httpx.URL(url, params=params)) if params else url
        self.calls.append(full)
        route = self._resolve(full) or self._resolve(url)
        if route is None:
            return httpx.Response(404, content=b"not found", request=httpx.Request(method, full))
        status, body = (200, route) if not isinstance(route, tuple) else route
        if isinstance(body, Path):
            body = body.read_bytes()
        return httpx.Response(status, content=body, request=httpx.Request(method, full))

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def get_ok(self, url: str, **kwargs) -> httpx.Response:
        resp = self.get(url, **kwargs)
        if resp.status_code != 200:
            from localledger.http import FetchError

            raise FetchError(f"HTTP {resp.status_code} for {url}")
        return resp


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("LL_RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("LL_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("LL_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LL_OFFLINE", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    from localledger.config import reset_settings
    from localledger.db import init_db, reset_engine

    reset_settings()
    reset_engine()
    init_db()
    yield tmp_path
    reset_engine()
    reset_settings()


@pytest.fixture()
def session(env):
    from sqlmodel import Session

    from localledger.db import get_engine

    with Session(get_engine(), expire_on_commit=False) as s:
        yield s


@pytest.fixture()
def seeded(session):
    from localledger.config import get_settings
    from localledger.seed import seed_universe

    seed_universe(session, get_settings().root / "data" / "Microcap_Local_Gov_Universe.xlsx")
    session.commit()
    return session


@pytest.fixture()
def fake_http():
    return FakeHttp
