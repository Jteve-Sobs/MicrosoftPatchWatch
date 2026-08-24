"""Tests for app.notifier — the ntfy push-notification module.

app.notifier builds its own httpx.AsyncClient (like BaseFetcher does), so —
mirroring the mock_fetch fixture in conftest.py — these tests monkeypatch
httpx.AsyncClient inside app.notifier to route through an httpx.MockTransport
instead of hitting the network.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from app import notifier
from app.config import get_settings
from app.notifier import NewPatchNotice, notify_fetch_errors, notify_new_patches


@pytest.fixture(autouse=True)
def _reset_settings_cache(monkeypatch):
    """get_settings() is @lru_cache'd, so changes to NTFY_* env vars within a
    test wouldn't otherwise take effect. Clear before and after every test —
    after, too, so a later test file doesn't inherit a cached Settings
    instance built with these env vars still set."""
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("NTFY_URL", raising=False)
    monkeypatch.delenv("NTFY_TOKEN", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    get_settings.cache_clear()


class _CapturedRequests:
    """Records every request the patched client sent. `status_code` is
    mutable so a test can flip it (e.g. to 500) before calling
    notify_new_patches."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status_code = 200


@pytest.fixture
def mock_ntfy(monkeypatch):
    """Patches httpx.AsyncClient inside app.notifier so requests are captured
    instead of sent. Returns a _CapturedRequests instance."""
    captured = _CapturedRequests()

    def handler(request: httpx.Request) -> httpx.Response:
        captured.requests.append(request)
        return httpx.Response(captured.status_code)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(notifier.httpx, "AsyncClient", fake_async_client)
    return captured


def _notice(**overrides) -> NewPatchNotice:
    defaults = dict(
        product_display_name="Windows 11, version 24H2",
        kb_number="KB5000001",
        build="26100.1000",
        title="Cumulative Update",
        severity=None,
        update_type="Security",
        release_date=dt.date(2026, 8, 12),
    )
    defaults.update(overrides)
    return NewPatchNotice(**defaults)


async def test_noop_without_ntfy_url(mock_ntfy, monkeypatch):
    # setenv("", ...) rather than delenv: Settings also reads a real .env
    # file on disk (see app/config.py's model_config), so simply unsetting
    # the process env var wouldn't shadow a value someone has configured
    # there for actual local use — an empty string does.
    monkeypatch.setenv("NTFY_URL", "")
    get_settings.cache_clear()

    await notify_new_patches([_notice()])

    assert mock_ntfy.requests == []


async def test_noop_with_no_new_patches(mock_ntfy, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()

    await notify_new_patches([])

    assert mock_ntfy.requests == []


async def test_sends_one_push_summarizing_all_notices(mock_ntfy, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()

    await notify_new_patches(
        [_notice(), _notice(kb_number="KB5000002", severity="Critical")]
    )

    assert len(mock_ntfy.requests) == 1
    request = mock_ntfy.requests[0]
    assert str(request.url) == "https://ntfy.example.com/patchwatch"
    assert request.headers["Title"] == "MicrosoftPatchWatch: 2 new patches"
    # Priority/tag get bumped to the worst severity across all notices.
    assert request.headers["Priority"] == "urgent"
    assert request.headers["Tags"] == "warning"
    assert "Authorization" not in request.headers

    body = request.read().decode("utf-8")
    assert "KB5000001" in body
    assert "KB5000002" in body
    assert "[Critical]" in body
    assert "(2026-08-12)" in body


async def test_omits_date_when_release_date_is_missing(mock_ntfy, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()

    await notify_new_patches([_notice(release_date=None)])

    body = mock_ntfy.requests[0].read().decode("utf-8")
    assert "(" not in body


async def test_singular_title_and_default_priority_without_severity(mock_ntfy, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()

    await notify_new_patches([_notice()])

    request = mock_ntfy.requests[0]
    assert request.headers["Title"] == "MicrosoftPatchWatch: 1 new patch"
    assert request.headers["Priority"] == "default"
    assert request.headers["Tags"] == "package"


async def test_caps_body_at_max_lines(mock_ntfy, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()

    notices = [_notice(kb_number=f"KB{5000000 + i}") for i in range(25)]
    await notify_new_patches(notices)

    body = mock_ntfy.requests[0].read().decode("utf-8")
    assert body.count("KB") == 20  # 20 listed lines
    assert "… and 5 more" in body


async def test_sends_optional_token_and_click_headers(mock_ntfy, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    monkeypatch.setenv("NTFY_TOKEN", "s3cr3t")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://patchwatch.example.com")
    get_settings.cache_clear()

    await notify_new_patches([_notice()])

    request = mock_ntfy.requests[0]
    assert request.headers["Authorization"] == "Bearer s3cr3t"
    assert request.headers["Click"] == "https://patchwatch.example.com"


async def test_send_failure_is_swallowed_not_raised(mock_ntfy, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()
    mock_ntfy.status_code = 500

    # Must not raise — a broken notify target must never fail a refresh.
    await notify_new_patches([_notice()])


# --- notify_fetch_errors --------------------------------------------------


async def test_fetch_errors_noop_without_ntfy_url(mock_ntfy, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "")
    get_settings.cache_clear()

    await notify_fetch_errors(["fake: boom"], status="error")

    assert mock_ntfy.requests == []


async def test_fetch_errors_noop_with_no_errors(mock_ntfy, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()

    await notify_fetch_errors([], status="error")

    assert mock_ntfy.requests == []


async def test_fetch_errors_total_failure_is_urgent(mock_ntfy, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()

    await notify_fetch_errors(["windows-release-health: table not found"], status="error")

    assert len(mock_ntfy.requests) == 1
    request = mock_ntfy.requests[0]
    assert request.headers["Title"] == "MicrosoftPatchWatch: all sources failed"
    assert request.headers["Priority"] == "urgent"
    assert request.headers["Tags"] == "rotating_light"
    assert "windows-release-health: table not found" in request.read().decode("utf-8")


async def test_fetch_errors_partial_failure_is_high_not_urgent(mock_ntfy, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()

    await notify_fetch_errors(["sql-server: boom"], status="partial")

    request = mock_ntfy.requests[0]
    assert request.headers["Title"] == "MicrosoftPatchWatch: a source failed"
    assert request.headers["Priority"] == "high"


async def test_fetch_errors_caps_body_at_max_lines(mock_ntfy, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()

    errors = [f"fake: error {i}" for i in range(25)]
    await notify_fetch_errors(errors, status="error")

    body = mock_ntfy.requests[0].read().decode("utf-8")
    assert body.count("error ") == 20
    assert "… and 5 more" in body


async def test_fetch_errors_send_failure_is_swallowed_not_raised(mock_ntfy, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/patchwatch")
    get_settings.cache_clear()
    mock_ntfy.status_code = 500

    # Must not raise — a broken notify target must never fail a refresh.
    await notify_fetch_errors(["fake: boom"], status="error")
