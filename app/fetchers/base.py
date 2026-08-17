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


@dataclasses.dataclass(slots=True)
class FetchResult:
    products: list[ProductInfo] = dataclasses.field(default_factory=list)
    patches: list[PatchInfo] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)


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
