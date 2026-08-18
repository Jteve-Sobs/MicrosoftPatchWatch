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
