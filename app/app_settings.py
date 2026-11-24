"""Runtime settings an operator toggles on /admin, stored in the
app_settings table (see models.AppSetting).

FETCH_OUTDATED_PRODUCTS: whether the refresh still queries products that
app.outdated considers outdated (e.g. SQL Server 2016 next to 2017–2025).
Off by default — those products stopped receiving updates, so querying them
every few hours mostly costs requests against Microsoft's sites. Their
stored history stays untouched and visible either way.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting

FETCH_OUTDATED_PRODUCTS = "fetch_outdated_products"

_DEFAULTS: dict[str, bool] = {
    FETCH_OUTDATED_PRODUCTS: False,
}


async def get_bool(session: AsyncSession, key: str) -> bool:
    value = (
        await session.execute(select(AppSetting.value).where(AppSetting.key == key))
    ).scalar_one_or_none()
    if value is None:
        return _DEFAULTS[key]
    return value == "1"


async def set_bool(session: AsyncSession, key: str, enabled: bool) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value="1" if enabled else "0"))
    else:
        row.value = "1" if enabled else "0"
