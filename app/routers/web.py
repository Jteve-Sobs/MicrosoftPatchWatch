import datetime as dt
import json
from functools import partial
from xml.etree.ElementTree import Element, SubElement, indent as xml_indent, tostring

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, select

from app.database import async_session_factory
from app.i18n import (
    SUPPORTED_LOCALES,
    DEFAULT_LOCALE,
    eol_status,
    format_date,
    format_datetime,
    resolve_locale,
    translate,
)
from app.models import FetchRun, Patch, Product
from app.product_sort import fetch_oldest_patch_dates, version_sort_key
from app.refresh_service import is_refresh_running, maybe_trigger_refresh
from app.static_version import static_version

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
# Vanilla Jinja2 has no tojson filter (unlike Flask). Must return Markup, not
# a plain str — otherwise the environment's autoescaping HTML-entity-encodes
# the quotes (&#34;), which is invalid inside a <script> block.
templates.env.filters["tojson"] = lambda value: Markup(json.dumps(value))
templates.env.globals["static_version"] = static_version

FAMILY_ORDER = ["windows_client", "windows_server", "dotnet_framework", "dotnet"]


def _i18n_context(locale: str) -> dict:
    return {
        "locale": locale,
        "t": partial(translate, locale),
        "format_date": partial(format_date, locale),
        "format_datetime": partial(format_datetime, locale),
        "eol_status": eol_status,
    }


def _safe_next(path: str | None) -> str:
    if not path or not path.startswith("/") or path.startswith("//") or "://" in path:
        return "/"
    return path


async def _load_dashboard_data():
    async with async_session_factory() as session:
        # Ordered by family only here; within a family, rows are ordered by
        # each product's oldest patch date further down (version_sort_key)
        # so "oldest on top" reflects actual release history rather than
        # trying to numerically parse Windows/.NET version codes.
        products = (
            await session.execute(select(Product).order_by(Product.family))
        ).scalars().all()

        oldest_dates = await fetch_oldest_patch_dates(session)

        latest_subq = (
            select(Patch.product_id, func.max(Patch.release_date).label("max_date"))
            .group_by(Patch.product_id)
            .subquery()
        )
        latest_rows = (
            await session.execute(
                select(Patch).join(
                    latest_subq,
                    (Patch.product_id == latest_subq.c.product_id)
                    & (Patch.release_date == latest_subq.c.max_date),
                )
            )
        ).scalars().all()

        latest_by_product: dict[int, Patch] = {}
        for p in latest_rows:
            existing = latest_by_product.get(p.product_id)
            if existing is None or p.last_seen_at > existing.last_seen_at:
                latest_by_product[p.product_id] = p

        last_run = (
            await session.execute(select(FetchRun).order_by(FetchRun.started_at.desc()).limit(1))
        ).scalar_one_or_none()

        # The client-side filter box used to only match a product's name and
        # its *latest* KB — searching for an older KB/build/title buried in a
        # product's history (or its severity/update type) found nothing. This
        # builds one search blob per product covering every patch it has, so
        # the row-level filter (see app.js patchwatchRefreshVisibility) can
        # match on any of it.
        search_rows = await session.execute(
            select(
                Patch.product_id,
                Patch.kb_number,
                Patch.build,
                Patch.title,
                Patch.update_type,
                Patch.severity,
                Patch.release_date,
            )
        )
        search_blob_by_product: dict[int, str] = {}
        for product_id, kb_number, build, title, update_type, severity, release_date in search_rows:
            parts = (kb_number, build, title, update_type, severity, release_date.isoformat() if release_date else None)
            text = " ".join(p for p in parts if p)
            if not text:
                continue
            existing = search_blob_by_product.get(product_id, "")
            search_blob_by_product[product_id] = f"{existing} {text}" if existing else text

        grouped: dict[str, list[dict]] = {family: [] for family in FAMILY_ORDER}
        for product in products:
            grouped.setdefault(product.family, []).append(
                {
                    "product": product,
                    "latest": latest_by_product.get(product.id),
                    "search_blob": search_blob_by_product.get(product.id, ""),
                }
            )

        for items in grouped.values():
            items.sort(key=lambda item: version_sort_key(item["product"], oldest_dates))
        return grouped, last_run


async def _build_export_xml(scope: str) -> str:
    """scope="month" limits to patches released in the current calendar month
    (across all products); scope="all" is the full history. Products with no
    matching patches are omitted entirely rather than emitted as an empty
    <Product> — keeps a "month" export from listing every product just to say
    nothing happened for most of them."""
    today = dt.date.today()

    async with async_session_factory() as session:
        products = (
            await session.execute(select(Product).order_by(Product.family, Product.display_name))
        ).scalars().all()
        patches = (
            await session.execute(
                select(Patch).order_by(Patch.product_id, Patch.release_date.desc().nullslast())
            )
        ).scalars().all()

    patches_by_product: dict[int, list[Patch]] = {}
    for patch in patches:
        patches_by_product.setdefault(patch.product_id, []).append(patch)

    root = Element(
        "Patches",
        {
            "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "scope": scope,
        },
    )
    for product in products:
        product_patches = patches_by_product.get(product.id, [])
        if scope == "month":
            product_patches = [
                p
                for p in product_patches
                if p.release_date and p.release_date.year == today.year and p.release_date.month == today.month
            ]
        if not product_patches:
            continue

        product_el = SubElement(
            root,
            "Product",
            {"key": product.key, "name": product.display_name, "family": product.family},
        )
        for patch in product_patches:
            attrs = {
                "kb": patch.kb_number or "",
                "build": patch.build or "",
                "title": patch.title or "",
                "type": patch.update_type or "",
                "date": patch.release_date.isoformat() if patch.release_date else "",
                "severity": patch.severity or "",
                "source": patch.source or "",
            }
            if patch.kb_url:
                attrs["url"] = patch.kb_url
            SubElement(product_el, "Patch", attrs)

    xml_indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="unicode")


@router.get("/export/xml", response_class=PlainTextResponse)
async def export_xml(scope: str = "all"):
    if scope not in ("all", "month"):
        scope = "all"
    xml_text = await _build_export_xml(scope)
    return PlainTextResponse(xml_text, media_type="application/xml")


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, locale: str = Depends(resolve_locale)):
    grouped, last_run = await _load_dashboard_data()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "grouped": grouped,
            "family_order": FAMILY_ORDER,
            "last_run": last_run,
            "refresh_running": is_refresh_running(),
            **_i18n_context(locale),
        },
    )


@router.get("/lang/{code}")
async def set_language(code: str, request: Request):
    if code not in SUPPORTED_LOCALES:
        code = DEFAULT_LOCALE
    response = RedirectResponse(url=_safe_next(request.query_params.get("next")), status_code=302)
    response.set_cookie("lang", code, max_age=60 * 60 * 24 * 365, samesite="lax")
    return response


@router.get("/partials/status", response_class=HTMLResponse)
async def status_partial(request: Request, locale: str = Depends(resolve_locale)):
    async with async_session_factory() as session:
        last_run = (
            await session.execute(select(FetchRun).order_by(FetchRun.started_at.desc()).limit(1))
        ).scalar_one_or_none()
    running = is_refresh_running()
    response = templates.TemplateResponse(
        request,
        "partials/status.html",
        {"last_run": last_run, "refresh_running": running, **_i18n_context(locale)},
    )
    # Tell the tables container (see index.html) to reload once a run has
    # finished, so newly found patches show up without a manual page refresh.
    if not running and last_run is not None and last_run.status != "running":
        response.headers["HX-Trigger"] = "patchwatch-refreshed"
    return response


@router.get("/partials/tables", response_class=HTMLResponse)
async def tables_partial(request: Request, locale: str = Depends(resolve_locale)):
    grouped, _ = await _load_dashboard_data()
    return templates.TemplateResponse(
        request,
        "partials/all_tables.html",
        {"grouped": grouped, "family_order": FAMILY_ORDER, **_i18n_context(locale)},
    )


@router.post("/refresh")
async def trigger_refresh():
    started = await maybe_trigger_refresh(trigger="manual")
    return {"started": started, "running": is_refresh_running()}


@router.get("/product/{product_key}/history", response_class=HTMLResponse)
async def product_history(request: Request, product_key: str, locale: str = Depends(resolve_locale)):
    async with async_session_factory() as session:
        product = (
            await session.execute(select(Product).where(Product.key == product_key))
        ).scalar_one_or_none()
        if product is None:
            return HTMLResponse("Not found", status_code=404)
        patches = (
            await session.execute(
                select(Patch)
                .where(Patch.product_id == product.id)
                .order_by(Patch.release_date.desc().nullslast())
            )
        ).scalars().all()
    return templates.TemplateResponse(
        request,
        "partials/history_rows.html",
        {"product": product, "patches": patches, **_i18n_context(locale)},
    )
