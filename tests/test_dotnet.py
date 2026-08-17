"""Tests for app.fetchers.dotnet against frozen real dotnet/core JSON — see
tests/fixtures/README.md for exactly what's in each fixture and why.
"""

from __future__ import annotations

import datetime as dt

import httpx

from app.fetchers.dotnet import INDEX_URL, DotNetFetcher
from tests.conftest import load_fixture, load_fixture_json

CHANNEL_URL = "https://builds.dotnet.microsoft.com/dotnet/release-metadata/{}/releases.json"


def _routes():
    return {
        INDEX_URL: (200, load_fixture("dotnet", "releases_index.json")),
        CHANNEL_URL.format("9.0"): (200, load_fixture("dotnet", "releases_9_0.json")),
        CHANNEL_URL.format("1.0"): (200, load_fixture("dotnet", "releases_1_0.json")),
        # Deliberately unmapped: the "3.1" entry in releases_index.json has a
        # releases.json URL, but we don't provide a fixture for it — the
        # mock_fetch 404 default stands in for "that request failed", which
        # is what exercises the per-channel error/fallback path below.
    }


async def test_full_history_fetched_for_every_channel_including_eol(mock_fetch):
    """Regression test for the bug fixed in dotnet.py: channels used to only
    get full release history when support-phase was active/maintenance/
    preview/go-live, silently starving EOL channels (5.0, Core 1.x-3.x, ...)
    of everything but their last-ever release. releases_1_0.json fixture is
    a long-EOL channel that still has 3 real historical releases — if the
    fetcher only kept the latest, this would fail."""
    mock_fetch(_routes())
    result = await DotNetFetcher().fetch()

    core_1_0_patches = [p for p in result.patches if p.product_key == "dotnet-1.0"]
    assert len(core_1_0_patches) == 3
    assert {p.build for p in core_1_0_patches} == {"1.0.16", "1.0.15", "1.0.14"}
    oldest = min(p.release_date for p in core_1_0_patches)
    assert oldest == dt.date(2019, 2, 12)


async def test_products_and_patches_from_active_channel(mock_fetch):
    mock_fetch(_routes())
    result = await DotNetFetcher().fetch()

    products = {p.key: p for p in result.products}
    assert products["dotnet-9.0"].display_name == ".NET 9.0"
    assert products["dotnet-1.0"].display_name == ".NET Core 1.0"

    patches_9_0 = [p for p in result.patches if p.product_key == "dotnet-9.0"]
    assert len(patches_9_0) == 4
    latest = max(patches_9_0, key=lambda p: p.release_date)
    assert latest.build == "9.0.19"
    assert latest.release_date == dt.date(2026, 8, 11)
    assert latest.update_type == "Security"
    assert latest.kb_number is None  # .NET has no KB numbers — see refresh_service normalization


async def test_channel_fetch_failure_falls_back_to_latest_release_only(mock_fetch):
    """The "3.1" channel's releases.json is unmapped (see _routes), so the
    real GET 404s. The fetcher must not crash the whole run — it should
    record an error and still produce one patch from the index entry's own
    latest-release/latest-release-date."""
    mock_fetch(_routes())
    result = await DotNetFetcher().fetch()

    assert any("3.1" in err for err in result.errors)
    patches_3_1 = [p for p in result.patches if p.product_key == "dotnet-3.1"]
    assert len(patches_3_1) == 1
    assert patches_3_1[0].build == "3.1.32"
    assert patches_3_1[0].release_date == dt.date(2022, 12, 13)


async def test_index_fetch_failure_returns_no_patches_but_no_crash(mock_fetch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    mock_fetch(handler)
    result = await DotNetFetcher().fetch()

    assert result.patches == []
    assert result.products == []
    assert any("releases index" in err for err in result.errors)


def test_index_fixture_is_well_formed():
    # Sanity check on the fixture itself, independent of the fetcher: guards
    # against a future edit to releases_index.json silently dropping the
    # entries the tests above rely on.
    data = load_fixture_json("dotnet", "releases_index.json")
    versions = {e["channel-version"] for e in data["releases-index"]}
    assert versions == {"9.0", "1.0", "3.1"}
