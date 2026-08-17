"""Shared test infrastructure for the fetcher tests.

The fetchers build their own httpx.AsyncClient per call (BaseFetcher.
make_client), so there's no constructor seam to inject a fake client through.
Instead we monkeypatch make_client() to return a client wired to an
httpx.MockTransport — a routing function that answers each request from a
frozen fixture file instead of hitting the network. See fixtures/README.md
for what's real vs hand-trimmed in each fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import httpx
import pytest

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
