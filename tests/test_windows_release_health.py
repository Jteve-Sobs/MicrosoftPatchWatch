"""Tests for app.fetchers.windows_release_health against a frozen real page
snippet — see tests/fixtures/README.md for exactly what's in it and why."""

from __future__ import annotations

import datetime as dt

from bs4 import BeautifulSoup

from app.fetchers.windows_release_health import PAGES, WindowsReleaseHealthFetcher, _classify_update_type
from tests.conftest import load_fixture

# Reuse the real page configs but point them at fixtures — the parsing code
# doesn't care which of the 3 PAGES entries it came from, so one fixture per
# *shape* of quirk is enough (windows10/11 share the exact same
# _fetch_page/_parse_history_table/_parse_summary_table as windows_server).
SERVER_PAGE = next(p for p in PAGES if p["prefix"] == "winsrv")
WIN11_PAGE = next(p for p in PAGES if p["prefix"] == "win11")


def _routes():
    html = load_fixture("windows_release_health", "windows_server.html")
    return {SERVER_PAGE["url"]: (200, html)}


def _win11_routes():
    html = load_fixture("windows_release_health", "windows11.html")
    return {WIN11_PAGE["url"]: (200, html)}


async def test_header_row_inside_tbody_is_not_parsed_as_a_patch(mock_fetch):
    """Regression test: on this real page, the header <tr> lives inside
    <tbody> (no <thead>) for both the summary and history tables. Before the
    "not row.find_all('td')" guard, that leaked through as a fake patch with
    build="Build", update_type="Update type", no date."""
    mock_fetch(_routes())
    result = await WindowsReleaseHealthFetcher().fetch()

    assert not any(p.build == "Build" for p in result.patches)
    assert not any(p.release_date is None for p in result.patches)
    patches_2022 = [p for p in result.patches if p.product_key == "winsrv-2022"]
    patches_2016 = [p for p in result.patches if p.product_key == "winsrv-2016"]
    assert len(patches_2022) == 5  # 6 real <tr> in the fixture minus the leaked header row
    assert len(patches_2016) == 5


async def test_products_get_ltsc_flag_and_eol_date_from_summary_table(mock_fetch):
    mock_fetch(_routes())
    result = await WindowsReleaseHealthFetcher().fetch()

    products = {p.key: p for p in result.products}
    server_2022 = products["winsrv-2022"]
    assert server_2022.display_name == "Windows Server 2022"
    # Heading text alone doesn't say "LTSC" ("... (OS build 20348)") — this
    # only works if the per-row "Servicing option" column scan runs.
    assert server_2022.is_ltsc is True
    assert server_2022.support_end_date == dt.date(2031, 10, 14)

    server_2016 = products["winsrv-2016"]
    assert server_2016.is_ltsc is True  # LTSB, detected the same way
    assert server_2016.support_end_date == dt.date(2027, 1, 12)


async def test_update_type_classification_and_human_readable_titles(mock_fetch):
    """Regression test for the raw-shorthand-as-title bug: the source column
    literally says "2026-08 B" / "2026-04 OOB" (Microsoft's internal
    release-train codes) — that must drive update_type, not end up verbatim
    in the user-facing title."""
    mock_fetch(_routes())
    result = await WindowsReleaseHealthFetcher().fetch()

    by_kb = {p.kb_number: p for p in result.patches}

    security = by_kb["KB5120242"]
    assert security.update_type == "Security"
    # Not "Windows Server 2022 – 2026-08 B" — the old, literal-shorthand title.
    assert security.title == "Windows Server 2022 – August 2026 Security Update"

    oob = by_kb["KB5091575"]
    assert oob.update_type == "Out-of-Band"
    assert oob.title == "Windows Server 2022 – April 2026 Out-of-Band Update"
    assert oob.build == "20348.5024"
    assert oob.release_date == dt.date(2026, 4, 19)


async def test_kb_link_with_non_standard_support_url_is_still_parsed(mock_fetch):
    """One real row in the fixture links to a support.microsoft.com/topic/...
    URL instead of the usual /help/<kb> shorthand — the KB number must still
    come from the link text, and kb_url must be the real link, unmodified."""
    mock_fetch(_routes())
    result = await WindowsReleaseHealthFetcher().fetch()

    patch = next(p for p in result.patches if p.kb_number == "KB5087545")
    assert patch.kb_url.startswith("https://support.microsoft.com/topic/")
    assert patch.build == "20348.5139"


# --- Windows 11 client: covers what the server fixture above can't ---------
# The Windows 10/11 summary table uses a *different* EOL schema than Server/
# LTSC (a pair of "End of updates: <editions>" columns instead of one
# "Extended support end date" column) — a separate code path
# (_parse_summary_table's `sac_cols` branch) that windows_server.html never
# exercises.


def test_sac_dual_column_end_of_updates_takes_the_later_date():
    """Direct unit test of _parse_summary_table's SAC branch (no fetch/mock
    needed — it's a pure static method): of the two "End of updates: ..."
    columns, the later date must win (support lasts until the last edition
    stops getting updates), and a column that only says "End of updates"
    (already ended, no date) must not prevent the other column's real date
    from being picked up."""
    html = load_fixture("windows_release_health", "windows11.html")
    table = BeautifulSoup(html, "lxml").find_all("table")[0]  # the SAC table, not the LTSC one

    eol: dict[str, tuple[dt.date | None, bool]] = {}
    WindowsReleaseHealthFetcher._parse_summary_table(table, eol)

    assert eol["26H1"] == (dt.date(2029, 3, 13), False)  # max(2028-03-14, 2029-03-13)
    assert eol["25H2"] == (dt.date(2028, 10, 10), False)
    # 23H2's "Home, Pro, ..." column just says "End of updates" (no date) —
    # the Enterprise column's real date must still come through.
    assert eol["23H2"] == (dt.date(2026, 11, 10), False)


async def test_ltsc_table_overrides_sac_date_for_same_version_code(mock_fetch):
    """Windows 11 24H2 is unusual: the *same* build train is both a regular
    GA-channel version and an LTSC edition, so it appears in both summary
    tables under the same code ("24H2"). Per the comment in
    _parse_history_table, whichever table is parsed later (LTSC, which
    follows the plain SAC table in document order) should win — it's the
    more specific, correct answer for that edition."""
    mock_fetch(_win11_routes())
    result = await WindowsReleaseHealthFetcher().fetch()

    product = next(p for p in result.products if p.key == "win11-24h2")
    assert product.support_end_date == dt.date(2034, 10, 10)  # LTSC's date, not the SAC table's 2027-10-12


async def test_version_footnote_and_combined_servicing_text(mock_fetch):
    """The LTSC summary row's version cell is "24H2<sup>1</sup>" (a real
    footnote marker) and the history rows' servicing-option cell is the
    combined text "LTSC • General Availability Channel" — both need to still
    resolve to plain "24H2" / is_ltsc=True rather than e.g. "win11-24h2-1"
    or missing the LTSC flag because the cell isn't an exact "LTSC" match."""
    mock_fetch(_win11_routes())
    result = await WindowsReleaseHealthFetcher().fetch()

    product = next(p for p in result.products if p.key == "win11-24h2")
    assert product.is_ltsc is True
    assert len([p for p in result.products if p.key.startswith("win11-24h2")]) == 1


async def test_preview_and_out_of_band_update_types_from_client_history(mock_fetch):
    """The server fixture only covers "B" (Security) and "OOB"; this table
    also has real "D" rows — Microsoft's non-security later-week preview
    release — which must classify as "Preview", not fall through to the
    generic "Update" default."""
    mock_fetch(_win11_routes())
    result = await WindowsReleaseHealthFetcher().fetch()

    by_kb = {p.kb_number: p for p in result.patches}
    assert by_kb["KB5101684"].update_type == "Preview"  # "2026-07 D"
    assert by_kb["KB5121767"].update_type == "Out-of-Band"  # "2026-07 OOB"
    assert by_kb["KB5121003"].update_type == "Security"  # "2026-08 B"


# --- _classify_update_type: real-world shorthand values ---------------------
# Regression tests for a bug found live: unrecognized shorthand (older
# history uses more letters than just B/C/D) fell through to `raw or
# "Update"`, which — since raw is never empty here — returned the raw
# shorthand ("2016-08 E") as the update_type. That value then failed every
# `t('badge.' ~ ...)` i18n lookup and rendered as the literal missing key
# ("badge.2016-08-e") instead of a label.


def test_classify_recognizes_letters_past_d():
    # Real value seen on a live Windows 10 1607 row (KB3176938, Aug 2016).
    assert _classify_update_type("2016-08 E") == "Preview"


def test_classify_unrecognized_shorthand_falls_back_to_a_clean_label():
    # "A" is real too (seen on new-version GA-release months, e.g. "2016-08 A"
    # for the 1607 Anniversary Update launch) — not a regular B/C/D/E-style
    # monthly-cadence letter, so it can't be classified as Security/Preview,
    # but it must never come back out as the raw, unlabeled shorthand either.
    assert _classify_update_type("2016-08 A") == "Update"
    assert _classify_update_type("") == "Update"
