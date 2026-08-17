"""Scrapes the Microsoft Learn "release health" pages for Windows 10, Windows 11
and Windows Server. These pages contain, per version, an "Update history" table
with exactly the columns we need: servicing option / update type, availability
date, build and KB article. That makes them a better primary source than the
MSRC security API, which only covers *security* updates and doesn't include
build numbers.

The same pages also carry a "current versions by servicing option" summary
section further up, with end-of-life data: an "End of updates" column pair for
mainstream (SAC) versions, and an "Extended support end date" column for
LTSC/LTSB and Server versions. We parse that section too and attach the
resulting date to each matching product as `support_end_date` — this is the
same page we're already fetching, so it's free (no extra request, no separate
lifecycle-API dependency). .NET Framework has no equivalent page, so it simply
never gets a support_end_date; that's expected, not a bug.

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
PAREN_CODE_RE = re.compile(r"\((\d{3,4})\)")  # "2019 (1809)" -> 1809
ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y")


def _slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _parse_date(text: str) -> dt.date | None:
    text = text.strip()
    # Cells sometimes carry trailing notes, e.g. "2032-01-13 (IoT Enterprise
    # only)" — pull the ISO date out rather than requiring an exact match.
    m = ISO_DATE_RE.search(text)
    if m:
        try:
            return dt.date.fromisoformat(m.group(1))
        except ValueError:
            pass
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None  # e.g. "End of updates" — already ended, no date given


_TITLE_TYPE_LABEL = {
    "Security": "Security Update",
    "Preview": "Preview Update",
    "Out-of-Band": "Out-of-Band Update",
}


def _release_title(display_name: str, update_type: str, release_date: dt.date | None) -> str:
    """Human-readable title, e.g. 'Windows 10, version 1607 – August 2026
    Security Update'. Not just cosmetic: the source table's own "type" cell
    is Microsoft's internal release-train shorthand ("2026-08 B", "2026-04
    OOB" — "B" being their term for the mid-month/Patch-Tuesday release),
    which reads as gibberish out of context. That raw text still drives
    _classify_update_type() below; this only changes what ends up in the
    title users actually see."""
    label = _TITLE_TYPE_LABEL.get(update_type, "Update")
    if release_date:
        return f"{display_name} – {release_date.strftime('%B %Y')} {label}"
    return f"{display_name} – {label}"


def _classify_update_type(raw: str) -> str:
    raw = raw.strip()
    upper = raw.upper()
    if "OOB" in upper:
        return "Out-of-Band"
    if "PREVIEW" in upper:
        return "Preview"
    # Microsoft's convention: the mid-month ("B") release is the security
    # release; later-week releases ("C", "D", and occasionally further
    # letters — "E" shows up in older history — for extra out-of-cycle
    # releases that month) are non-security previews.
    if re.search(r"\bB\b", upper):
        return "Security"
    if re.search(r"\b[C-Z]\b", upper):
        return "Preview"
    # Anything else — e.g. "A" (seen on a new version's initial GA-release
    # month, not a regular monthly-cadence letter) or a blank cell — falls
    # back to a plain, honest label. Must NOT be `raw` here: that used to
    # echo the unrecognized shorthand straight through (e.g. "2016-08 E"
    # ending up as the literal update_type), which then failed every
    # `t('badge.' ~ ...)` lookup and rendered as the raw i18n key.
    return "Update"


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


def _extract_version_code(text: str) -> str:
    """Normalizes a version cell/heading to a short matching code, e.g.
    'Version 22H2 (OS build 19045)' -> '22H2', '2019 (1809)' -> '1809',
    'Windows Server 2019 (version 1809)' -> '2019', '24H2 1' (footnote) -> '24H2'.
    Used to line up rows between the summary table (end-of-life dates) and the
    release-history table (KB/build) for the same version, even though
    Microsoft formats the version differently in each."""
    m = VERSION_H_RE.search(text)
    if m:
        return m.group(1)
    m = SERVER_YEAR_RE.search(text)
    if m:
        return m.group(1)
    m = PAREN_CODE_RE.search(text)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{3,4})\b", text)
    if m:
        return m.group(1)
    return text.strip()


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

        # Different h2 sections on the page nest their content at different
        # DOM depths (the "current versions" summary lives in its own
        # sub-tree, "release history" is a flat run of siblings) — sibling
        # walking only works for the latter. find_all() sidesteps that
        # entirely: it returns every match in document order regardless of
        # nesting, so a single pass with a small "which section am I in"
        # state machine handles both reliably.
        #
        # In "history": <strong>Version X</strong> or <details><summary>
        # Version X</summary> mark a version, immediately followed by its
        # table. In "summary": each table has its own "Version" column
        # (one row per version) plus the end-of-life columns we want.
        section: str | None = None
        pending_label: str | None = None
        # code -> (end_date, ended). end_date is set when the source gives an
        # actual date; ended=True with end_date=None means the source says
        # support already ended (literally "End of updates") but without
        # repeating the exact date on this page.
        eol_by_code: dict[str, tuple[dt.date | None, bool]] = {}
        found_tables = 0

        for el in content.find_all(["h2", "h4", "strong", "summary", "table"]):
            if el.name == "h2":
                text = el.get_text(strip=True).lower()
                if "current versions" in text or "major versions" in text:
                    section = "summary"
                elif "release history" in text or "update history" in text:
                    section = "history"
                else:
                    section = None
                pending_label = None
                continue

            if section is None or el.name == "h4":
                continue

            if el.name in ("strong", "summary"):
                text = el.get_text(" ", strip=True)
                if text:
                    pending_label = text
                continue

            if el.name != "table":
                continue

            if section == "summary":
                self._parse_summary_table(el, eol_by_code)
            elif section == "history" and self._is_history_table(el):
                found_tables += 1
                self._parse_history_table(el, pending_label or page["os_label"], page, result, eol_by_code)
            pending_label = None

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

    @staticmethod
    def _parse_summary_table(table: Tag, into: dict[str, tuple[dt.date | None, bool]]) -> None:
        headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
        col_index = {name: i for i, name in enumerate(headers)}

        idx_version = col_index.get("version")
        if idx_version is None:
            idx_version = col_index.get("windows server version")
        if idx_version is None:
            return

        # LTSC/LTSB and Server tables have a definitive "Extended support end
        # date" (true end of security updates). SAC/GA tables instead have two
        # "End of updates: <editions>" columns; we take the later of the two,
        # i.e. the last date *any* edition still gets updates.
        idx_extended = col_index.get("extended support end date")
        sac_cols = [i for name, i in col_index.items() if name.startswith("end of updates")]
        date_cols = [idx_extended] if idx_extended is not None else sac_cols

        body = table.find("tbody")
        rows = body.find_all("tr") if body else table.find_all("tr")[1:]
        # See the matching guard in _parse_history_table: a header <tr> can
        # end up inside <tbody> on some pages, so drop any row that's all
        # <th> (real data rows always have a <td>).
        rows = [row for row in rows if row.find_all("td")]

        for row in rows:
            cells = row.find_all(["td", "th"])
            if idx_version >= len(cells):
                continue
            code = _extract_version_code(cells[idx_version].get_text(" ", strip=True))
            if not code:
                continue

            raw_texts = [cells[i].get_text(" ", strip=True) for i in date_cols if i < len(cells)]
            parsed_dates = [d for text in raw_texts for d in [_parse_date(text)] if d is not None]

            if parsed_dates:
                # A later table for the same code (e.g. the LTSC table, which
                # is parsed after the plain SAC table) intentionally wins —
                # it's the more specific/correct answer for that edition.
                into[code] = (max(parsed_dates), False)
            elif any(raw_texts):
                # No column parsed as a date, but the cell isn't empty either
                # — that's Microsoft's "End of updates" wording: support has
                # already ended, the page just doesn't repeat the exact date.
                into[code] = (None, True)

    def _parse_history_table(
        self,
        table: Tag,
        heading: str,
        page: dict,
        result: FetchResult,
        eol_by_code: dict[str, tuple[dt.date | None, bool]],
    ) -> None:
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
        # Some pages put the header <tr> inside <tbody> instead of a separate
        # <thead>, so it survives the split above and gets read as a data row
        # (cells = ["Build", "Update type", ...] — "Build" is truthy, so it
        # even passed the "not kb_number and not build" skip below). A real
        # data row always has at least one <td>; an all-<th> row never does.
        rows = [row for row in rows if row.find_all("td")]

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

        end_date, ended = eol_by_code.get(_extract_version_code(heading), (None, False))
        result.products.append(
            ProductInfo(
                key=product_key,
                display_name=display_name,
                family=page["family"],
                is_ltsc=is_ltsc,
                source_url=page["url"],
                support_end_date=end_date,
                support_ended=ended,
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

            update_type = _classify_update_type(update_type_raw)
            result.patches.append(
                PatchInfo(
                    product_key=product_key,
                    kb_number=kb_number,
                    build=build or None,
                    title=_release_title(display_name, update_type, release_date),
                    update_type=update_type,
                    release_date=release_date,
                    severity=None,
                    kb_url=kb_url,
                    source=self.name,
                )
            )
