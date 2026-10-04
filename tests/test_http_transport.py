"""Tests for the shared fetcher transport (app/fetchers/http_transport.py):
retries on transient failures and per-host pacing."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.fetchers.http_transport import RetryingTransport


def _client(handler, **kwargs) -> httpx.AsyncClient:
    kwargs.setdefault("backoff_seconds", 0.0)
    kwargs.setdefault("throttle_interval_seconds", 0.0)
    transport = RetryingTransport(httpx.MockTransport(handler), **kwargs)
    return httpx.AsyncClient(transport=transport, follow_redirects=True)


@pytest.mark.parametrize("status", [403, 429, 503])
async def test_retries_transient_status_then_succeeds(status):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status if calls < 3 else 200, text="ok")

    async with _client(handler) as client:
        resp = await client.get("https://example.com/page")
    assert resp.status_code == 200
    assert calls == 3


async def test_gives_up_after_max_retries_and_returns_last_response():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(403)

    async with _client(handler, max_retries=2) as client:
        resp = await client.get("https://example.com/page")
    assert resp.status_code == 403
    assert calls == 3  # 1 try + 2 retries


async def test_does_not_retry_404():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    async with _client(handler) as client:
        resp = await client.get("https://example.com/page")
    assert resp.status_code == 404
    assert calls == 1


async def test_does_not_retry_post():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    async with _client(handler) as client:
        resp = await client.post("https://example.com/page")
    assert resp.status_code == 503
    assert calls == 1


async def test_retries_timeout_then_reraises_when_exhausted():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    async with _client(handler, max_retries=2) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.get("https://example.com/page")
    assert calls == 3


async def test_retries_the_redirect_target_hop():
    """The real-world case: learn.microsoft.com 301s to support.microsoft.com,
    which then 403s once — only that second hop should be repeated."""
    hits: list[str] = []

    def handler(request):
        hits.append(str(request.url))
        if request.url.host == "learn.microsoft.com":
            return httpx.Response(301, headers={"Location": "https://support.microsoft.com/sql"})
        return httpx.Response(403 if hits.count("https://support.microsoft.com/sql") == 1 else 200, text="ok")

    async with _client(handler) as client:
        resp = await client.get("https://learn.microsoft.com/sql")
    assert resp.status_code == 200
    assert hits == [
        "https://learn.microsoft.com/sql",
        "https://support.microsoft.com/sql",
        "https://support.microsoft.com/sql",
    ]


async def test_honours_retry_after_header():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0.2"})
        return httpx.Response(200)

    async with _client(handler) as client:
        start = time.monotonic()
        await client.get("https://example.com/page")
    assert time.monotonic() - start >= 0.2


async def test_throttled_host_requests_are_serialized_and_spaced():
    in_flight = 0
    max_in_flight = 0
    starts: list[float] = []

    async def handler(request):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        starts.append(time.monotonic())
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200)

    async with _client(handler, throttle_interval_seconds=0.05) as client:
        await asyncio.gather(*(client.get(f"https://support.microsoft.com/help/{i}") for i in range(4)))
    assert max_in_flight == 1
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(gap >= 0.045 for gap in gaps)


async def test_other_hosts_are_not_serialized():
    in_flight = 0
    max_in_flight = 0

    async def handler(request):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return httpx.Response(200)

    async with _client(handler, throttle_interval_seconds=1.0) as client:
        await asyncio.gather(*(client.get(f"https://example.com/{i}") for i in range(4)))
    assert max_in_flight == 4
