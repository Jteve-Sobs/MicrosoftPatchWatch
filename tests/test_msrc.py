"""Tests for app.fetchers.msrc against a frozen real MSRC CVRF document —
see tests/fixtures/README.md for exactly what's in it and why."""

from __future__ import annotations

import datetime as dt

from app.fetchers.msrc import CVRF_URL_TEMPLATE, KB_HELP_URL_TEMPLATE, UPDATES_URL, MsrcDotNetFrameworkFetcher
from tests.conftest import load_fixture


def _routes():
    return {
        UPDATES_URL: (200, load_fixture("msrc", "updates.json")),
        CVRF_URL_TEMPLATE.format(update_id="2026-Aug"): (200, load_fixture("msrc", "cvrf_2026_aug.json")),
        # The other 5 months MONTHS_TO_SCAN pulls in (Jul..Mar) are
        # deliberately unmapped — there's no fixture for them, so the mock
        # 404s. That's the point: it proves one bad/missing month doesn't
        # take down the whole fetch (see test below).
        #
        # Real support.microsoft.com articles for the fixture's two granular
        # Framework KBs (see _discover_os_bundles) — one has a per-OS
        # "combined" KB cross-referenced (5120703 -> 5121645), one doesn't
        # (5120702, an older-OS KB; Microsoft doesn't publish a combined
        # variant for those) — see kb_pages/README in fixtures/README.md.
        KB_HELP_URL_TEMPLATE.format(kb="5120702"): (200, load_fixture("msrc", "kb_pages", "kb5120702.html")),
        KB_HELP_URL_TEMPLATE.format(kb="5120703"): (200, load_fixture("msrc", "kb_pages", "kb5120703.html")),
    }


async def test_parses_products_and_deduplicates_patches(mock_fetch):
    mock_fetch(_routes())
    result = await MsrcDotNetFrameworkFetcher().fetch()

    # 9 real ProductIDs in the fixture collapse to 2 distinct .NET Framework
    # versions ("3.5 AND 4.8" on several products, plain "4.8" on others) —
    # exercises _split_versions and the known_versions de-dup. Plus the one
    # .NET (Core) 8.0 product the fixture also carries, plus the one OS-level
    # "combined KB" product _discover_os_bundles finds via KB5120703's own
    # article (KB5120702's article has no combined KB — see _routes).
    product_keys = {p.key for p in result.products}
    assert product_keys == {
        "dotnetfx-4.8",
        "dotnetfx-3.5",
        "dotnet-8.0",
        "dotnetfx-os-windows-10-version-1809-and-windows-server-2019",
    }

    # Fixture has 4 vulnerabilities: two (different CVEs) share KB5120702 on
    # 4.8 — the second must be dropped by seen_in_month — one (KB5120703)
    # covers both 3.5 and 4.8, producing one patch per version, and one
    # (CVE-2026-62902, KB5122104) is a .NET 8.0 fix — that one must NOT show
    # up here (see patch_kb_hints below): unlike Framework, a .NET (Core) row
    # from this source would have no build number to match dotnet.py's real
    # row on, so it'd just be a second, incomplete row for the same release.
    kbs_by_product = sorted((p.product_key, p.kb_number) for p in result.patches)
    assert kbs_by_product == [
        ("dotnetfx-3.5", "KB5120703"),
        ("dotnetfx-4.8", "KB5120702"),
        ("dotnetfx-4.8", "KB5120703"),
        ("dotnetfx-os-windows-10-version-1809-and-windows-server-2019", "KB5121645"),
    ]

    for patch in result.patches:
        assert patch.release_date == dt.date(2026, 8, 11)  # from the fixture's InitialReleaseDate
        assert patch.update_type == "Security"
        assert patch.build is None  # MSRC has no build numbers — see refresh_service normalization
    for patch in result.patches:
        if patch.product_key.startswith("dotnetfx-os-"):
            continue  # its title is the bundle's own description, not the month's DocumentTitle
        assert patch.title == "August 2026 Security Updates"

    # The .NET 8.0 KB instead lands as a hint, keyed by (product_key, month) —
    # refresh_service matches that against dotnet.py's build-only row.
    assert result.patch_kb_hints == {("dotnet-8.0", "2026-08"): ("KB5122104", "https://dotnet.microsoft.com/download/dotnet/8.0")}


async def test_one_missing_month_does_not_break_the_others(mock_fetch):
    mock_fetch(_routes())
    result = await MsrcDotNetFrameworkFetcher().fetch()

    # MONTHS_TO_SCAN=6 pulls in Aug..Mar; only Aug has a fixture, so the
    # other 5 should each land as a logged error, not an exception.
    assert len(result.errors) == 5
    assert all("2026-" in err for err in result.errors)
    # ...and August's real data still came through despite those failures
    # (3 dotnetfx patches + the 1 OS-bundle patch _discover_os_bundles adds).
    assert len(result.patches) == 4


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
    assert len(result.patches) == 4  # August's data still found despite the reversed order


def test_parse_release_date_prefers_initial_release_date_over_update_id():
    """Regression guard: update_id ("2026-Aug") only carries year+month —
    using it directly would flatten every patch that month to the 1st, which
    is what shipped originally. InitialReleaseDate has the real day."""
    fetcher = MsrcDotNetFrameworkFetcher()
    assert fetcher._parse_release_date({"ID": "2026-Aug", "InitialReleaseDate": "2026-08-11T07:00:00Z"}) == dt.date(
        2026, 8, 11
    )


def test_parse_release_date_falls_back_to_update_id_when_missing():
    fetcher = MsrcDotNetFrameworkFetcher()
    assert fetcher._parse_release_date({"ID": "2026-Aug"}) == dt.date(2026, 8, 1)
    assert fetcher._parse_release_date({"ID": "2026-Aug", "InitialReleaseDate": "garbage"}) == dt.date(2026, 8, 1)


async def test_os_bundle_kb_page_fetch_failure_is_non_fatal(mock_fetch):
    """A granular KB's own article page is a best-effort second hop (see
    _discover_os_bundles) — if it 404s or the site is down, that must not
    show up in result.errors (which would wrongly flag the whole msrc source
    as broken) and must not stop the real per-version patches from coming
    through."""
    routes = _routes()
    del routes[KB_HELP_URL_TEMPLATE.format(kb="5120703")]  # now unmapped -> 404
    mock_fetch(routes)

    result = await MsrcDotNetFrameworkFetcher().fetch()

    # The 5 unrelated "unmapped month" errors _routes() already causes (see
    # its docstring) are still expected — none of them is about the KB page.
    assert len(result.errors) == 5
    assert not any(p.product_key.startswith("dotnetfx-os-") for p in result.patches)
    # The real dotnetfx-* patches are unaffected.
    assert len(result.patches) == 3


async def test_os_bundle_kb_malformed_page_is_non_fatal(mock_fetch):
    routes = _routes()
    routes[KB_HELP_URL_TEMPLATE.format(kb="5120703")] = (200, "<html><body>not the expected shape</body></html>")
    mock_fetch(routes)

    result = await MsrcDotNetFrameworkFetcher().fetch()

    assert len(result.errors) == 5  # same 5 unrelated unmapped-month errors, nothing new
    assert not any(p.product_key.startswith("dotnetfx-os-") for p in result.patches)


def test_parse_bundle_links_handles_multiple_entries():
    """KB5120701's real article (Windows 10 21H2+22H2 share one granular KB)
    lists two combined KBs in one "Additional information" section — both
    must come back, each with its own OS name."""
    html = """
    <h2 id="additional-information-about-this-update">Additional information about this update</h2>
    <p>The following articles contain additional information about this update as it relates to individual product versions.</p>
    <ul>
      <li><a href="../a">5121646</a> Description of the Cumulative Update for .NET Framework 3.5, 4.8 and 4.8.1 for Windows 10 Version 21H2 (KB5121646)</li>
      <li><a href="../b">5121647</a> Description of the Cumulative Update for .NET Framework 3.5, 4.8 and 4.8.1 for Windows 10 Version 22H2 (KB5121647)</li>
    </ul>
    """
    assert MsrcDotNetFrameworkFetcher._parse_bundle_links(html) == [
        ("5121646", "Windows 10 Version 21H2", "../a"),
        ("5121647", "Windows 10 Version 22H2", "../b"),
    ]


def test_parse_bundle_links_returns_empty_without_the_heading():
    assert MsrcDotNetFrameworkFetcher._parse_bundle_links("<html><body><p>nothing here</p></body></html>") == []


async def test_same_bundle_kb_from_two_granular_kbs_is_not_duplicated(mock_fetch):
    """The fixture's two granular KBs (5120702, 5120703) are both real
    fetches this run — if both happened to cross-reference the same bundle
    KB (plausible: several granular KBs can share one OS), it must still
    only produce one product/patch, not two."""
    shared_bundle_page = """
    <h2 id="additional-information-about-this-update">Additional information about this update</h2>
    <ul>
      <li><a href="../shared">5199999</a> Description of the Cumulative Update for .NET Framework 3.5 and 4.8 for Windows Server 2022 (KB5199999)</li>
    </ul>
    """
    routes = _routes()
    routes[KB_HELP_URL_TEMPLATE.format(kb="5120702")] = (200, shared_bundle_page)
    routes[KB_HELP_URL_TEMPLATE.format(kb="5120703")] = (200, shared_bundle_page)
    mock_fetch(routes)

    result = await MsrcDotNetFrameworkFetcher().fetch()

    bundle_patches = [p for p in result.patches if p.kb_number == "KB5199999"]
    assert len(bundle_patches) == 1
    assert len([p for p in result.products if p.key == "dotnetfx-os-windows-server-2022"]) == 1
