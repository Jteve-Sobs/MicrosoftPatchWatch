"""Scrapes the Microsoft Learn "release health" pages for Windows 10, Windows 11
and Windows Server. These pages contain, per version, an "Update history" table
with exactly the columns we need: servicing option / update type, availability
date, build and KB article. That makes them a better primary source than the
MSRC security API, which only covers *security* updates and doesn't include
build numbers.

The scraper does not hardcode a version list (22H2, 24H2, 25H2, LTSC 2021, ...)
— it discovers whatever version headings + history tables Microsoft currently
publishes on the page. That means new versions (e.g. a future 26H1) show up
automatically without a code change, and Windows 10 LTSB/LTSC editions are
picked up as long as Microsoft documents them on the same page.

Caveat: this is HTML scraping of a page Microsoft can restructure at any time.
If a page's table/heading layout changes materially, this fetcher will quietly
find nothing for that page (it reports that via FetchResult.errors) rather
than crash the whole refresh.
"""

from __future__ import annotations

import datetime as dt
import logging
import re

from bs4 import BeautifulSoup, Tag

from app.fetchers.base import BaseFetcher, FetchResult, PatchInfo, ProductInfo
from app.models import ProductFamily

logger = logging.getLogger("patchwatch.fetchers.windows_release_health")

PAGES = [
    {
        "url": "https://learn.microsoft.com/en-us/windows/release-health/windows11-release-information",
        "family": ProductFamily.WINDOWS_CLIENT.value,
        "prefix": "win11",
        "os_label": "Windows 11",
    },
    {
        "url": "https://learn.microsoft.com/en-us/windows/release-health/release-information",
        "family": ProductFamily.WINDOWS_CLIENT.value,
        "prefix": "win10",
        "os_label": "Windows 10",
    },
    {
        "url": "https://learn.microsoft.com/en-us/windows/release-health/windows-server-release-info",
        "family": ProductFamily.WINDOWS_SERVER.value,
        "prefix": "winsrv",
        "os_label": "Windows Server",
    },
]

KB_RE = re.compile(r"KB\s?(\d{4,7})", re.IGNORECASE)
VERSION_H_RE = re.compile(r"\b(\d{2}H\d)\b")  # 22H2, 24H2, 25H2, 26H1...
VERSION_OLD_RE = re.compile(r"\bVersion\s+(\d{3,4})\b", re.IGNORECASE)  # 1809, 1607, 1507...
LTSC_YEAR_RE = re.compile(r"\bLTSC\s+(\d{4})\b", re.IGNORECASE)
LTSB_YEAR_RE = re.compile(r"\b(\d{4})\s+LTSB\b", re.IGNORECASE)
SERVER_YEAR_RE = re.compile(r"\bServer\s+(\d{4})\b", re.IGNORECASE)
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y")


def _slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _parse_date(text: str) -> dt.date | None:
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _classify_update_type(raw: str) -> str:
    raw = raw.strip()
    upper = raw.upper()
    if "OOB" in upper:
        return "Out-of-Band"
    if "PREVIEW" in upper:
        return "Preview"
    # Microsoft's convention: the mid-month ("B") release is the security
    # release; later-week releases ("C"/"D") are non-security previews.
    if re.search(r"\bB\b", upper):
        return "Security"
    if re.search(r"\b[CD]\b", upper):
        return "Preview"
    return raw or "Update"


def _version_label(heading: str, os_label: str) -> tuple[str, bool]:
    """Best-effort short label + LTSC flag extracted from a heading like
    'Version 22H2 (OS build 19045)', 'Version 1809 (OS build 17763)' or
    'Windows Server 2025 (OS build 26100)'."""
    is_ltsc = bool(re.search(r"LTSC|LTSB", heading, re.IGNORECASE))

    m = LTSC_YEAR_RE.search(heading)
    if m:
        return f"LTSC {m.group(1)}", True
    m = LTSB_YEAR_RE.search(heading)
    if m:
        return f"LTSB {m.group(1)}", True
    m = VERSION_H_RE.search(heading)
    if m:
        return m.group(1), is_ltsc
    m = VERSION_OLD_RE.search(heading)
    if m:
        return m.group(1), is_ltsc
    m = SERVER_YEAR_RE.search(heading)
    if m:
        return m.group(1), is_ltsc

    cleaned = heading.split("(")[0].strip()
    if os_label.lower() in cleaned.lower():
        cleaned = re.sub(re.escape(os_label), "", cleaned, flags=re.IGNORECASE).strip(" ,")
    return cleaned or heading, is_ltsc


def _display_name(os_label: str, family: str, version_label: str) -> str:
    if re.match(r"LTSC|LTSB", version_label, re.IGNORECASE):
        return f"{os_label} Enterprise {version_label}"
    if family == ProductFamily.WINDOWS_SERVER.value:
        return f"{os_label} {version_label}".strip()
    return f"{os_label}, version {version_label}".strip()


class WindowsReleaseHealthFetcher(BaseFetcher):
    name = "windows-release-health"

    async def fetch(self) -> FetchResult:
        result = FetchResult()
        async with self.make_client() as client:
            for page in PAGES:
                try:
                    await self._fetch_page(client, page, result)
                except Exception as exc:  # noqa: BLE001
                    msg = f"windows-release-health: failed to process {page['url']}: {exc}"
                    logger.exception(msg)
                    result.errors.append(msg)
        return result

    async def _fetch_page(self, client, page: dict, result: FetchResult) -> None:
        response = await client.get(page["url"])
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")

        content = soup.find(attrs={"role": "main"}) or soup.find(id="main") or soup

        # The page has several h2 sections (servicing-option summary, release
        # history, hotpatch calendar, ...). We only want "release/update
        # history": it's the one with a table per version, each carrying
        # build + KB + date. The current version's table sits directly under
        # a <strong>Version X</strong> marker; older versions are collapsed
        # into <details><summary>Version X</summary><table>...</table></details>.
        # Both patterns were confirmed by inspecting the live page structure.
        section_heading = None
        for h2 in content.find_all("h2"):
            if re.search(r"release history|update history", h2.get_text(strip=True), re.IGNORECASE):
                section_heading = h2
                break

        if section_heading is None:
            result.errors.append(
                f"windows-release-health: no 'release history' section found on {page['url']} "
                "(page layout may have changed)"
            )
            return

        found_tables = 0
        pending_label: str | None = None
        node = section_heading.find_next_sibling()

        while node is not None and node.name != "h2":
            if node.name == "strong":
                text = node.get_text(" ", strip=True)
                if text:
                    pending_label = text
            elif node.name == "table":
                if self._is_history_table(node):
                    found_tables += 1
                    self._parse_history_table(node, pending_label or page["os_label"], page, result)
                pending_label = None
            elif node.name == "details":
                summary = node.find("summary")
                label = summary.get_text(" ", strip=True) if summary else node.get_text(" ", strip=True)[:80]
                table = node.find("table")
                if table is not None and self._is_history_table(table):
                    found_tables += 1
                    self._parse_history_table(table, label, page, result)
            node = node.find_next_sibling()

        if found_tables == 0:
            result.errors.append(
                f"windows-release-health: 'release history' section on {page['url']} had no "
                "usable tables (page layout may have changed)"
            )

    @staticmethod
    def _is_history_table(table: Tag) -> bool:
        header_cells = table.find_all("th")
        headers = " | ".join(c.get_text(" ", strip=True).lower() for c in header_cells)
        return "kb article" in headers and "build" in headers

    def _parse_history_table(self, table: Tag, heading: str, page: dict, result: FetchResult) -> None:
        header_cells = [c.get_text(" ", strip=True).lower() for c in table.find_all("th")]
        col_index = {name: i for i, name in enumerate(header_cells)}

        idx_servicing = col_index.get("servicing option")
        idx_type = col_index.get("update type", idx_servicing)
        idx_date = col_index.get("availability date")
        idx_build = col_index.get("build")
        idx_kb = col_index.get("kb article")

        if idx_date is None or idx_kb is None:
            return

        version_label, is_ltsc = _version_label(heading, page["os_label"])
        product_key = f"{page['prefix']}-{_slugify(version_label)}"
        display_name = _display_name(page["os_label"], page["family"], version_label)

        body_rows = table.find("tbody")
        rows = body_rows.find_all("tr") if body_rows else table.find_all("tr")[1:]

        # The heading text itself rarely says "LTSC" (e.g. "Version 21H2"), but
        # the per-row "Servicing option" column does (e.g. "LTSC", "LTSB").
        # Scan once up front so the product-level badge reflects reality.
        if idx_servicing is not None:
            for row in rows:
                cells = row.find_all(["td", "th"])
                if idx_servicing < len(cells) and re.search(
                    r"LTSC|LTSB", cells[idx_servicing].get_text(" ", strip=True), re.IGNORECASE
                ):
                    is_ltsc = True
                    break

        result.products.append(
            ProductInfo(
                key=product_key,
                display_name=display_name,
                family=page["family"],
                is_ltsc=is_ltsc,
                source_url=page["url"],
            )
        )

        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) <= max(filter(None, [idx_type, idx_date, idx_build, idx_kb])):
                continue

            date_text = cells[idx_date].get_text(" ", strip=True)
            release_date = _parse_date(date_text)

            kb_cell = cells[idx_kb]
            kb_link = kb_cell.find("a")
            kb_text = kb_cell.get_text(" ", strip=True)
            kb_match = KB_RE.search(kb_text) or KB_RE.search(kb_link.get_text(" ", strip=True) if kb_link else "")
            kb_number = f"KB{kb_match.group(1)}" if kb_match else None
            kb_url = kb_link["href"] if kb_link and kb_link.has_attr("href") else (
                f"https://support.microsoft.com/help/{kb_match.group(1)}" if kb_match else None
            )

            build = cells[idx_build].get_text(" ", strip=True) if idx_build is not None else None
            update_type_raw = cells[idx_type].get_text(" ", strip=True) if idx_type is not None else ""

            if not kb_number and not build:
                continue

            result.patches.append(
                PatchInfo(
                    product_key=product_key,
                    kb_number=kb_number,
                    build=build or None,
                    title=f"{display_name} – {update_type_raw}".strip(" –"),
                    update_type=_classify_update_type(update_type_raw),
                    release_date=release_date,
                    severity=None,
                    kb_url=kb_url,
                    source=self.name,
                )
            )
