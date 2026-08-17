"""Tests for app.fetchers.msrc against a frozen real MSRC CVRF document —
see tests/fixtures/README.md for exactly what's in it and why."""

from __future__ import annotations

import datetime as dt

from app.fetchers.msrc import CVRF_URL_TEMPLATE, UPDATES_URL, MsrcDotNetFrameworkFetcher
from tests.conftest import load_fixture


def _routes():
    return {
        UPDATES_URL: (200, load_fixture("msrc", "updates.json")),
        CVRF_URL_TEMPLATE.format(update_id="2026-Aug"): (200, load_fixture("msrc", "cvrf_2026_aug.json")),
        # The other 5 months MONTHS_TO_SCAN pulls in (Jul..Mar) are
        # deliberately unmapped — there's no fixture for them, so the mock
        # 404s. That's the point: it proves one bad/missing month doesn't
        # take down the whole fetch (see test below).
    }


async def test_parses_products_and_deduplicates_patches(mock_fetch):
    mock_fetch(_routes())
    result = await MsrcDotNetFrameworkFetcher().fetch()

    # 9 real ProductIDs in the fixture collapse to 2 distinct .NET Framework
    # versions ("3.5 AND 4.8" on several products, plain "4.8" on others) —
    # exercises _split_versions and the known_versions de-dup.
    product_keys = {p.key for p in result.products}
    assert product_keys == {"dotnetfx-4.8", "dotnetfx-3.5"}

    # Fixture has 3 vulnerabilities: two (different CVEs) share KB5120702 on
    # 4.8 — the second must be dropped by seen_in_month — and one (KB5120703)
    # covers both 3.5 and 4.8, producing one patch per version.
    kbs_by_product = sorted((p.product_key, p.kb_number) for p in result.patches)
    assert kbs_by_product == [
        ("dotnetfx-3.5", "KB5120703"),
        ("dotnetfx-4.8", "KB5120702"),
        ("dotnetfx-4.8", "KB5120703"),
    ]

    for patch in result.patches:
        assert patch.release_date == dt.date(2026, 8, 1)  # from the "2026-Aug" update_id
        assert patch.title == "August 2026 Security Updates"
        assert patch.update_type == "Security"
        assert patch.build is None  # MSRC has no build numbers — see refresh_service normalization


async def test_one_missing_month_does_not_break_the_others(mock_fetch):
    mock_fetch(_routes())
    result = await MsrcDotNetFrameworkFetcher().fetch()

    # MONTHS_TO_SCAN=6 pulls in Aug..Mar; only Aug has a fixture, so the
    # other 5 should each land as a logged error, not an exception.
    assert len(result.errors) == 5
    assert all("2026-" in err for err in result.errors)
    # ...and August's real data still came through despite those failures.
    assert len(result.patches) == 3


async def test_updates_list_is_scanned_newest_first(mock_fetch):
    """updates.json is unsorted in reality (verified against the live API) —
    the fetcher must sort by InitialReleaseDate itself, not trust list order.
    Regression guard: shuffle the fixture's order and confirm Aug is still
    the one that gets processed within MONTHS_TO_SCAN."""
    import json

    updates = json.loads(load_fixture("msrc", "updates.json"))
    updates["value"] = list(reversed(updates["value"]))  # oldest-first now

    routes = _routes()
    routes[UPDATES_URL] = (200, json.dumps(updates))
    mock_fetch(routes)

    result = await MsrcDotNetFrameworkFetcher().fetch()
    assert len(result.patches) == 3  # August's data still found despite the reversed order
