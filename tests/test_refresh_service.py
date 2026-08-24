"""Tests for app.refresh_service — specifically the fetch-error notification
added alongside notify_fetch_errors (see app/notifier.py's docstring). The
upsert/new-patch-notice logic in run_all_fetchers is already exercised
indirectly by the per-fetcher tests (test_windows_release_health.py etc.);
this module isolates the "a source is broken" path with a fake fetcher,
since none of the real fetchers can be made to fail on demand.

Uses the `db_session` fixture from conftest.py (fresh SQLite schema per
test) and a local mock_ntfy fixture mirroring the one in test_notifier.py,
so a real HTTP call to ntfy never happens.
"""

from __future__ import annotations

import httpx
import pytest

from app import refresh_service
from app.config import get_settings
from app.fetchers.base import BaseFetcher, FetchResult, PatchInfo, ProductInfo


class _FakeFetcher(BaseFetcher):
    name = "fake"

    def __init__(self, result: FetchResult | None = None, raises: Exception | None = None):
        self._result = result
        self._raises = raises

    async def fetch(self) -> FetchResult:
        if self._raises is not None:
            raise self._raises
        return self._result if self._result is not None else FetchResult()


def _install_fetchers(monkeypatch, *fetchers: BaseFetcher) -> None:
    monkeypatch.setattr(refresh_service, "get_fetchers", lambda: list(fetchers))


@pytest.fixture(autouse=True)
def _reset_settings_cache(monkeypatch):
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("NTFY_URL", raising=False)
    get_settings.cache_clear()


class _CapturedRequests:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []


@pytest.fixture
def mock_ntfy(monkeypatch):
    """Same mechanism as test_notifier.py's fixture of the same name —
    routes app.notifier's outgoing httpx.AsyncClient through a MockTransport
    instead of the network."""
    captured = _CapturedRequests()

    def handler(request: httpx.Request) -> httpx.Response:
        captured.requests.append(request)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    from app import notifier

    monkeypatch.setattr(notifier.httpx, "AsyncClient", fake_async_client)
    return captured


async def test_broken_fetcher_sends_alert(db_session, monkeypatch, mock_ntfy):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()
    _install_fetchers(monkeypatch, _FakeFetcher(FetchResult(errors=["fake: boom"])))

    await refresh_service.run_all_fetchers(trigger="test")

    assert len(mock_ntfy.requests) == 1
    request = mock_ntfy.requests[0]
    # No products touched -> status "error" -> the "all sources failed" wording.
    assert request.headers["Title"] == "MicrosoftPatchWatch: all sources failed"
    assert request.headers["Priority"] == "urgent"
    assert "fake: boom" in request.read().decode("utf-8")


async def test_crashing_fetcher_also_alerts(db_session, monkeypatch, mock_ntfy):
    """A fetcher that raises (rather than returning FetchResult.errors) is
    caught in run_all_fetchers and turned into an "{name}: {exc}" error —
    that path should alert exactly the same way."""
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()
    _install_fetchers(monkeypatch, _FakeFetcher(raises=RuntimeError("connection reset")))

    await refresh_service.run_all_fetchers(trigger="test")

    assert len(mock_ntfy.requests) == 1
    assert "fake: connection reset" in mock_ntfy.requests[0].read().decode("utf-8")


async def test_still_broken_next_run_does_not_realert(db_session, monkeypatch, mock_ntfy):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()
    _install_fetchers(monkeypatch, _FakeFetcher(FetchResult(errors=["fake: boom"])))

    await refresh_service.run_all_fetchers(trigger="test")
    await refresh_service.run_all_fetchers(trigger="test")

    # Same error text both times -> only the first run (the transition into
    # "broken") should have alerted.
    assert len(mock_ntfy.requests) == 1


async def test_changed_error_text_realerts(db_session, monkeypatch, mock_ntfy):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()

    _install_fetchers(monkeypatch, _FakeFetcher(FetchResult(errors=["fake: boom"])))
    await refresh_service.run_all_fetchers(trigger="test")

    _install_fetchers(monkeypatch, _FakeFetcher(FetchResult(errors=["fake: kaboom"])))
    await refresh_service.run_all_fetchers(trigger="test")

    assert len(mock_ntfy.requests) == 2


async def test_recovery_then_breaking_again_realerts(db_session, monkeypatch, mock_ntfy):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()

    _install_fetchers(monkeypatch, _FakeFetcher(FetchResult(errors=["fake: boom"])))
    await refresh_service.run_all_fetchers(trigger="test")

    _install_fetchers(monkeypatch, _FakeFetcher(FetchResult()))  # healthy run in between
    await refresh_service.run_all_fetchers(trigger="test")

    _install_fetchers(monkeypatch, _FakeFetcher(FetchResult(errors=["fake: boom"])))
    await refresh_service.run_all_fetchers(trigger="test")

    # Break, recover, break again with the *same* text as before recovery ->
    # still two alerts, since the healthy run in between reset the baseline.
    assert len(mock_ntfy.requests) == 2


async def test_partial_failure_uses_high_priority_not_urgent(db_session, monkeypatch, mock_ntfy):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()

    healthy = FetchResult(
        products=[ProductInfo(key="p1", display_name="Product 1", family="windows_client")],
        patches=[
            PatchInfo(
                product_key="p1",
                kb_number="KB1",
                build="",
                title="t",
                update_type=None,
                release_date=None,
                severity=None,
                kb_url=None,
                source="fake-healthy",
            )
        ],
    )
    broken = FetchResult(errors=["fake-broken: boom"])
    _install_fetchers(monkeypatch, _FakeFetcher(healthy), _FakeFetcher(broken))

    await refresh_service.run_all_fetchers(trigger="test")

    # This is the first-ever run, so the new-patch notice is suppressed
    # (see run_all_fetchers' is_first_run) -> the one request left is the
    # error alert, at "partial" (not "error") since a product was touched.
    assert len(mock_ntfy.requests) == 1
    request = mock_ntfy.requests[0]
    assert request.headers["Title"] == "MicrosoftPatchWatch: a source failed"
    assert request.headers["Priority"] == "high"


async def test_healthy_run_does_not_alert(db_session, monkeypatch, mock_ntfy):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()
    _install_fetchers(monkeypatch, _FakeFetcher(FetchResult()))

    await refresh_service.run_all_fetchers(trigger="test")

    assert mock_ntfy.requests == []
