"""Orchestrates a full refresh: run every fetcher, upsert what it found, and
record a FetchRun. Also implements the two triggers the app supports:

- maybe_trigger_refresh(): called on every page load via HTMX. Starts a
  background refresh unless one is already running or the debounce window
  (MIN_REFRESH_INTERVAL_MINUTES) hasn't elapsed yet.
- run_all_fetchers(): called directly by the APScheduler job on a fixed
  interval (FETCH_INTERVAL_HOURS), independent of whether anyone visits.

Both funnel through the same asyncio.Lock so a scheduled run and a
page-triggered run can never overlap.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_factory
from app.fetchers.base import PatchInfo, ProductInfo
from app.fetchers.registry import get_fetchers
from app.models import FetchRun, Patch, Product

logger = logging.getLogger("patchwatch.refresh")
settings = get_settings()

_refresh_lock = asyncio.Lock()
_last_run_started_at: dt.datetime | None = None


def is_refresh_running() -> bool:
    return _refresh_lock.locked()


async def maybe_trigger_refresh(trigger: str = "page-load") -> bool:
    """Returns True if a refresh was (just) started."""
    if _refresh_lock.locked():
        return False
    if _last_run_started_at is not None:
        elapsed = dt.datetime.now(dt.timezone.utc) - _last_run_started_at
        if elapsed < dt.timedelta(minutes=settings.min_refresh_interval_minutes):
            return False
    asyncio.create_task(run_all_fetchers(trigger=trigger))
    return True


async def run_all_fetchers(trigger: str = "scheduler") -> None:
    global _last_run_started_at
    async with _refresh_lock:
        _last_run_started_at = dt.datetime.now(dt.timezone.utc)
        async with async_session_factory() as session:
            run = FetchRun(trigger=trigger, status="running")
            session.add(run)
            await session.commit()

            new_patches = 0
            touched_products: set[str] = set()
            errors: list[str] = []

            for fetcher in get_fetchers():
                try:
                    result = await fetcher.fetch()
                except Exception as exc:  # noqa: BLE001 - one bad source must not kill the run
                    logger.exception("Fetcher %s crashed", fetcher.name)
                    errors.append(f"{fetcher.name}: {exc}")
                    continue

                errors.extend(result.errors)

                for product_info in result.products:
                    await _upsert_product(session, product_info)
                await session.flush()

                product_ids = await _product_key_to_id(session)

                for patch_info in result.patches:
                    product_id = product_ids.get(patch_info.product_key)
                    if product_id is None:
                        continue
                    touched_products.add(patch_info.product_key)
                    if await _upsert_patch(session, product_id, patch_info):
                        new_patches += 1

                await session.commit()
                logger.info("Fetcher %s done: %s patches seen", fetcher.name, len(result.patches))

            run.finished_at = dt.datetime.now(dt.timezone.utc)
            if errors and not touched_products:
                run.status = "error"
            elif errors:
                run.status = "partial"
            else:
                run.status = "success"
            run.new_patches = new_patches
            run.updated_products = len(touched_products)
            run.error = "\n".join(errors)[:8000] if errors else None
            await session.commit()

            logger.info(
                "Refresh (%s) finished: %s new patches across %s products, status=%s",
                trigger, new_patches, len(touched_products), run.status,
            )


async def _upsert_product(session: AsyncSession, info: ProductInfo) -> None:
    stmt = pg_insert(Product).values(
        key=info.key,
        display_name=info.display_name,
        family=info.family,
        is_ltsc=info.is_ltsc,
        source_url=info.source_url,
        support_end_date=info.support_end_date,
        support_ended=info.support_ended,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Product.key],
        set_={
            "display_name": stmt.excluded.display_name,
            "family": stmt.excluded.family,
            "is_ltsc": stmt.excluded.is_ltsc,
            "source_url": stmt.excluded.source_url,
            # Keep the previous date if this run didn't find one (e.g. a
            # transient parse miss), rather than clobbering it with NULL.
            "support_end_date": func.coalesce(stmt.excluded.support_end_date, Product.support_end_date),
            "support_ended": stmt.excluded.support_ended,
        },
    )
    await session.execute(stmt)


async def _product_key_to_id(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(select(Product.key, Product.id))
    return dict(rows.all())


async def _upsert_patch(session: AsyncSession, product_id: int, info: PatchInfo) -> bool:
    """Returns True if this was a genuinely new patch (not seen before)."""
    insert_stmt = (
        pg_insert(Patch)
        .values(
            product_id=product_id,
            kb_number=info.kb_number,
            build=info.build,
            title=info.title,
            update_type=info.update_type,
            release_date=info.release_date,
            severity=info.severity,
            kb_url=info.kb_url,
            source=info.source,
        )
        .on_conflict_do_nothing(index_elements=[Patch.product_id, Patch.kb_number, Patch.build])
        .returning(Patch.id)
    )
    inserted_id = (await session.execute(insert_stmt)).scalar_one_or_none()
    if inserted_id is not None:
        return True

    # Already known: just refresh mutable fields / last_seen_at so the UI can
    # show "last confirmed" and severity enrichment from a later source can
    # still land on an existing row.
    kb_filter = Patch.kb_number.is_(None) if info.kb_number is None else Patch.kb_number == info.kb_number
    build_filter = Patch.build.is_(None) if info.build is None else Patch.build == info.build
    await session.execute(
        update(Patch)
        .where(Patch.product_id == product_id, kb_filter, build_filter)
        .values(last_seen_at=dt.datetime.now(dt.timezone.utc), title=info.title, severity=info.severity)
    )
    return False
