"""Auth-gating tests for app/routers/admin.py — the whole area is protected
by HTTP Basic Auth (see require_admin in admin.py); this just checks that
gate actually holds, not the admin UI's functionality in depth."""

from __future__ import annotations


async def test_admin_index_without_credentials_is_401(client):
    resp = await client.get("/admin")

    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Basic"


async def test_admin_index_with_wrong_password_is_401(client):
    resp = await client.get("/admin", auth=("admin", "wrong-password"))

    assert resp.status_code == 401


async def test_admin_index_with_wrong_username_is_401(client):
    resp = await client.get("/admin", auth=("not-admin", "change-me"))

    assert resp.status_code == 401


async def test_admin_index_with_correct_credentials_succeeds(client):
    # "change-me" is Settings.admin_password's default (see app/config.py);
    # .env doesn't override it, and DATABASE_URL is the only env var these
    # tests set (see conftest.py), so it's safe to rely on here.
    resp = await client.get("/admin", auth=("admin", "change-me"))

    assert resp.status_code == 200


async def test_admin_product_patches_unknown_key_is_404(client):
    resp = await client.get("/admin/products/does-not-exist", auth=("admin", "change-me"))

    assert resp.status_code == 404


async def test_admin_logs_requires_auth(client):
    resp = await client.get("/admin/logs")

    assert resp.status_code == 401


async def test_admin_logs_lists_runs_newest_first_with_details(client):
    import datetime as dt

    from app.database import async_session_factory
    from app.models import FetchRun, FetchRunSource

    t0 = dt.datetime(2026, 10, 4, 9, 0, tzinfo=dt.timezone.utc)
    async with async_session_factory() as session:
        old = FetchRun(trigger="old-run", status="success", started_at=t0, finished_at=t0)
        new = FetchRun(trigger="new-run", status="partial", started_at=t0, finished_at=t0 + dt.timedelta(seconds=95))
        session.add_all([old, new])
        await session.flush()
        session.add(
            FetchRunSource(
                run_id=new.id, fetcher="sql-server", status="partial", started_at=t0, finished_at=t0,
                errors="sql-server: failed to process SQL Server 2017: 403 Forbidden",
                log="09:29:29 INFO patchwatch.fetchers.http: GET ... returned 403, retry 1/3",
                new_patches=1,
                new_patch_list='[{"product": "SQL Server 2022", "kb_number": "KB5099999", "build": "16.0.4999.1", '
                '"title": "CU25", "update_type": "Update", "severity": null, "release_date": "2026-10-01"}]',
            )
        )
        await session.commit()

    resp = await client.get("/admin/logs", auth=("admin", "change-me"))

    assert resp.status_code == 200
    html = resp.text
    assert html.index("new-run") < html.index("old-run")
    assert "SQL Server 2017: 403 Forbidden" in html
    assert "returned 403, retry 1/3" in html
    assert "1 min 35 s" in html
    assert "KB5099999" in html and "16.0.4999.1" in html
