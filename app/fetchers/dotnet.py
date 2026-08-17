"""Fetches modern .NET (Core, 5+) release history from the official
dotnet/core releases-index.json on GitHub. This is a clean, maintained JSON
source (no scraping needed).

Not to be confused with fetchers/msrc.py, which covers *.NET Framework*
(3.5 / 4.x) — a different product line with no equivalent JSON feed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from app.fetchers.base import BaseFetcher, FetchResult, PatchInfo, ProductInfo
from app.models import ProductFamily

logger = logging.getLogger("patchwatch.fetchers.dotnet")

INDEX_URL = "https://raw.githubusercontent.com/dotnet/core/main/release-notes/releases-index.json"
FULL_HISTORY_PHASES = {"active", "maintenance", "preview", "go-live"}
MAX_CONCURRENT_REQUESTS = 5


def _parse_date(text: str | None) -> dt.date | None:
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


class DotNetFetcher(BaseFetcher):
    name = "dotnet-core"

    async def fetch(self) -> FetchResult:
        result = FetchResult()
        async with self.make_client() as client:
            try:
                response = await client.get(INDEX_URL, headers={"Accept": "application/json"})
                response.raise_for_status()
                index = response.json().get("releases-index", [])
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"dotnet-core: failed to fetch releases index: {exc}")
                return result

            semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
            await asyncio.gather(
                *(self._handle_channel(client, entry, result, semaphore) for entry in index)
            )
        return result

    async def _handle_channel(self, client, entry: dict, result: FetchResult, semaphore) -> None:
        version = entry.get("channel-version")
        if not version:
            return
        product = entry.get("product", ".NET")
        product_key = f"dotnet-{version}"
        display_name = f"{product} {version}"

        result.products.append(
            ProductInfo(
                key=product_key,
                display_name=display_name,
                family=ProductFamily.DOTNET.value,
                is_ltsc=False,
                source_url="https://github.com/dotnet/core/blob/main/release-notes/releases-index.json",
            )
        )

        phase = entry.get("support-phase")
        releases_url = entry.get("releases.json")

        if phase in FULL_HISTORY_PHASES and releases_url:
            try:
                async with semaphore:
                    resp = await client.get(releases_url, headers={"Accept": "application/json"})
                    resp.raise_for_status()
                    releases = resp.json().get("releases", [])
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"dotnet-core: failed to fetch releases for {version}: {exc}")
                releases = []

            for rel in releases:
                rel_version = rel.get("release-version")
                if not rel_version:
                    continue
                result.patches.append(
                    PatchInfo(
                        product_key=product_key,
                        kb_number=None,
                        build=rel_version,
                        title=f"{display_name} – {rel_version}",
                        update_type="Security" if rel.get("security") else "Update",
                        release_date=_parse_date(rel.get("release-date")),
                        severity=None,
                        kb_url=rel.get("release-notes") or releases_url,
                        source=self.name,
                    )
                )
            if releases:
                return

        # EOL / preview channels (or a failed releases.json fetch): fall back to
        # just the single latest release from the index itself.
        latest = entry.get("latest-release")
        if latest:
            result.patches.append(
                PatchInfo(
                    product_key=product_key,
                    kb_number=None,
                    build=latest,
                    title=f"{display_name} – {latest}",
                    update_type="Security" if entry.get("security") else "Update",
                    release_date=_parse_date(entry.get("latest-release-date")),
                    severity=None,
                    kb_url=releases_url,
                    source=self.name,
                )
            )
