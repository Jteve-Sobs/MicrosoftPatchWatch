"""Tests for app.fetchers.sql_server against frozen real page snippets — see
tests/fixtures/README.md for exactly what's in them and why."""

from __future__ import annotations

import datetime as dt

from app.fetchers.sql_server import SqlServerFetcher
from tests.conftest import load_fixture

URL_2022 = "https://learn.microsoft.com/en-us/troubleshoot/sql/releases/sqlserver-2022/build-versions"
URL_2016 = "https://learn.microsoft.com/en-us/troubleshoot/sql/releases/sqlserver-2016/build-versions"


async def test_modern_two_table_page_yields_one_product_with_both_tracks(mock_fetch):
    """2019/2022/2025 all share this shape: exactly two tables (CU, GDR),
    seven columns including separate Analysis Services build/file-version
    columns the fetcher must skip over to find the right "build" column."""
    mock_fetch({URL_2022: (200, load_fixture("sql_server", "sqlserver_2022.html"))})
    result = await SqlServerFetcher().fetch()

    products = {p.key: p for p in result.products}
    assert list(products) == ["sqlserver-2022"]
    assert products["sqlserver-2022"].display_name == "SQL Server 2022"
    assert products["sqlserver-2022"].family == "sql_server"
    # No end-of-life section on these pages at all (unlike Windows) — see the
    # module docstring.
    assert products["sqlserver-2022"].support_end_date is None

    patches = [p for p in result.patches if p.product_key == "sqlserver-2022"]
    assert len(patches) == 5  # 3 CU rows + 2 GDR rows

    by_kb = {p.kb_number: p for p in patches}
    assert by_kb["KB5093420"].update_type == "Update"
    assert by_kb["KB5093420"].build == "16.0.4265.3"
    # "(Latest)" is a snapshot-in-time claim, not history — stripped from the
    # stored title so an old row doesn't keep asserting it forever.
    assert by_kb["KB5093420"].title == "SQL Server 2022 CU26"
    assert "(Latest)" not in by_kb["KB5093420"].title

    assert by_kb["KB5101347"].update_type == "Security"  # from the GDR table
    assert by_kb["KB5101347"].title == "SQL Server 2022 CU25 + GDR"


async def test_kb_link_href_used_verbatim_absolute_or_relative(mock_fetch):
    """Real quirk: some KB links are absolute (support.microsoft.com), others
    are relative-path anchors to a sibling doc page on the same site. Both
    are still a legitimate "more info" link — kb_url should be whatever the
    page actually links to, not a reconstructed URL, for either case."""
    mock_fetch({URL_2022: (200, load_fixture("sql_server", "sqlserver_2022.html"))})
    result = await SqlServerFetcher().fetch()

    by_kb = {p.kb_number: p for p in result.patches}
    assert by_kb["KB5093420"].kb_url == "https://support.microsoft.com/help/5093420"
    assert by_kb["KB5081477"].kb_url == "cumulativeupdate25"


async def test_older_service_pack_tables_aggregate_into_one_product(mock_fetch):
    """2016/2017 additionally split CU history per Service Pack baseline
    across several tables, and use a 4-column layout (just "Product version",
    no separate Analysis Services columns) — both tables must still land on
    the *same* product, not one product per SP."""
    mock_fetch({URL_2016: (200, load_fixture("sql_server", "sqlserver_2016.html"))})
    result = await SqlServerFetcher().fetch()

    products = {p.key: p for p in result.products}
    assert list(products) == ["sqlserver-2016"]

    patches = [p for p in result.patches if p.product_key == "sqlserver-2016"]
    assert len(patches) == 3  # 2 rows from the SP3 table + 1 from the SP1 table

    by_kb = {p.kb_number: p for p in patches}
    assert by_kb["KB5102340"].build == "13.0.6500.1"
    assert by_kb["KB5102340"].release_date == dt.date(2026, 7, 14)
    assert by_kb["KB5021129"].title == "SQL Server 2016 CU18"  # "(Latest)" stripped here too


async def test_missing_page_is_reported_as_error_not_a_crash(mock_fetch):
    """None of the 5 version URLs are mocked here, so mock_fetch's default
    (404 for anything unmapped) kicks in for every one of them — the fetcher
    must survive that and just report it, per BaseFetcher's contract."""
    mock_fetch({})
    result = await SqlServerFetcher().fetch()

    assert result.products == []
    assert result.patches == []
    assert len(result.errors) == 5
