"""Which products are "outdated", i.e. no longer receive updates while the
rest of their family still does — the basis for the "Hide outdated" button
in the dashboard's hidden-products menu.

The rule is deliberately relative, never a fixed cutoff date: a product is
outdated when its newest patch lags more than OUTDATED_LAG_DAYS behind the
newest patch of *any* product in the same family. That way each family sets
its own pace:

- SQL Server 2016 (last update July) is outdated next to 2017–2025, which
  all got a November update.
- The .NET Framework family is never outdated as a whole just because its
  products only got an update in October while Windows already has
  November's — they're only compared with each other, and all of them got
  the same October update.
- Retired Windows versions and out-of-support .NET versions fall out
  automatically once their siblings move on, with no product list to
  maintain.

75 days is roughly two and a half monthly patch cycles: long enough that a
product on a bi-monthly cadence (e.g. some SQL Server CU tracks) or one that
skipped a single month isn't flagged, short enough that a product which has
clearly stopped receiving updates shows up within a few months.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Mapping

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Patch, Product

OUTDATED_LAG_DAYS = 75


def find_outdated(
    latest_dates: Mapping[str, dt.date | None], lag_days: int = OUTDATED_LAG_DAYS
) -> set[str]:
    """latest_dates: product key -> release date of that product's newest
    patch, for the products of ONE family. Returns the keys that lag more
    than lag_days behind the family's newest patch.

    Products without any known patch are never flagged — there's nothing to
    compare, and hiding them would hide a product the fetchers simply
    haven't picked up data for yet."""
    known = [d for d in latest_dates.values() if d is not None]
    if not known:
        return set()
    cutoff = max(known) - dt.timedelta(days=lag_days)
    return {key for key, d in latest_dates.items() if d is not None and d < cutoff}


async def find_outdated_products(session: AsyncSession) -> list[Product]:
    """The outdated products in the database, by the
    same rule as find_outdated above — so what the refresh skips (see
    app_settings.FETCH_OUTDATED_PRODUCTS) is exactly what the dashboard's
    "Hide outdated" button hides. Sorted by family, then name."""
    rows = (
        await session.execute(
            select(Product, func.max(Patch.release_date))
            .outerjoin(Patch, Patch.product_id == Product.id)
            .group_by(Product.id)
        )
    ).all()
    by_family: dict[str, dict[str, dt.date | None]] = defaultdict(dict)
    products_by_key = {}
    for product, latest in rows:
        by_family[product.family][product.key] = latest
        products_by_key[product.key] = product
    outdated_keys: set[str] = set()
    for latest_dates in by_family.values():
        outdated_keys |= find_outdated(latest_dates)
    return sorted(
        (products_by_key[key] for key in outdated_keys),
        key=lambda p: (p.family, p.display_name),
    )

