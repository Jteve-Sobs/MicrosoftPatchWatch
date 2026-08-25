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

import datetime as dt

import httpx
import pytest
from sqlalchemy import select

from app import refresh_service
from app.config import get_settings
from app.database import async_session_factory
from app.fetchers.base import BaseFetcher, FetchResult, PatchInfo, ProductInfo
from app.models import Patch


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


async def test_patch_kb_hint_fills_in_build_only_row(db_session, monkeypatch):
    """Mirrors the real dotnet.py/msrc.py split: one fetcher creates the real
    row (build number, no KB), another only knows the KB for the same
    product+month (see FetchResult.patch_kb_hints) — the hint must land on
    that row rather than becoming a second, incomplete one, and must only
    touch kb_url — dotnet.py's own release_notes_url (its GitHub link) is a
    separate field precisely so this doesn't clobber it (see
    models.Patch.release_notes_url)."""
    builder = _FakeFetcher(
        FetchResult(
            products=[ProductInfo(key="dotnet-8.0", display_name=".NET 8.0", family="dotnet")],
            patches=[
                PatchInfo(
                    product_key="dotnet-8.0",
                    kb_number=None,
                    build="8.0.30",
                    title=".NET 8.0 – 8.0.30",
                    update_type="Security",
                    release_date=dt.date(2026, 8, 11),
                    severity=None,
                    kb_url="https://github.com/dotnet/core",
                    source="dotnet-core",
                    release_notes_url="https://github.com/dotnet/core",
                )
            ],
        )
    )
    hinter = _FakeFetcher(
        FetchResult(
            patch_kb_hints={("dotnet-8.0", "2026-08"): ("KB5122104", "https://support.microsoft.com/help/5122104")}
        )
    )
    _install_fetchers(monkeypatch, builder, hinter)

    await refresh_service.run_all_fetchers(trigger="test")

    async with async_session_factory() as session:
        rows = (await session.execute(select(Patch))).scalars().all()

    assert len(rows) == 1  # no second, KB-only row
    assert rows[0].build == "8.0.30"
    assert rows[0].kb_number == "KB5122104"
    assert rows[0].kb_url == "https://support.microsoft.com/help/5122104"
    assert rows[0].release_notes_url == "https://github.com/dotnet/core"  # untouched by the hint


async def test_kb_hint_survives_next_runs_build_only_reupsert(db_session, monkeypatch):
    """Regression guard: once a hint fills in a build-only row's kb_number,
    dotnet.py keeps re-upserting that same release every subsequent refresh
    with kb_number=None (it has no way to know the hint happened) — that
    must update the existing row, not insert a second, blank-kb one for the
    same build (see _upsert_patch's build-only branch)."""
    builder = _FakeFetcher(
        FetchResult(
            products=[ProductInfo(key="dotnet-8.0", display_name=".NET 8.0", family="dotnet")],
            patches=[
                PatchInfo(
                    product_key="dotnet-8.0",
                    kb_number=None,
                    build="8.0.30",
                    title=".NET 8.0 – 8.0.30",
                    update_type="Security",
                    release_date=dt.date(2026, 8, 11),
                    severity=None,
                    kb_url=None,
                    source="dotnet-core",
                )
            ],
        )
    )
    hinter = _FakeFetcher(
        FetchResult(patch_kb_hints={("dotnet-8.0", "2026-08"): ("KB5122104", "https://support.microsoft.com/help/5122104")})
    )
    _install_fetchers(monkeypatch, builder, hinter)
    await refresh_service.run_all_fetchers(trigger="test")

    # Next scheduled refresh: same builder output (kb_number=None again),
    # hint source now empty (already applied, nothing new to report).
    _install_fetchers(monkeypatch, builder, _FakeFetcher(FetchResult()))
    await refresh_service.run_all_fetchers(trigger="test")

    async with async_session_factory() as session:
        rows = (await session.execute(select(Patch))).scalars().all()

    assert len(rows) == 1
    assert rows[0].build == "8.0.30"
    assert rows[0].kb_number == "KB5122104"


async def test_patch_kb_hint_does_not_overwrite_manual_edit(db_session, monkeypatch):
    builder = _FakeFetcher(
        FetchResult(
            products=[ProductInfo(key="dotnet-8.0", display_name=".NET 8.0", family="dotnet")],
            patches=[
                PatchInfo(
                    product_key="dotnet-8.0",
                    kb_number=None,
                    build="8.0.30",
                    title=".NET 8.0 – 8.0.30",
                    update_type="Security",
                    release_date=dt.date(2026, 8, 11),
                    severity=None,
                    kb_url=None,
                    source="dotnet-core",
                )
            ],
        )
    )
    hinter = _FakeFetcher(
        FetchResult(patch_kb_hints={("dotnet-8.0", "2026-08"): ("KB9999999", "https://example.invalid")})
    )
    _install_fetchers(monkeypatch, builder, hinter)
    await refresh_service.run_all_fetchers(trigger="test")

    async with async_session_factory() as session:
        row = (await session.execute(select(Patch))).scalar_one()
        row.manually_edited = True
        row.kb_number = "KB-CORRECTED"
        await session.commit()

    # A second refresh with the same hint must not clobber the manual fix.
    _install_fetchers(monkeypatch, builder, hinter)
    await refresh_service.run_all_fetchers(trigger="test")

    async with async_session_factory() as session:
        row = (await session.execute(select(Patch))).scalar_one()
        assert row.kb_number == "KB-CORRECTED"


def _kb_patch_info(**overrides) -> PatchInfo:
    defaults = dict(
        product_key="dotnetfx-4.8",
        kb_number="KB1111111",
        build=None,
        title="t",
        update_type="Security",
        release_date=dt.date(2026, 8, 11),
        severity=None,
        kb_url=None,
        source="msrc",
    )
    defaults.update(overrides)
    return PatchInfo(**defaults)


async def test_release_date_self_heals_on_a_later_refresh(db_session, monkeypatch):
    """Regression guard for the msrc.py bug where every patch that month got
    flattened to the 1st (see _parse_release_date) — once the source starts
    reporting the correct date, an already-stored row must pick it up on its
    next refresh rather than keeping the old wrong one forever."""
    stale = FetchResult(
        products=[ProductInfo(key="dotnetfx-4.8", display_name=".NET Framework 4.8", family="dotnet_framework")],
        patches=[_kb_patch_info(release_date=dt.date(2026, 8, 1))],
    )
    _install_fetchers(monkeypatch, _FakeFetcher(stale))
    await refresh_service.run_all_fetchers(trigger="test")

    corrected = FetchResult(patches=[_kb_patch_info(release_date=dt.date(2026, 8, 11))])
    _install_fetchers(monkeypatch, _FakeFetcher(corrected))
    await refresh_service.run_all_fetchers(trigger="test")

    async with async_session_factory() as session:
        row = (await session.execute(select(Patch))).scalar_one()
        assert row.release_date == dt.date(2026, 8, 11)


async def test_release_date_does_not_overwrite_manual_edit(db_session, monkeypatch):
    _install_fetchers(
        monkeypatch,
        _FakeFetcher(
            FetchResult(
                products=[ProductInfo(key="dotnetfx-4.8", display_name=".NET Framework 4.8", family="dotnet_framework")],
                patches=[_kb_patch_info(release_date=dt.date(2026, 8, 1))],
            )
        ),
    )
    await refresh_service.run_all_fetchers(trigger="test")

    async with async_session_factory() as session:
        row = (await session.execute(select(Patch))).scalar_one()
        row.manually_edited = True
        row.release_date = dt.date(2026, 8, 12)
        await session.commit()

    _install_fetchers(monkeypatch, _FakeFetcher(FetchResult(patches=[_kb_patch_info(release_date=dt.date(2026, 8, 11))])))
    await refresh_service.run_all_fetchers(trigger="test")

    async with async_session_factory() as session:
        row = (await session.execute(select(Patch))).scalar_one()
        assert row.release_date == dt.date(2026, 8, 12)


async def test_kb_url_self_heals_on_a_later_refresh(db_session, monkeypatch):
    """Regression guard for msrc.py's _discover_os_bundles upgrade (Update
    Catalog search link -> readable support.microsoft.com article): a row
    already stored with the old link must pick up the new one on its next
    refresh, not keep the stale link forever."""
    stale = FetchResult(
        products=[ProductInfo(key="dotnetfx-4.8", display_name=".NET Framework 4.8", family="dotnet_framework")],
        patches=[_kb_patch_info(kb_url="https://catalog.update.microsoft.com/v7/site/Search.aspx?q=KB1111111")],
    )
    _install_fetchers(monkeypatch, _FakeFetcher(stale))
    await refresh_service.run_all_fetchers(trigger="test")

    corrected = FetchResult(
        patches=[_kb_patch_info(kb_url="https://support.microsoft.com/en-us/servicing/dotnetframework/kb1111111")]
    )
    _install_fetchers(monkeypatch, _FakeFetcher(corrected))
    await refresh_service.run_all_fetchers(trigger="test")

    async with async_session_factory() as session:
        row = (await session.execute(select(Patch))).scalar_one()
        assert row.kb_url == "https://support.microsoft.com/en-us/servicing/dotnetframework/kb1111111"


async def test_kb_url_does_not_overwrite_manual_edit(db_session, monkeypatch):
    _install_fetchers(
        monkeypatch,
        _FakeFetcher(
            FetchResult(
                products=[ProductInfo(key="dotnetfx-4.8", display_name=".NET Framework 4.8", family="dotnet_framework")],
                patches=[_kb_patch_info(kb_url="https://catalog.update.microsoft.com/v7/site/Search.aspx?q=KB1111111")],
            )
        ),
    )
    await refresh_service.run_all_fetchers(trigger="test")

    async with async_session_factory() as session:
        row = (await session.execute(select(Patch))).scalar_one()
        row.manually_edited = True
        row.kb_url = "https://example.invalid/manually-corrected"
        await session.commit()

    _install_fetchers(
        monkeypatch,
        _FakeFetcher(
            FetchResult(patches=[_kb_patch_info(kb_url="https://support.microsoft.com/en-us/servicing/dotnetframework/kb1111111")])
        ),
    )
    await refresh_service.run_all_fetchers(trigger="test")

    async with async_session_factory() as session:
        row = (await session.execute(select(Patch))).scalar_one()
        assert row.kb_url == "https://example.invalid/manually-corrected"
