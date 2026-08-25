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
from app.notifier import NewPatchNotice, notify_fetch_errors, notify_new_patches

logger = logging.getLogger("patchwatch.refresh")
settings = get_settings()

_refresh_lock = asyncio.Lock()
_last_run_started_at: dt.datetime | None = None


def is_refresh_running() -> bool:
    return _refresh_lock.locked()


async def maybe_trigger_refresh(trigger: str = "page-load", force: bool = False) -> bool:
    """Returns True if a refresh was (just) started.

    force=True skips the debounce window (but a run already in progress
    still wins — starting a second concurrent refresh isn't safe, see
    _refresh_lock). Used by /admin's "refresh now" so a just-made correction
    can be checked against fresh data immediately, without waiting out
    MIN_REFRESH_INTERVAL_MINUTES."""
    if _refresh_lock.locked():
        return False
    if not force and _last_run_started_at is not None:
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
            # A totally fresh DB (first-ever startup fetch) makes every patch
            # look "new" — that's correct for FetchRun.new_patches, but would
            # blast a notification with hundreds of entries. Decide this
            # *before* inserting this run's own row, so it only ever counts
            # runs that came before it.
            is_first_run = (
                await session.execute(select(func.count()).select_from(FetchRun))
            ).scalar_one() == 0

            run = FetchRun(trigger=trigger, status="running")
            session.add(run)
            await session.commit()

            new_patches = 0
            touched_products: set[str] = set()
            errors: list[str] = []
            product_display_names: dict[str, str] = {}
            new_notices: list[NewPatchNotice] = []
            patch_kb_hints: dict[tuple[str, str], tuple[str, str | None]] = {}

            for fetcher in get_fetchers():
                try:
                    result = await fetcher.fetch()
                except Exception as exc:  # noqa: BLE001 - one bad source must not kill the run
                    logger.exception("Fetcher %s crashed", fetcher.name)
                    errors.append(f"{fetcher.name}: {exc}")
                    continue

                errors.extend(result.errors)
                patch_kb_hints.update(result.patch_kb_hints)

                for product_info in result.products:
                    product_display_names[product_info.key] = product_info.display_name
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
                        new_notices.append(
                            NewPatchNotice(
                                product_display_name=product_display_names.get(
                                    patch_info.product_key, patch_info.product_key
                                ),
                                kb_number=patch_info.kb_number or "",
                                build=patch_info.build or "",
                                title=patch_info.title,
                                severity=patch_info.severity,
                                update_type=patch_info.update_type,
                                release_date=patch_info.release_date,
                            )
                        )

                await session.commit()
                logger.info("Fetcher %s done: %s patches seen", fetcher.name, len(result.patches))

            if patch_kb_hints:
                await _apply_patch_kb_hints(session, patch_kb_hints)
                await session.commit()

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

            # Only alert when the error text actually changed from the
            # previous run — a source that's still broken the same way it
            # was 6 hours ago shouldn't re-alert on every scheduled refresh,
            # only the transition into (or a change in) a broken state
            # should. No "first run" suppression here though (unlike
            # new-patch notices below): a fetcher that's broken from the very
            # first run is exactly the case worth knowing about immediately.
            previous_error = (
                await session.execute(
                    select(FetchRun.error)
                    .where(FetchRun.id != run.id)
                    .order_by(FetchRun.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            should_notify_errors = bool(run.error) and run.error != previous_error

            await session.commit()

            logger.info(
                "Refresh (%s) finished: %s new patches across %s products, status=%s",
                trigger, new_patches, len(touched_products), run.status,
            )

        if new_notices and not is_first_run:
            await notify_new_patches(new_notices)
        if should_notify_errors:
            await notify_fetch_errors(errors, run.status)


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
    # kb_number and build are normalized to "" (never NULL) before they touch
    # the DB. Reason: the uniqueness constraint is (product_id, kb_number,
    # build), and Postgres treats every NULL as distinct from every other
    # NULL — so for any source that leaves one of these unset (all of
    # .NET/.NET Core has no kb_number; MSRC's .NET Framework updates have no
    # build), ON CONFLICT DO NOTHING never matched and every refresh silently
    # inserted a fresh duplicate. "" collides with "" like any normal value,
    # which is what we want here. Both fields need this, not just one — this
    # is the second time this exact class of bug showed up, once per column.
    kb_number = info.kb_number or ""
    build = info.build or ""

    # Sources that identify a release by build number rather than KB (only
    # dotnet.py today) always write kb_number="" themselves — the real KB,
    # if MSRC has one for that product+month, gets filled in afterwards by
    # _apply_patch_kb_hints, mutating just this row's kb_number in place (see
    # its docstring). So for these, "build" alone is this row's natural key:
    # matching on the full (kb_number, build) triple like the branch below
    # would stop finding the row the moment a hint fills in its kb_number —
    # every later refresh would then insert a fresh, blank-kb duplicate for
    # the same build, since the triple it re-upserts with no longer matches
    # anything. No DB-level unique constraint covers (product_id, build)
    # alone, so this branch checks and inserts/updates in plain application
    # code instead of via ON CONFLICT.
    if build and not kb_number:
        existing_id = (
            await session.execute(
                select(Patch.id).where(Patch.product_id == product_id, Patch.build == build)
            )
        ).scalar_one_or_none()
        if existing_id is None:
            patch = Patch(
                product_id=product_id,
                kb_number=kb_number,
                build=build,
                title=info.title,
                update_type=info.update_type,
                release_date=info.release_date,
                severity=info.severity,
                kb_url=info.kb_url,
                release_notes_url=info.release_notes_url,
                source=info.source,
            )
            session.add(patch)
            await session.flush()
            return True

        await session.execute(
            update(Patch).where(Patch.id == existing_id).values(last_seen_at=dt.datetime.now(dt.timezone.utc))
        )
        await session.execute(
            update(Patch)
            .where(Patch.id == existing_id, Patch.manually_edited.is_(False))
            .values(
                title=info.title,
                severity=info.severity,
                release_notes_url=info.release_notes_url,
                release_date=info.release_date,
            )
        )
        return False

    insert_stmt = (
        pg_insert(Patch)
        .values(
            product_id=product_id,
            kb_number=kb_number,
            build=build,
            title=info.title,
            update_type=info.update_type,
            release_date=info.release_date,
            severity=info.severity,
            kb_url=info.kb_url,
            release_notes_url=info.release_notes_url,
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
    kb_filter = Patch.kb_number == kb_number
    build_filter = Patch.build == build

    # Bumping last_seen_at doesn't clobber anything, so it always happens.
    await session.execute(
        update(Patch)
        .where(Patch.product_id == product_id, kb_filter, build_filter)
        .values(last_seen_at=dt.datetime.now(dt.timezone.utc))
    )
    # But title/severity/release_date/kb_url come from the scraper — skip
    # overwriting them on a row a human has manually corrected via /admin, or
    # the next refresh would silently revert the correction. They're
    # refreshed here (not just set at insert time) so a since-fixed fetcher
    # bug self-heals already-stored rows on their next refresh, rather than
    # leaving old wrong data in place forever — release_date for msrc.py's
    # _parse_release_date, which used to flatten every date to the 1st of
    # the month; kb_url for msrc.py's _discover_os_bundles, which upgrades it
    # from an Update Catalog search link to the readable support.microsoft.
    # com article once that KB's own page has been fetched.
    await session.execute(
        update(Patch)
        .where(
            Patch.product_id == product_id,
            kb_filter,
            build_filter,
            Patch.manually_edited.is_(False),
        )
        .values(title=info.title, severity=info.severity, release_date=info.release_date, kb_url=info.kb_url)
    )
    return False


async def _apply_patch_kb_hints(
    session: AsyncSession, hints: dict[tuple[str, str], tuple[str, str | None]]
) -> None:
    """Fills in kb_number/kb_url on existing build-only patch rows from
    another fetcher's patch_kb_hints (see FetchResult) — e.g. dotnet.py's
    .NET Core rows, which have a build number but no KB, matched against
    msrc.py's KB-only knowledge of the same product+month. Runs once after
    every fetcher has committed, so it doesn't depend on fetcher order.

    Matches on (product key, release month) rather than an exact date: the
    two sources are usually driven by the same Patch Tuesday, but there's no
    guarantee they always agree on the exact day.
    """
    rows = (
        await session.execute(
            select(Patch.id, Product.key, Patch.release_date)
            .join(Product, Product.id == Patch.product_id)
            .where(Patch.kb_number == "", Patch.build != "", Patch.manually_edited.is_(False))
        )
    ).all()

    for patch_id, product_key, release_date in rows:
        if release_date is None:
            continue
        hint = hints.get((product_key, release_date.strftime("%Y-%m")))
        if hint is None:
            continue
        kb_number, kb_url = hint
        await session.execute(
            update(Patch).where(Patch.id == patch_id).values(kb_number=kb_number, kb_url=kb_url)
        )
