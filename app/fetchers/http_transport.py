"""httpx transport shared by every fetcher (see BaseFetcher.make_client):
retries transient failures and paces requests to hosts known to rate-limit.

Why a transport and not a helper around client.get(): every fetcher already
goes through make_client, and follow_redirects sends each redirect hop through
the transport separately — which matters here, because the 403s seen in
practice come from support.microsoft.com *after* a 301 from learn.microsoft.com
(SQL Server build pages, since 2026-10), so a retry has to happen per hop, not
per original URL.

Retried: 403/429/502/503/504 responses and timeouts/connection errors, for
GET/HEAD only. 403 is normally permanent, but support.microsoft.com's bot
protection answers the very same URL with 200 or 403 seconds apart (observed
2026-10-03/04), so it gets the same treatment as 429. A Retry-After header is
honoured (capped), otherwise the wait doubles per attempt, with jitter.

Paced: requests to THROTTLED_HOSTS are serialized across the whole process
(not just one client) with a minimum gap between them — msrc.py's KB-article
fan-out used to fire several at once, which is what seemed to trip the block.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
import weakref

import httpx

logger = logging.getLogger("patchwatch.fetchers.http")

RETRY_STATUS_CODES = frozenset({403, 429, 502, 503, 504})
RETRY_METHODS = frozenset({"GET", "HEAD"})
# Upper bound for a server-supplied Retry-After, so a hostile/odd value can't
# stall a whole refresh for an hour.
MAX_RETRY_AFTER_SECONDS = 60.0
THROTTLED_HOSTS = frozenset({"support.microsoft.com"})


class _HostThrottle:
    """One lock + "last request started at" per host. Kept per event loop,
    since an asyncio.Lock can't be shared across loops (matters for tests,
    which get a fresh loop per test)."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_start: dict[str, float] = {}

    def lock(self, host: str) -> asyncio.Lock:
        return self._locks.setdefault(host, asyncio.Lock())

    async def wait_turn(self, host: str, min_interval: float) -> None:
        last = self._last_start.get(host)
        if last is not None:
            delay = last + min_interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        self._last_start[host] = time.monotonic()


_throttles: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _HostThrottle] = weakref.WeakKeyDictionary()


def _throttle() -> _HostThrottle:
    loop = asyncio.get_running_loop()
    throttle = _throttles.get(loop)
    if throttle is None:
        throttle = _throttles[loop] = _HostThrottle()
    return throttle


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        seconds = parsed.timestamp() - time.time()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


class RetryingTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        inner: httpx.AsyncBaseTransport | None = None,
        *,
        max_retries: int = 3,
        backoff_seconds: float = 2.0,
        throttle_interval_seconds: float = 1.0,
        throttled_hosts: frozenset[str] = THROTTLED_HOSTS,
    ) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._throttle_interval = throttle_interval_seconds
        self._throttled_hosts = throttled_hosts

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host in self._throttled_hosts:
            throttle = _throttle()
            # Held across retries on purpose: while this host is pushing back,
            # nobody else should be hitting it either.
            async with throttle.lock(request.url.host):
                return await self._send_with_retries(request, throttle)
        return await self._send_with_retries(request, None)

    async def _send_with_retries(self, request: httpx.Request, throttle: _HostThrottle | None) -> httpx.Response:
        retryable = request.method in RETRY_METHODS
        attempt = 0
        while True:
            if throttle is not None:
                await throttle.wait_turn(request.url.host, self._throttle_interval)
            try:
                response = await self._inner.handle_async_request(request)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if not retryable or attempt >= self._max_retries:
                    raise
                delay = self._backoff(attempt)
                logger.info(
                    "%s %s failed (%s), retry %d/%d in %.1fs",
                    request.method, request.url, type(exc).__name__, attempt + 1, self._max_retries, delay,
                )
            else:
                if not retryable or response.status_code not in RETRY_STATUS_CODES or attempt >= self._max_retries:
                    return response
                retry_after = _retry_after_seconds(response)
                delay = retry_after if retry_after is not None else self._backoff(attempt)
                await response.aclose()
                logger.info(
                    "%s %s returned %d, retry %d/%d in %.1fs",
                    request.method, request.url, response.status_code, attempt + 1, self._max_retries, delay,
                )
            await asyncio.sleep(delay)
            attempt += 1

    def _backoff(self, attempt: int) -> float:
        base = self._backoff_seconds * (2**attempt)
        return base + random.uniform(0, base / 2)

    async def aclose(self) -> None:
        await self._inner.aclose()
