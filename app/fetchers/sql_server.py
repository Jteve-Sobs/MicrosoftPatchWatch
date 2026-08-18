"""Scrapes the Microsoft Learn "build versions" pages for SQL Server — one
page per major version (2016, 2017, 2019, 2022, 2025), each listing every
Cumulative Update (CU) and General Distribution Release (GDR) build for that
version in an HTML table, with build number, KB, and release date. Older
versions (2016, 2017) additionally carry separate tables per Service Pack
baseline and an "Azure Connect Pack" track; all of those feed into the same
product (one product per major version, not per SP/track) — a user browsing
patch history for "SQL Server 2016" expects one timeline, not one row per
internal Microsoft servicing detail.

Column headers vary slightly release to release ("SQL Server build version"
vs "SQL Server product version" vs, on 2016, plain "Product version") —
column detection below matches on keywords rather than an exact header
string so that drift doesn't silently break parsing.

No end-of-life data: unlike the Windows release-health pages, these carry no
support-end-date column or section, so SQL Server products never get a
support_end_date. Same situation as .NET Framework — see
windows_release_health.py's module docstring.
"""

from __future__ import annotations

import datetime as dt
import logging
import re

from bs4 import BeautifulSoup, Tag

from app.fetchers.base import BaseFetcher, FetchResult, PatchInfo, ProductInfo
from app.models import ProductFamily

logger = logging.getLogger("patchwatch.fetchers.sql_server")

# No index page lists these for us the way the dotnet/core fetcher gets one
# for free — Microsoft Learn's build-version pages are one static URL per
# major version, so (unlike windows_release_health.py) we do have to hardcode
# the list. New major versions need a one-line addition here.
VERSIONS = [2016, 2017, 2019, 2022, 2025]

KB_RE = re.compile(r"KB\s?(\d{4,7})", re.IGNORECASE)
LATEST_SUFFIX_RE = re.compile(r"\s*\(Latest\)\s*$", re.IGNORECASE)
DATE_FORMATS = ("%B %d, %Y", "%Y-%m-%d")


def _parse_date(text: str) -> dt.date | None:
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _classify_update_type(heading: str) -> str:
    """GDR (General Distribution Release) is Microsoft's security-only
    servicing track for SQL Server; everything else (CU, the SP-scoped CU
    tables on 2016/2017, the Azure Connect Pack track) is the general
    cumulative-update track."""
    return "Security" if "gdr" in heading.lower() else "Update"


class SqlServerFetcher(BaseFetcher):
    name = "sql-server"

    async def fetch(self) -> FetchResult:
        result = FetchResult()
        async with self.make_client() as client:
            for year in VERSIONS:
                try:
                    await self._fetch_version(client, year, result)
                except Exception as exc:  # noqa: BLE001 - one bad version must not kill the run
                    msg = f"sql-server: failed to process SQL Server {year}: {exc}"
                    logger.exception(msg)
                    result.errors.append(msg)
        return result

    async def _fetch_version(self, client, year: int, result: FetchResult) -> None:
        url = f"https://learn.microsoft.com/en-us/troubleshoot/sql/releases/sqlserver-{year}/build-versions"
        response = await client.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        content = soup.find(attrs={"role": "main"}) or soup.find(id="main") or soup

        product_key = f"sqlserver-{year}"
        display_name = f"SQL Server {year}"

        # Each build table is preceded by an <h2> naming its track ("...
        # Cumulative Update (CU) builds", "... General Distribution Release
        # (GDR) builds", "... Service Pack 2 (SP2) Cumulative Update (CU)
        # builds", ...) — that heading text is what _classify_update_type
        # reads, not anything in the table itself.
        found_tables = 0
        heading = ""
        for el in content.find_all(["h2", "table"]):
            if el.name == "h2":
                heading = el.get_text(" ", strip=True)
                continue
            if not self._is_build_table(el):
                continue
            found_tables += 1
            self._parse_table(el, heading, product_key, display_name, result)

        if found_tables == 0:
            result.errors.append(
                f"sql-server: no usable build tables found on {url} (page layout may have changed)"
            )
            return

        result.products.append(
            ProductInfo(
                key=product_key,
                display_name=display_name,
                family=ProductFamily.SQL_SERVER.value,
                source_url=url,
            )
        )

    @staticmethod
    def _is_build_table(table: Tag) -> bool:
        headers = " | ".join(c.get_text(" ", strip=True).lower() for c in table.find_all("th"))
        return "knowledge base" in headers and "release date" in headers

    def _parse_table(
        self, table: Tag, heading: str, product_key: str, display_name: str, result: FetchResult
    ) -> None:
        headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
        col_index = {name: i for i, name in enumerate(headers)}

        idx_kb = next((i for name, i in col_index.items() if "knowledge base" in name), None)
        idx_date = next((i for name, i in col_index.items() if "release date" in name), None)
        # The build-number column's header text differs by release ("SQL
        # Server build version" / "SQL Server product version" / on 2016,
        # plain "Product version") — "version" but not "file version" (the
        # sqlservr.exe/msmdsrv.exe columns) or "analysis services" picks it
        # out regardless of which wording this particular page uses.
        idx_build = next(
            (
                i
                for name, i in col_index.items()
                if "version" in name and "file version" not in name and "analysis services" not in name
            ),
            None,
        )
        if idx_kb is None or idx_date is None:
            return

        update_type = _classify_update_type(heading)

        body = table.find("tbody")
        rows = body.find_all("tr") if body else table.find_all("tr")[1:]
        # A header <tr> living inside <tbody> (no separate <thead>) shows up
        # on some Microsoft Learn pages — see the identical guard in
        # windows_release_health.py. A real data row always has a <td>.
        rows = [row for row in rows if row.find_all("td")]

        for row in rows:
            cells = row.find_all(["td", "th"])
            if idx_kb >= len(cells) or idx_date >= len(cells):
                continue

            release_date = _parse_date(cells[idx_date].get_text(" ", strip=True))

            kb_cell = cells[idx_kb]
            kb_link = kb_cell.find("a")
            kb_match = KB_RE.search(kb_cell.get_text(" ", strip=True)) or (
                KB_RE.search(kb_link.get_text(" ", strip=True)) if kb_link else None
            )
            kb_number = f"KB{kb_match.group(1)}" if kb_match else None
            kb_url = kb_link["href"] if kb_link and kb_link.has_attr("href") else (
                f"https://support.microsoft.com/help/{kb_match.group(1)}" if kb_match else None
            )

            build = (
                cells[idx_build].get_text(" ", strip=True)
                if idx_build is not None and idx_build < len(cells)
                else None
            )

            if not kb_number and not build:
                continue

            # e.g. "CU26 (Latest)" -> "CU26" — "(Latest)" is only ever true
            # for the row that happens to be newest *right now*; stripping it
            # keeps older history entries from carrying a stale claim once a
            # later CU actually is the latest (the next refresh re-scrapes
            # the current "(Latest)" row's title fresh anyway, but there's no
            # reason to have ever shown the wrong one in the meantime).
            name = LATEST_SUFFIX_RE.sub("", cells[0].get_text(" ", strip=True)) if cells else ""

            result.patches.append(
                PatchInfo(
                    product_key=product_key,
                    kb_number=kb_number,
                    build=build or None,
                    title=f"{display_name} {name}".strip() if name else None,
                    update_type=update_type,
                    release_date=release_date,
                    severity=None,
                    kb_url=kb_url,
                    source=self.name,
                )
            )
