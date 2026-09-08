"""Route tests for app/routers/web.py — the public dashboard, its partials,
and the JSON export added on top of it. Uses the `client`/`db_session`/
`make_product`/`make_patch` fixtures from conftest.py (SQLite-backed, no
lifespan — see the fixtures' docstrings for why)."""

from __future__ import annotations

import datetime as dt

from app.database import async_session_factory
from app.models import FetchRun


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


async def test_export_json_latest_scope_keeps_only_each_products_newest_release_date(
    client, make_product, make_patch
):
    """Regression test: this used to be a "current calendar month" filter, so
    a product whose newest patch shipped last month vanished from the export
    entirely just because "now" had rolled into a new month. "latest" instead
    tracks each product's own most recent release date, whenever that was."""
    today = dt.date.today()
    last_month = (today.replace(day=1) - dt.timedelta(days=1))
    await _seed(
        make_product,
        make_patch,
        patches=[
            {"kb_number": "KB_NEWEST", "release_date": last_month},
            {"kb_number": "KB_OLDER", "release_date": dt.date(2020, 1, 1)},
        ],
    )

    resp = await client.get("/export/json", params={"scope": "latest"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["scope"] == "latest"
    assert len(data["products"]) == 1
    kbs = {p["kb"] for p in data["products"][0]["patches"]}
    assert kbs == {"KB_NEWEST"}


async def test_export_json_exclude_preview_drops_preview_patches(client, make_product, make_patch):
    await _seed(
        make_product,
        make_patch,
        patches=[
            {"kb_number": "KB_SECURITY", "release_date": dt.date(2026, 8, 1), "update_type": "Security"},
            {"kb_number": "KB_PREVIEW", "release_date": dt.date(2026, 8, 15), "update_type": "Preview"},
        ],
    )

    resp = await client.get("/export/json", params={"scope": "all", "exclude_preview": "true"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["exclude_preview"] is True
    kbs = {p["kb"] for p in data["products"][0]["patches"]}
    assert kbs == {"KB_SECURITY"}


async def test_export_json_latest_scope_with_exclude_preview_skips_newer_preview_patch(
    client, make_product, make_patch
):
    """The newest patch overall is a preview build; with exclude_preview the
    "latest" one reported should be the newest *non-preview* patch instead,
    not an empty result."""
    await _seed(
        make_product,
        make_patch,
        patches=[
            {"kb_number": "KB_SECURITY", "release_date": dt.date(2026, 8, 1), "update_type": "Security"},
            {"kb_number": "KB_PREVIEW", "release_date": dt.date(2026, 8, 15), "update_type": "Preview"},
        ],
    )

    resp = await client.get(
        "/export/json", params={"scope": "latest", "exclude_preview": "true"}
    )

    assert resp.status_code == 200
    kbs = {p["kb"] for p in resp.json()["products"][0]["patches"]}
    assert kbs == {"KB_SECURITY"}


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


async def _seed_fetch_runs(rows: list[dict]) -> None:
    async with async_session_factory() as session:
        for overrides in rows:
            defaults = {"status": "success", "trigger": "scheduler", "new_patches": 0, "updated_products": 0}
            session.add(FetchRun(**{**defaults, **overrides}))
        await session.commit()


async def test_status_partial_picks_the_latest_run_by_id_not_by_started_at(client):
    """Regression guard for the real 2036 case: a run's started_at is only
    as trustworthy as the system clock was at the moment it kicked off — a
    one-off bad clock reading (RTC/NTP glitch) can leave a run dated further
    in the future than any real run will ever reach, poisoning an
    order-by-started_at query forever. Ordering by id (a monotonic serial,
    immune to the clock) instead means a later real run always wins, however
    the earlier one was dated."""
    await _seed_fetch_runs(
        [
            # This id is lower (earlier, in reality) but its started_at is
            # absurdly far in the future — exactly what a clock glitch
            # produces. If the query ordered by started_at, this one would
            # incorrectly keep "winning" forever.
            {
                "started_at": dt.datetime(2036, 2, 2, 1, 44, tzinfo=dt.timezone.utc),
                "finished_at": dt.datetime(2036, 2, 2, 1, 44, tzinfo=dt.timezone.utc),
                "status": "error",
                "error": "certificate has expired",
            },
            {
                "started_at": dt.datetime(2026, 9, 8, 20, 29, tzinfo=dt.timezone.utc),
                "finished_at": dt.datetime(2026, 9, 8, 20, 29, tzinfo=dt.timezone.utc),
                "status": "success",
            },
        ]
    )

    resp = await client.get("/partials/status")

    assert resp.status_code == 200
    assert "2036" not in resp.text
    assert "status-success" in resp.text
    assert "status-error" not in resp.text
