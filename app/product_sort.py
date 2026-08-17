"""Shared "oldest patch on top" product ordering — used by both the public
dashboard (routers/web.py) and the admin overview (routers/admin.py). Pulled
out into its own module after the admin page shipped with plain alphabetical
sort and reproduced the exact ".NET 10.0 before .NET 6.0" bug the public
page already had fixed; one implementation now, so that can't happen again.
"""

from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Patch, Product

_NATSORT_SPLIT_RE = re.compile(r"(\d+)")


def natural_sort_key(text: str) -> tuple:
    """Splits e.g. '.NET 10.0' into [".net ", 10, ".", 0, ""] so version
    numbers compare numerically instead of lexicographically (plain string
    sort puts ".NET 10.0" before ".NET 6.0" since "1" < "6"). Every token is
    wrapped as (is_text, value) so tuples of mixed digit/non-digit tokens
    stay comparable across differently-shaped strings.

    Only used as a tie-breaker/fallback below — Windows version *codes* mix
    incompatible numbering schemes in one family (e.g. "1507"..."2004" are
    YYMM, but "20H2"/"21H1"... split into much smaller digit runs), so a
    numeric compare of the codes themselves would rank "20H2" ahead of
    "1507" even though 1507 shipped five years earlier. Actual release date
    (see version_sort_key) is the real ordering; this only breaks ties."""
    tokens = _NATSORT_SPLIT_RE.split(text or "")
    return tuple((0, int(tok)) if tok.isdigit() else (1, tok.lower()) for tok in tokens)


async def fetch_oldest_patch_dates(session: AsyncSession) -> dict[int, dt.date]:
    """product_id -> its earliest known patch's release_date."""
    rows = await session.execute(
        select(Patch.product_id, func.min(Patch.release_date))
        .where(Patch.release_date.is_not(None))
        .group_by(Patch.product_id)
    )
    return dict(rows.all())


def version_sort_key(product: Product, oldest_dates: dict[int, dt.date]) -> tuple:
    """(has_date, date, name) — products with a known oldest patch date sort
    by that (true release order); the rest fall back to natural-sorted name,
    after everything dated."""
    oldest = oldest_dates.get(product.id)
    return (
        0 if oldest is not None else 1,
        oldest or dt.date.max,
        natural_sort_key(product.display_name),
    )


async def sort_products_chronologically(session: AsyncSession, products: list[Product]) -> list[Product]:
    oldest_dates = await fetch_oldest_patch_dates(session)
    return sorted(products, key=lambda p: version_sort_key(p, oldest_dates))
