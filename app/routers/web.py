from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.database import async_session_factory
from app.models import FetchRun, Patch, Product
from app.refresh_service import is_refresh_running, maybe_trigger_refresh

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

FAMILY_LABELS = {
    "windows_client": "Windows (Client)",
    "windows_server": "Windows Server",
    "dotnet_framework": ".NET Framework",
    "dotnet": ".NET",
}
FAMILY_ORDER = ["windows_client", "windows_server", "dotnet_framework", "dotnet"]


async def _load_dashboard_data():
    async with async_session_factory() as session:
        products = (
            await session.execute(select(Product).order_by(Product.family, Product.display_name))
        ).scalars().all()

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

        grouped: dict[str, list[dict]] = {family: [] for family in FAMILY_ORDER}
        for product in products:
            grouped.setdefault(product.family, []).append(
                {"product": product, "latest": latest_by_product.get(product.id)}
            )
        return grouped, last_run


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    grouped, last_run = await _load_dashboard_data()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "grouped": grouped,
            "family_labels": FAMILY_LABELS,
            "family_order": FAMILY_ORDER,
            "last_run": last_run,
            "refresh_running": is_refresh_running(),
        },
    )


@router.get("/partials/status", response_class=HTMLResponse)
async def status_partial(request: Request):
    async with async_session_factory() as session:
        last_run = (
            await session.execute(select(FetchRun).order_by(FetchRun.started_at.desc()).limit(1))
        ).scalar_one_or_none()
    running = is_refresh_running()
    response = templates.TemplateResponse(
        request, "partials/status.html", {"last_run": last_run, "refresh_running": running}
    )
    # Tell the tables container (see index.html) to reload once a run has
    # finished, so newly found patches show up without a manual page refresh.
    if not running and last_run is not None and last_run.status != "running":
        response.headers["HX-Trigger"] = "patchwatch-refreshed"
    return response


@router.get("/partials/tables", response_class=HTMLResponse)
async def tables_partial(request: Request):
    grouped, _ = await _load_dashboard_data()
    return templates.TemplateResponse(
        request,
        "partials/all_tables.html",
        {"grouped": grouped, "family_labels": FAMILY_LABELS, "family_order": FAMILY_ORDER},
    )


@router.post("/refresh")
async def trigger_refresh():
    started = await maybe_trigger_refresh(trigger="manual")
    return {"started": started, "running": is_refresh_running()}


@router.get("/product/{product_key}/history", response_class=HTMLResponse)
async def product_history(request: Request, product_key: str):
    async with async_session_factory() as session:
        product = (
            await session.execute(select(Product).where(Product.key == product_key))
        ).scalar_one_or_none()
        if product is None:
            return HTMLResponse("Nicht gefunden", status_code=404)
        patches = (
            await session.execute(
                select(Patch)
                .where(Patch.product_id == product.id)
                .order_by(Patch.release_date.desc().nullslast())
            )
        ).scalars().all()
    return templates.TemplateResponse(
        request, "partials/history_rows.html", {"product": product, "patches": patches}
    )
