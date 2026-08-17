"""Admin area: manual correction of individual patch rows for the cases a
scraper gets wrong (README "Ideen für später" -> now implemented), plus a
debounce-bypassing "refresh now" for checking a correction immediately.

Protected by HTTP Basic Auth (see app.config.Settings.admin_password) — this
is a single-operator tool, not a multi-user system, so there's one fixed
username ("admin") and one shared password rather than real accounts.
"""

from __future__ import annotations

import datetime as dt
import secrets
from functools import partial

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.config import get_settings
from app.database import async_session_factory
from app.i18n import (
    eol_status,
    format_date,
    format_datetime,
    resolve_locale,
    translate,
)
from app.models import Patch, Product
from app.product_sort import sort_products_chronologically
from app.refresh_service import is_refresh_running, maybe_trigger_refresh
from app.static_version import static_version

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["static_version"] = static_version

_basic_auth = HTTPBasic()


def require_admin(credentials: HTTPBasicCredentials = Depends(_basic_auth)) -> str:
    settings = get_settings()
    # compare_digest on both (not just the password) avoids leaking, via
    # response timing, whether a guessed username was even the right length.
    username_ok = secrets.compare_digest(credentials.username, "admin")
    password_ok = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def _i18n_context(locale: str) -> dict:
    return {
        "locale": locale,
        "t": partial(translate, locale),
        "format_date": partial(format_date, locale),
        "format_datetime": partial(format_datetime, locale),
        "eol_status": eol_status,
    }


UPDATE_TYPES = ["Security", "Preview", "Out-of-Band", "Update"]


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def admin_index(request: Request, locale: str = Depends(resolve_locale)):
    async with async_session_factory() as session:
        # Not ordered in SQL: the template groups by family itself via
        # `selectattr`, which preserves this list's relative order — so this
        # only needs to get the *within-family* order right, via the same
        # "oldest patch first" chronological sort as the public dashboard
        # (see app.product_sort — plain alphabetical sort here previously
        # reproduced the exact ".NET 10.0 before .NET 6.0" bug that was
        # already fixed on the public page).
        products = (await session.execute(select(Product))).scalars().all()
        products = await sort_products_chronologically(session, products)
        count_by_product: dict[int, int] = dict(
            (
                await session.execute(
                    select(Patch.product_id, func.count(Patch.id)).group_by(Patch.product_id)
                )
            ).all()
        )

    return templates.TemplateResponse(
        request,
        "admin/index.html",
        {
            "products": products,
            "count_by_product": count_by_product,
            "refresh_running": is_refresh_running(),
            **_i18n_context(locale),
        },
    )


@router.post("/refresh", dependencies=[Depends(require_admin)])
async def admin_force_refresh():
    started = await maybe_trigger_refresh(trigger="admin-manual", force=True)
    return RedirectResponse(url=f"/admin?refresh_started={int(started)}", status_code=status.HTTP_303_SEE_OTHER)


async def _get_product_or_404(session, key: str) -> Product:
    product = (await session.execute(select(Product).where(Product.key == key))).scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=404, detail="Unknown product")
    return product


@router.get(
    "/products/{product_key}", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def admin_product_patches(
    request: Request, product_key: str, locale: str = Depends(resolve_locale)
):
    async with async_session_factory() as session:
        product = await _get_product_or_404(session, product_key)
        patches = (
            await session.execute(
                select(Patch)
                .where(Patch.product_id == product.id)
                .order_by(Patch.release_date.desc().nullslast(), Patch.id.desc())
            )
        ).scalars().all()

    return templates.TemplateResponse(
        request,
        "admin/product_patches.html",
        {"product": product, "patches": patches, **_i18n_context(locale)},
    )


def _patch_form_context(locale: str, *, product: Product, patch: Patch | None, error: str | None = None) -> dict:
    return {
        "product": product,
        "patch": patch,
        "update_types": UPDATE_TYPES,
        "error": error,
        **_i18n_context(locale),
    }


@router.get(
    "/products/{product_key}/patches/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
async def admin_new_patch_form(request: Request, product_key: str, locale: str = Depends(resolve_locale)):
    async with async_session_factory() as session:
        product = await _get_product_or_404(session, product_key)
    return templates.TemplateResponse(
        request, "admin/patch_form.html", _patch_form_context(locale, product=product, patch=None)
    )


def _parse_date_field(value: str) -> dt.date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"'{value}' is not a valid date (expected YYYY-MM-DD)") from exc


@router.post(
    "/products/{product_key}/patches/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
async def admin_create_patch(
    request: Request,
    product_key: str,
    locale: str = Depends(resolve_locale),
    release_date: str = Form(""),
    kb_number: str = Form(""),
    build: str = Form(""),
    title: str = Form(""),
    update_type: str = Form(""),
    severity: str = Form(""),
    kb_url: str = Form(""),
    manually_edited: str | None = Form(None),
):
    async with async_session_factory() as session:
        product = await _get_product_or_404(session, product_key)
        try:
            parsed_date = _parse_date_field(release_date)
        except ValueError as exc:
            return templates.TemplateResponse(
                request,
                "admin/patch_form.html",
                _patch_form_context(locale, product=product, patch=None, error=str(exc)),
                status_code=422,
            )

        # A manual entry has no natural (product_id, kb_number, build)
        # uniqueness guarantee the way scraper output does — kb_number/build
        # are free text here, so we just insert. If it collides with an
        # existing row later, the *scraper's* upsert will simply skip it
        # (ON CONFLICT DO NOTHING) rather than error.
        patch = Patch(
            product_id=product.id,
            kb_number=kb_number.strip() or "",
            build=build.strip() or "",
            title=title.strip() or None,
            update_type=update_type.strip() or None,
            release_date=parsed_date,
            severity=severity.strip() or None,
            kb_url=kb_url.strip() or None,
            source="manual",
            # Checkbox unchecked -> field absent from the form -> None here.
            # Left true by default so a fresh manual entry isn't immediately
            # eligible for the scraper to silently touch, but the admin can
            # untick it (now or later, via edit) to re-enable sync.
            manually_edited=manually_edited is not None,
        )
        session.add(patch)
        await session.commit()

    return RedirectResponse(url=f"/admin/products/{product_key}", status_code=status.HTTP_303_SEE_OTHER)


async def _get_patch_or_404(session, patch_id: int) -> Patch:
    patch = (await session.execute(select(Patch).where(Patch.id == patch_id))).scalar_one_or_none()
    if patch is None:
        raise HTTPException(status_code=404, detail="Unknown patch")
    return patch


@router.get(
    "/patches/{patch_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def admin_edit_patch_form(request: Request, patch_id: int, locale: str = Depends(resolve_locale)):
    async with async_session_factory() as session:
        patch = await _get_patch_or_404(session, patch_id)
        product = (await session.execute(select(Product).where(Product.id == patch.product_id))).scalar_one()
    return templates.TemplateResponse(
        request, "admin/patch_form.html", _patch_form_context(locale, product=product, patch=patch)
    )


@router.post(
    "/patches/{patch_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def admin_update_patch(
    request: Request,
    patch_id: int,
    locale: str = Depends(resolve_locale),
    release_date: str = Form(""),
    kb_number: str = Form(""),
    build: str = Form(""),
    title: str = Form(""),
    update_type: str = Form(""),
    severity: str = Form(""),
    kb_url: str = Form(""),
    manually_edited: str | None = Form(None),
):
    async with async_session_factory() as session:
        patch = await _get_patch_or_404(session, patch_id)
        product = (await session.execute(select(Product).where(Product.id == patch.product_id))).scalar_one()
        try:
            parsed_date = _parse_date_field(release_date)
        except ValueError as exc:
            return templates.TemplateResponse(
                request,
                "admin/patch_form.html",
                _patch_form_context(locale, product=product, patch=patch, error=str(exc)),
                status_code=422,
            )

        patch.kb_number = kb_number.strip() or ""
        patch.build = build.strip() or ""
        patch.title = title.strip() or None
        patch.update_type = update_type.strip() or None
        patch.release_date = parsed_date
        patch.severity = severity.strip() or None
        patch.kb_url = kb_url.strip() or None
        # Unticking this in the edit form is the whole point of the
        # checkbox: it lets the next scraper refresh freely touch the row
        # again instead of being permanently protected.
        patch.manually_edited = manually_edited is not None
        await session.commit()
        product_key = product.key

    return RedirectResponse(url=f"/admin/products/{product_key}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/patches/{patch_id}/delete", dependencies=[Depends(require_admin)])
async def admin_delete_patch(patch_id: int):
    async with async_session_factory() as session:
        patch = await _get_patch_or_404(session, patch_id)
        product = (await session.execute(select(Product).where(Product.id == patch.product_id))).scalar_one()
        product_key = product.key
        await session.delete(patch)
        await session.commit()

    return RedirectResponse(url=f"/admin/products/{product_key}", status_code=status.HTTP_303_SEE_OTHER)
