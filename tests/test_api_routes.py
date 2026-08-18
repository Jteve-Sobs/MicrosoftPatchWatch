"""Route tests for app/routers/api.py — the small read-only JSON API."""

from __future__ import annotations

import datetime as dt

from app.database import async_session_factory


async def test_list_products_returns_known_fields(client, make_product):
    async with async_session_factory() as session:
        session.add(
            make_product(
                "win11-24h2",
                display_name="Windows 11, version 24H2",
                family="windows_client",
                support_end_date=dt.date(2027, 10, 12),
            )
        )
        await session.commit()

    resp = await client.get("/api/products")

    assert resp.status_code == 200
    data = resp.json()
    assert data == [
        {
            "key": "win11-24h2",
            "display_name": "Windows 11, version 24H2",
            "family": "windows_client",
            "is_ltsc": False,
            "source_url": None,
            "support_end_date": "2027-10-12",
            "support_ended": False,
        }
    ]


async def test_list_patches_for_known_product(client, make_product, make_patch):
    async with async_session_factory() as session:
        product = make_product("win11-24h2")
        session.add(product)
        await session.flush()
        session.add(make_patch(product.id, kb_number="KB1", release_date=dt.date(2026, 1, 1)))
        session.add(make_patch(product.id, kb_number="KB2", release_date=dt.date(2026, 8, 1)))
        await session.commit()

    resp = await client.get("/api/products/win11-24h2/patches")

    assert resp.status_code == 200
    data = resp.json()
    assert [p["kb_number"] for p in data] == ["KB2", "KB1"]  # newest first


async def test_list_patches_for_unknown_product_returns_error(client):
    resp = await client.get("/api/products/does-not-exist/patches")

    assert resp.status_code == 200  # the route doesn't 404, it returns {"error": ...}
    assert resp.json() == {"error": "not found"}
