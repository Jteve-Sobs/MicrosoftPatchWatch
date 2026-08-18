"""Route tests for app/routers/web.py — the public dashboard, its partials,
and the JSON export added on top of it. Uses the `client`/`db_session`/
`make_product`/`make_patch` fixtures from conftest.py (SQLite-backed, no
lifespan — see the fixtures' docstrings for why)."""

from __future__ import annotations

import datetime as dt

from app.database import async_session_factory


async def _seed(make_product, make_patch, *, product_overrides=None, patches=()):
    """Inserts one product plus the given patches (list of override dicts,
    newest-first doesn't matter — routes sort themselves) and returns the
    product's id."""
    async with async_session_factory() as session:
        product = make_product("win11-24h2", **(product_overrides or {}))
        session.add(product)
        await session.flush()
        for overrides in patches:
            session.add(make_patch(product.id, **overrides))
        await session.commit()
        return product.id


async def test_index_lists_seeded_product(client, make_product, make_patch):
    await _seed(
        make_product,
        make_patch,
        product_overrides={"display_name": "Windows 11, version 24H2", "family": "windows_client"},
        patches=[{"kb_number": "KB5041160", "release_date": dt.date(2026, 8, 12)}],
    )

    resp = await client.get("/")

    assert resp.status_code == 200
    assert "Windows 11, version 24H2" in resp.text
    assert "KB5041160" in resp.text


async def test_index_shows_empty_state_with_no_products(client):
    resp = await client.get("/")

    assert resp.status_code == 200
    assert "No data yet" in resp.text


async def test_product_history_returns_patches_newest_first(client, make_product, make_patch):
    product_id = await _seed(
        make_product,
        make_patch,
        patches=[
            {"kb_number": "KB1", "release_date": dt.date(2026, 6, 1)},
            {"kb_number": "KB2", "release_date": dt.date(2026, 8, 1)},
        ],
    )
    assert product_id

    resp = await client.get("/product/win11-24h2/history")

    assert resp.status_code == 200
    # KB2 (newer) must come before KB1 in the rendered markup.
    assert resp.text.index("KB2") < resp.text.index("KB1")


async def test_product_history_unknown_key_is_404(client):
    resp = await client.get("/product/does-not-exist/history")

    assert resp.status_code == 404


async def test_export_json_all_scope_includes_full_history(client, make_product, make_patch):
    await _seed(
        make_product,
        make_patch,
        product_overrides={"display_name": "Windows 11, version 24H2"},
        patches=[
            {"kb_number": "KB_OLD", "release_date": dt.date(2020, 1, 1)},
            {"kb_number": "KB_NEW", "release_date": dt.date(2026, 8, 1)},
        ],
    )

    resp = await client.get("/export/json", params={"scope": "all"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["scope"] == "all"
    assert len(data["products"]) == 1
    kbs = {p["kb"] for p in data["products"][0]["patches"]}
    assert kbs == {"KB_OLD", "KB_NEW"}


async def test_export_json_month_scope_filters_to_current_month(client, make_product, make_patch):
    today = dt.date.today()
    last_month = (today.replace(day=1) - dt.timedelta(days=1))
    await _seed(
        make_product,
        make_patch,
        patches=[
            {"kb_number": "KB_THIS_MONTH", "release_date": today},
            {"kb_number": "KB_LAST_MONTH", "release_date": last_month},
        ],
    )

    resp = await client.get("/export/json", params={"scope": "month"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["scope"] == "month"
    assert len(data["products"]) == 1
    kbs = {p["kb"] for p in data["products"][0]["patches"]}
    assert kbs == {"KB_THIS_MONTH"}


async def test_export_json_month_scope_omits_products_with_no_match_this_month(
    client, make_product, make_patch
):
    async with async_session_factory() as session:
        product = make_product("dotnet-8", display_name=".NET 8.0", family="dotnet")
        session.add(product)
        await session.flush()
        session.add(make_patch(product.id, kb_number="", build="8.0.1", release_date=dt.date(2020, 1, 1)))
        await session.commit()

    resp = await client.get("/export/json", params={"scope": "month"})

    assert resp.status_code == 200
    assert resp.json()["products"] == []


async def test_export_json_unknown_scope_falls_back_to_all(client, make_product, make_patch):
    await _seed(make_product, make_patch, patches=[{"kb_number": "KB1", "release_date": dt.date(2020, 1, 1)}])

    resp = await client.get("/export/json", params={"scope": "bogus"})

    assert resp.status_code == 200
    assert resp.json()["scope"] == "all"
    assert len(resp.json()["products"]) == 1


async def test_lang_switch_sets_cookie_and_redirects_to_safe_next(client):
    resp = await client.get("/lang/de", params={"next": "/admin"}, follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin"
    assert resp.cookies.get("lang") == "de"


async def test_lang_switch_rejects_offsite_next_as_open_redirect(client):
    resp = await client.get(
        "/lang/de", params={"next": "https://evil.example/phish"}, follow_redirects=False
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


async def test_lang_switch_unsupported_code_falls_back_to_default(client):
    resp = await client.get("/lang/xx", follow_redirects=False)

    assert resp.cookies.get("lang") == "en"
