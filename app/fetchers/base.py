from __future__ import annotations

import dataclasses
import datetime as dt

import httpx

from app.config import get_settings


@dataclasses.dataclass(slots=True)
class ProductInfo:
    key: str
    display_name: str
    family: str
    is_ltsc: bool = False
    source_url: str | None = None
    # Last date this version receives any updates ("end of life"). Left None
    # when a source doesn't track it (e.g. .NET Framework has no equivalent
    # page) or the source only says "already ended" without a concrete date
    # (in that case support_ended is set instead — see below).
    support_end_date: dt.date | None = None
    # True when the source says support has already ended but doesn't repeat
    # an exact date (Microsoft's "End of updates" wording). Distinguishes
    # "known to be over, exact date just not machine-readable here" from
    # "we simply have no data for this product" (both look like None above).
    support_ended: bool = False


@dataclasses.dataclass(slots=True)
class PatchInfo:
    product_key: str
    kb_number: str | None
    build: str | None
    title: str | None
    update_type: str | None
    release_date: dt.date | None
    severity: str | None
    kb_url: str | None
    source: str
    # See models.Patch.release_notes_url — only dotnet.py sets this today.
    release_notes_url: str | None = None


@dataclasses.dataclass(slots=True)
class FetchResult:
    products: list[ProductInfo] = dataclasses.field(default_factory=list)
    patches: list[PatchInfo] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    # (product_key, "YYYY-MM") -> (kb_number, kb_url). For a product whose
    # patch history is split across two sources that each have half the data
    # (e.g. dotnet.py has .NET Core's build numbers but no KB; msrc.py has the
    # KB but no build to match a row on) - a fetcher that only knows the KB
    # side puts it here instead of a competing, build-less patch row; see
    # refresh_service._apply_patch_kb_hints, which fills in kb_number/kb_url
    # on the matching build-only row (by product + release month) once all
    # fetchers have run.
    patch_kb_hints: dict[tuple[str, str], tuple[str, str | None]] = dataclasses.field(default_factory=dict)


class BaseFetcher:
    """One data source. Implementations must never raise — catch what you can
    and put a message in FetchResult.errors instead, so one broken source
    doesn't take the whole refresh down."""

    name: str = "base"

    async def fetch(self) -> FetchResult:  # pragma: no cover - interface
        raise NotImplementedError

    @staticmethod
    def make_client() -> httpx.AsyncClient:
        settings = get_settings()
        return httpx.AsyncClient(
            headers={
                "User-Agent": settings.http_user_agent,
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            },
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
        )
