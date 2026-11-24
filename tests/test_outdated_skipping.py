"""Outdated products are not queried by default (app_settings.
FETCH_OUTDATED_PRODUCTS, toggled on /admin): the refresh hands their keys to
every fetcher (fetchers.base.BaseFetcher.skip_product_keys), the per-product
fetchers skip their requests, and refresh_service drops whatever a shared
page still returned for them."""

from __future__ import annotations

import datetime as dt

import httpx
from sqlalchemy import select

from app import refresh_service
from app.app_settings import FETCH_OUTDATED_PRODUCTS, get_bool, set_bool
from app.database import async_session_factory
from app.fetchers.base import BaseFetcher, FetchResult, PatchInfo, ProductInfo
from app.fetchers.dotnet import INDEX_URL, DotNetFetcher
from app.fetchers.sql_server import SqlServerFetcher
from app.models import Patch, Product
from app.outdated import find_outdated_products
from tests.conftest import load_fixture

SQL_URL = "https://learn.microsoft.com/en-us/troubleshoot/sql/releases/sqlserver-{}/build-versions"
DOTNET_CHANNEL_URL = "https://builds.dotnet.microsoft.com/dotnet/release-metadata/{}/releases.json"


async def _seed_sql_server(make_product, make_patch) -> None:
    """SQL Server 2016's last update in July, 2022's in November: 2016 is
    outdated (more than OUTDATED_LAG_DAYS behind its family)."""
    async with async_session_factory() as session:
        old = make_product("sqlserver-2016", display_name="SQL Server 2016", family="sql_server")
        new = make_product("sqlserver-2022", display_name="SQL Server 2022", family="sql_server")
        session.add_all([old, new])
        await session.flush()
        session.add_all([
            make_patch(old.id, kb_number="KB1", release_date=dt.date(2026, 7, 8)),
            make_patch(new.id, kb_number="KB2", release_date=dt.date(2026, 11, 10)),
        ])
        await session.commit()


def _patch(product_key: str, kb: str) -> PatchInfo:
    return PatchInfo(
        product_key=product_key, kb_number=kb, build="", title=kb, update_type="Security",
        release_date=dt.date(2026, 11, 20), severity=None, kb_url=None, source="fake",
    )


class _RecordingFetcher(BaseFetcher):
    """Returns new patches for both SQL Server versions (like a shared page
    would) and remembers which keys it was told to skip."""

    name = "fake"

    def __init__(self) -> None:
        self.seen_skip_keys: frozenset[str] | None = None

    async def fetch(self) -> FetchResult:
        self.seen_skip_keys = self.skip_product_keys
        return FetchResult(
            products=[
                ProductInfo(key="sqlserver-2016", display_name="SQL Server 2016 (renamed)", family="sql_server"),
                ProductInfo(key="sqlserver-2022", display_name="SQL Server 2022", family="sql_server"),
            ],
            patches=[_patch("sqlserver-2016", "KB-NEW-2016"), _patch("sqlserver-2022", "KB-NEW-2022")],
            patch_kb_hints={("sqlserver-2016", "2026-07"): ("KB-HINT", None)},
        )


async def _stored_kbs() -> set[str]:
    async with async_session_factory() as session:
        return set((await session.execute(select(Patch.kb_number))).scalars().all())


async def test_find_outdated_products_uses_the_dashboard_rule(db_session, make_product, make_patch):
    await _seed_sql_server(make_product, make_patch)
    async with async_session_factory() as session:
        # A product without any patch is never outdated (nothing to compare)
        session.add(make_product("sqlserver-2025", display_name="SQL Server 2025", family="sql_server"))
        await session.commit()
        outdated = await find_outdated_products(session)
    assert [p.key for p in outdated] == ["sqlserver-2016"]


async def test_setting_defaults_to_off_and_can_be_toggled(db_session):
    async with async_session_factory() as session:
        assert await get_bool(session, FETCH_OUTDATED_PRODUCTS) is False
        await set_bool(session, FETCH_OUTDATED_PRODUCTS, True)
        await session.commit()
        assert await get_bool(session, FETCH_OUTDATED_PRODUCTS) is True
        await set_bool(session, FETCH_OUTDATED_PRODUCTS, False)
        await session.commit()
        assert await get_bool(session, FETCH_OUTDATED_PRODUCTS) is False


async def test_refresh_skips_outdated_products_by_default(db_session, make_product, make_patch, monkeypatch):
    await _seed_sql_server(make_product, make_patch)
    fetcher = _RecordingFetcher()
    monkeypatch.setattr(refresh_service, "get_fetchers", lambda: [fetcher])

    await refresh_service.run_all_fetchers(trigger="test")

    assert fetcher.seen_skip_keys == frozenset({"sqlserver-2016"})
    kbs = await _stored_kbs()
    assert "KB-NEW-2022" in kbs
    assert "KB-NEW-2016" not in kbs
    assert "KB-HINT" not in kbs
    async with async_session_factory() as session:
        old = (await session.execute(select(Product).where(Product.key == "sqlserver-2016"))).scalar_one()
        # stored data of a skipped product stays exactly as it was
        assert old.display_name == "SQL Server 2016"
        assert len((await session.execute(select(Patch).where(Patch.product_id == old.id))).scalars().all()) == 1


async def test_refresh_queries_outdated_products_when_enabled(db_session, make_product, make_patch, monkeypatch):
    await _seed_sql_server(make_product, make_patch)
    async with async_session_factory() as session:
        await set_bool(session, FETCH_OUTDATED_PRODUCTS, True)
        await session.commit()
    fetcher = _RecordingFetcher()
    monkeypatch.setattr(refresh_service, "get_fetchers", lambda: [fetcher])

    await refresh_service.run_all_fetchers(trigger="test")

    assert fetcher.seen_skip_keys == frozenset()
    assert {"KB-NEW-2016", "KB-NEW-2022"} <= await _stored_kbs()


async def test_first_run_on_empty_database_skips_nothing(db_session, monkeypatch):
    fetcher = _RecordingFetcher()
    monkeypatch.setattr(refresh_service, "get_fetchers", lambda: [fetcher])

    await refresh_service.run_all_fetchers(trigger="test")

    assert fetcher.seen_skip_keys == frozenset()


async def test_sql_server_fetcher_does_not_request_skipped_versions(mock_fetch):
    requested: list[str] = []
    page_2022 = load_fixture("sql_server", "sqlserver_2022.html")

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == SQL_URL.format(2022):
            return httpx.Response(200, text=page_2022)
        return httpx.Response(404)

    mock_fetch(handler)
    fetcher = SqlServerFetcher()
    fetcher.skip_product_keys = frozenset({"sqlserver-2016", "sqlserver-2017", "sqlserver-2019", "sqlserver-2025"})
    result = await fetcher.fetch()

    assert requested == [SQL_URL.format(2022)]
    assert [p.key for p in result.products] == ["sqlserver-2022"]
    assert result.errors == []


async def test_dotnet_fetcher_does_not_request_skipped_channels(mock_fetch):
    requested: list[str] = []
    index = load_fixture("dotnet", "releases_index.json")
    channel_9 = load_fixture("dotnet", "releases_9_0.json")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url == INDEX_URL:
            return httpx.Response(200, text=index)
        if url == DOTNET_CHANNEL_URL.format("9.0"):
            return httpx.Response(200, text=channel_9)
        return httpx.Response(404)

    mock_fetch(handler)
    fetcher = DotNetFetcher()
    fetcher.skip_product_keys = frozenset({"dotnet-1.0", "dotnet-3.1"})
    result = await fetcher.fetch()

    assert DOTNET_CHANNEL_URL.format("1.0") not in requested
    assert DOTNET_CHANNEL_URL.format("3.1") not in requested
    keys = {p.key for p in result.products}
    assert "dotnet-9.0" in keys
    assert not keys & {"dotnet-1.0", "dotnet-3.1"}
    assert not {p.product_key for p in result.patches} & {"dotnet-1.0", "dotnet-3.1"}


async def test_admin_page_shows_and_saves_the_setting(client, make_product, make_patch):
    await _seed_sql_server(make_product, make_patch)
    auth = ("admin", "change-me")

    page = (await client.get("/admin", auth=auth)).text
    assert 'name="fetch_outdated"' in page
    assert 'name="fetch_outdated" value="true" checked' not in page
    assert "Currently not queried:" in page
    assert "SQL Server 2016" in page.split("Currently not queried:")[1].split("</p>")[0]
    assert "SQL Server 2022" not in page.split("Currently not queried:")[1].split("</p>")[0]

    resp = await client.post("/admin/settings", data={"fetch_outdated": "true"}, auth=auth)
    assert resp.status_code == 303
    page = (await client.get("/admin", auth=auth)).text
    assert 'name="fetch_outdated" value="true" checked' in page
    assert "Outdated, but still queried:" in page

    # unchecked checkbox = field missing from the form
    await client.post("/admin/settings", data={}, auth=auth)
    async with async_session_factory() as session:
        assert await get_bool(session, FETCH_OUTDATED_PRODUCTS) is False


async def test_settings_require_auth(client):
    resp = await client.post("/admin/settings", data={"fetch_outdated": "true"})
    assert resp.status_code == 401
