"""Shared test infrastructure for the fetcher tests and the web-route tests.

The fetchers build their own httpx.AsyncClient per call (BaseFetcher.
make_client), so there's no constructor seam to inject a fake client through.
Instead we monkeypatch make_client() to return a client wired to an
httpx.MockTransport — a routing function that answers each request from a
frozen fixture file instead of hitting the network. See fixtures/README.md
for what's real vs hand-trimmed in each fixture.

The web routes (app/routers/*.py) pull their DB session from
app.database.async_session_factory directly rather than through a FastAPI
dependency, so there's no per-test override seam there either — the fix is
the DATABASE_URL environment variable below, which must be set before
anything imports app.database (directly, or transitively via app.models,
which every fetcher module already does). It points the whole test session
at a throwaway SQLite file instead of the Postgres the app normally runs
against, so `pytest` works without a live database.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable

import httpx
import pytest

_TEST_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="patchwatch-test-db-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TEST_DB_PATH}")

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(*parts: str) -> str:
    return (FIXTURES_DIR.joinpath(*parts)).read_text()


def load_fixture_json(*parts: str):
    return json.loads(load_fixture(*parts))


@pytest.fixture
def mock_fetch(monkeypatch):
    """Returns a function that patches BaseFetcher.make_client for the
    duration of the test. Pass it a dict of {url: (status_code, body)} or a
    routing callable `(request) -> httpx.Response`.

    Any request that isn't explicitly mapped gets a 404 — better a loud test
    failure than a silent real network call.
    """

    def _install(routes: dict[str, tuple[int, str]] | Callable[[httpx.Request], httpx.Response]) -> None:
        if callable(routes):
            handler = routes
        else:
            def handler(request: httpx.Request) -> httpx.Response:
                key = str(request.url)
                if key not in routes:
                    return httpx.Response(404, text=f"unmapped URL in test: {key}")
                status, body = routes[key]
                return httpx.Response(status, text=body)

        transport = httpx.MockTransport(handler)

        def fake_make_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=transport)

        # Patched on BaseFetcher itself (a @staticmethod), so it takes effect
        # for whichever fetcher subclass the test instantiates.
        import app.fetchers.base as base_module

        monkeypatch.setattr(base_module.BaseFetcher, "make_client", staticmethod(fake_make_client))

    return _install


@pytest.fixture
async def db_session():
    """Fresh schema for one test, on the SQLite file from DATABASE_URL above.
    Function-scoped so tests can't see each other's rows without needing
    per-test transaction-rollback machinery — cheap enough on SQLite to just
    create/drop every table around each test."""
    from app.database import Base, engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client(db_session):
    """httpx client wired directly to the FastAPI app via ASGI. Deliberately
    does *not* run the app's lifespan (see app.main.lifespan) — that would
    call init_db() again, start the background scheduler, and, with the
    default FETCH_ON_STARTUP, kick off a real fetch against Microsoft's
    servers. None of that belongs in a route test; db_session already covers
    the one thing lifespan would have given us (a created schema)."""
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def make_product():
    """Factory fixture: make_product("key", **overrides) -> an unsaved
    Product with sane defaults, so a test only has to spell out the fields it
    actually cares about. Caller still has to session.add(...)/flush/commit."""
    from app.models import Product

    def _make(key: str, **overrides) -> Product:
        defaults = dict(key=key, display_name=key, family="windows_client")
        defaults.update(overrides)
        return Product(**defaults)

    return _make


@pytest.fixture
def make_patch():
    """Factory fixture: make_patch(product_id, **overrides) -> an unsaved
    Patch with sane defaults. Mirrors make_product above."""
    from app.models import Patch

    def _make(product_id: int, **overrides) -> Patch:
        defaults = dict(
            product_id=product_id,
            kb_number="",
            build="",
            title="Test patch",
            update_type="Security",
            release_date=None,
            severity=None,
            kb_url=None,
            source="test",
        )
        defaults.update(overrides)
        return Patch(**defaults)

    return _make
