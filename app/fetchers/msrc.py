"""Fetches .NET Framework *and* .NET (Core) 5+ patch data from the Microsoft
Security Response Center (MSRC) CVRF API — the closest thing left to a
public, machine-readable feed since Microsoft retired the security bulletin
RSS feed.

Why MSRC and not the release-health pages for .NET Framework: unlike Windows,
.NET Framework has no equivalent "release information" page with a clean
history table. MSRC's monthly CVRF documents list every security fix,
including its KB number and the exact .NET Framework version(s) it applies to,
via the document's ProductTree + per-vulnerability Remediations.

.NET (Core) 5+ is normally covered by fetchers/dotnet.py, from the official
dotnet/core releases-index.json — a much cleaner source for build numbers and
full history, but one that carries *no* KB number at all. MSRC's ProductTree
also lists ".NET 8.0 installed on Windows/Linux/Mac" (etc.) entries with the
KB every security release ships under, so this fetcher picks those up too and
hands them to refresh_service as patch_kb_hints (see FetchResult) rather than
as full patch rows — dotnet.py already has the real row (with build number,
full title, non-security releases too); this only fills in the one field it's
missing, keyed by (product_key, year-month) since that's all MSRC gives us to
match on (no build number here).

Known limitation: MSRC only covers *security* updates. Non-security .NET
Framework rollups are not captured here. It also only lists the fine-grained,
single-version-branch KBs (e.g. "KB5120705 ... for .NET Framework 3.5 and
4.8") — the combined "3.5, 4.8 and 4.8.1 in one package" KB Microsoft also
publishes for the same release is not referenced anywhere in the CVRF
document (verified against the August 2026 update), so it never appears here
either. Data quality also depends on Microsoft's CVRF document consistency,
which has been known to vary — this fetcher is written defensively (per-item
try/except) so a malformed entry is skipped rather than aborting the whole
run.
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
# .NET (Core) 5+ product names look like ".NET 8.0 installed on Windows" —
# distinct enough from the Framework wording above ("... on Windows Server
# 2022", no "installed") that a separate, simpler pattern is all this needs.
DOTNET_CORE_VERSION_RE = re.compile(r"\.NET (\d+\.\d+) installed on", re.IGNORECASE)
KB_DIGITS_RE = re.compile(r"(\d{6,7})")

FAMILY_BY_PREFIX = {
    "dotnetfx": ProductFamily.DOTNET_FRAMEWORK.value,
    "dotnet": ProductFamily.DOTNET.value,
}
DISPLAY_NAME_BY_PREFIX = {
    "dotnetfx": ".NET Framework {version}",
    "dotnet": ".NET {version}",
}


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

            known_versions: set[tuple[str, str]] = set()

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

    async def _process_month(
        self, client, update_id: str, result: FetchResult, known_versions: set[tuple[str, str]]
    ) -> None:
        resp = await client.get(CVRF_URL_TEMPLATE.format(update_id=update_id), headers=JSON_HEADERS)
        resp.raise_for_status()
        doc = resp.json()

        product_names = self._collect_product_names(doc.get("ProductTree", {}))
        products_by_id = self._versions_by_product_id(product_names)
        if not products_by_id:
            return

        release_date = self._parse_month(update_id)

        for prefix, version in {pv for pvs in products_by_id.values() for pv in pvs}:
            if (prefix, version) in known_versions:
                continue
            known_versions.add((prefix, version))
            product_key = f"{prefix}-{version}"
            result.products.append(
                ProductInfo(
                    key=product_key,
                    display_name=DISPLAY_NAME_BY_PREFIX[prefix].format(version=version),
                    family=FAMILY_BY_PREFIX[prefix],
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
                        remediation, products_by_id, release_date, title, result, seen_in_month
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
    def _versions_by_product_id(product_names: dict[str, str]) -> dict[str, list[tuple[str, str]]]:
        """Maps ProductID -> [(product_key_prefix, version), ...] for every
        product line this fetcher understands — "dotnetfx" for .NET
        Framework (a product line can cover several versions in one entry,
        e.g. "3.5 AND 4.8.1"), "dotnet" for .NET (Core) 5+ (always exactly
        one version). Prefixes match the product_key scheme fetchers/dotnet.py
        uses, so patches for "dotnet" land on the same product row."""
        result: dict[str, list[tuple[str, str]]] = {}
        for pid, name in product_names.items():
            lname = name.lower()
            if ".net framework" in lname:
                m = FRAMEWORK_VERSION_RE.search(name)
                if m:
                    result[pid] = [("dotnetfx", v) for v in _split_versions(m.group(1))]
            elif ".net" in lname:
                m = DOTNET_CORE_VERSION_RE.search(name)
                if m:
                    result[pid] = [("dotnet", m.group(1))]
        return result

    def _handle_remediation(
        self,
        remediation: dict,
        products_by_id: dict[str, list[tuple[str, str]]],
        release_date: dt.date | None,
        title: str | None,
        result: FetchResult,
        seen_in_month: set[tuple[str, str]],
    ) -> None:
        if remediation.get("Type") not in ("Vendor Fix", 2, "2"):
            return

        product_ids = [str(p) for p in remediation.get("ProductID", []) or []]
        versions: set[tuple[str, str]] = set()
        for pid in product_ids:
            versions.update(products_by_id.get(pid, []))
        if not versions:
            return

        description = remediation.get("Description")
        desc_value = description.get("Value") if isinstance(description, dict) else description
        kb_match = KB_DIGITS_RE.search(desc_value or "") or KB_DIGITS_RE.search(remediation.get("URL", "") or "")
        if not kb_match:
            return
        kb_number = f"KB{kb_match.group(1)}"
        kb_url = remediation.get("URL") or f"https://support.microsoft.com/help/{kb_match.group(1)}"

        for prefix, version in versions:
            product_key = f"{prefix}-{version}"
            dedup_key = (product_key, kb_number)
            if dedup_key in seen_in_month:
                continue
            seen_in_month.add(dedup_key)

            if prefix == "dotnet":
                # dotnet.py already writes the real row for this product/month
                # (build number, full title, non-security releases too) — we
                # only know the KB, not the build, so we can't match it to a
                # row ourselves. Hand it to refresh_service as a hint instead
                # of a competing kb-less patch row; see FetchResult.
                if release_date:
                    result.patch_kb_hints[(product_key, release_date.strftime("%Y-%m"))] = (kb_number, kb_url)
                continue

            result.patches.append(
                PatchInfo(
                    product_key=product_key,
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
