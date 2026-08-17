"""Fetches .NET Framework patch data from the Microsoft Security Response
Center (MSRC) CVRF API — the closest thing left to a public, machine-readable
feed since Microsoft retired the security bulletin RSS feed.

Why MSRC and not the release-health pages for .NET Framework: unlike Windows,
.NET Framework has no equivalent "release information" page with a clean
history table. MSRC's monthly CVRF documents list every security fix,
including its KB number and the exact .NET Framework version(s) it applies to,
via the document's ProductTree + per-vulnerability Remediations.

Known limitation: MSRC only covers *security* updates. Non-security .NET
Framework rollups are not captured here. Data quality also depends on
Microsoft's CVRF document consistency, which has been known to vary — this
fetcher is written defensively (per-item try/except) so a malformed entry is
skipped rather than aborting the whole run.
"""

from __future__ import annotations

import datetime as dt
import logging
import re

from app.fetchers.base import BaseFetcher, FetchResult, PatchInfo, ProductInfo
from app.models import ProductFamily

logger = logging.getLogger("patchwatch.fetchers.msrc")

UPDATES_URL = "https://api.msrc.microsoft.com/cvrf/v2.0/updates"
CVRF_URL_TEMPLATE = "https://api.msrc.microsoft.com/cvrf/v2.0/cvrf/{update_id}"
JSON_HEADERS = {"Accept": "application/json"}

# How many of the most recent monthly documents to walk. .NET Framework
# releases monthly, so a handful of months is enough to fill in recent history
# without hammering the API on every refresh.
MONTHS_TO_SCAN = 6

FRAMEWORK_VERSION_RE = re.compile(r"\.NET Framework ([0-9.]+(?:\s*(?:AND|,)\s*[0-9.]+)*)", re.IGNORECASE)
KB_DIGITS_RE = re.compile(r"(\d{6,7})")


def _split_versions(raw: str) -> list[str]:
    parts = re.split(r"\s*(?:AND|,)\s*", raw, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


class MsrcDotNetFrameworkFetcher(BaseFetcher):
    name = "msrc"

    async def fetch(self) -> FetchResult:
        result = FetchResult()
        async with self.make_client() as client:
            try:
                resp = await client.get(UPDATES_URL, headers=JSON_HEADERS)
                resp.raise_for_status()
                updates = resp.json()
                if isinstance(updates, dict):
                    updates = updates.get("value", [])
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"msrc: failed to list updates: {exc}")
                return result

            updates = sorted(updates, key=lambda u: u.get("InitialReleaseDate", ""), reverse=True)[:MONTHS_TO_SCAN]

            known_versions: set[str] = set()

            for update in updates:
                update_id = update.get("ID")
                if not update_id:
                    continue
                try:
                    await self._process_month(client, update_id, result, known_versions)
                except Exception as exc:  # noqa: BLE001
                    msg = f"msrc: failed to process {update_id}: {exc}"
                    logger.exception(msg)
                    result.errors.append(msg)
        return result

    async def _process_month(self, client, update_id: str, result: FetchResult, known_versions: set[str]) -> None:
        resp = await client.get(CVRF_URL_TEMPLATE.format(update_id=update_id), headers=JSON_HEADERS)
        resp.raise_for_status()
        doc = resp.json()

        product_names = self._collect_product_names(doc.get("ProductTree", {}))
        framework_products = self._framework_versions_by_product_id(product_names)
        if not framework_products:
            return

        for version in {v for versions in framework_products.values() for v in versions}:
            if version in known_versions:
                continue
            known_versions.add(version)
            product_key = f"dotnetfx-{version}"
            result.products.append(
                ProductInfo(
                    key=product_key,
                    display_name=f".NET Framework {version}",
                    family=ProductFamily.DOTNET_FRAMEWORK.value,
                    is_ltsc=False,
                    source_url="https://msrc.microsoft.com/update-guide",
                )
            )

        seen_in_month: set[tuple[str, str]] = set()

        for vuln in doc.get("Vulnerability", []) or []:
            title = (doc.get("DocumentTitle") or {}).get("Value") if isinstance(doc.get("DocumentTitle"), dict) else doc.get("DocumentTitle")
            for remediation in vuln.get("Remediations", []) or []:
                try:
                    self._handle_remediation(
                        remediation, framework_products, update_id, title, result, seen_in_month
                    )
                except Exception:  # noqa: BLE001
                    continue

    @staticmethod
    def _collect_product_names(node: dict) -> dict[str, str]:
        """Flatten the recursive ProductTree into {ProductID: FullProductName}."""
        names: dict[str, str] = {}

        def walk(n) -> None:
            if not isinstance(n, dict):
                return
            for fpn in n.get("FullProductName", []) or []:
                pid = str(fpn.get("ProductID"))
                value = fpn.get("Value")
                if pid and value:
                    names[pid] = value
            for branch in n.get("Branch", []) or []:
                walk(branch)

        walk(node)
        return names

    @staticmethod
    def _framework_versions_by_product_id(product_names: dict[str, str]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for pid, name in product_names.items():
            if ".net framework" not in name.lower():
                continue
            m = FRAMEWORK_VERSION_RE.search(name)
            if not m:
                continue
            result[pid] = _split_versions(m.group(1))
        return result

    def _handle_remediation(
        self,
        remediation: dict,
        framework_products: dict[str, list[str]],
        update_id: str,
        title: str | None,
        result: FetchResult,
        seen_in_month: set[tuple[str, str]],
    ) -> None:
        if remediation.get("Type") not in ("Vendor Fix", 2, "2"):
            return

        product_ids = [str(p) for p in remediation.get("ProductID", []) or []]
        versions: set[str] = set()
        for pid in product_ids:
            versions.update(framework_products.get(pid, []))
        if not versions:
            return

        description = remediation.get("Description")
        desc_value = description.get("Value") if isinstance(description, dict) else description
        kb_match = KB_DIGITS_RE.search(desc_value or "") or KB_DIGITS_RE.search(remediation.get("URL", "") or "")
        if not kb_match:
            return
        kb_number = f"KB{kb_match.group(1)}"
        kb_url = remediation.get("URL") or f"https://support.microsoft.com/help/{kb_match.group(1)}"

        release_date = self._parse_month(update_id)

        for version in versions:
            dedup_key = (version, kb_number)
            if dedup_key in seen_in_month:
                continue
            seen_in_month.add(dedup_key)
            result.patches.append(
                PatchInfo(
                    product_key=f"dotnetfx-{version}",
                    kb_number=kb_number,
                    build=None,
                    title=title or f".NET Framework {version} security update",
                    update_type="Security",
                    release_date=release_date,
                    severity=None,
                    kb_url=kb_url,
                    source=self.name,
                )
            )

    @staticmethod
    def _parse_month(update_id: str) -> dt.date | None:
        # update_id looks like "2026-Aug"
        try:
            return dt.datetime.strptime(update_id, "%Y-%b").date()
        except ValueError:
            return None
