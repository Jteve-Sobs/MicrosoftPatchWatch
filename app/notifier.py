"""Push notifications for newly discovered patches, via ntfy
(https://ntfy.sh or a self-hosted instance — see https://ntfy.sh/docs/).

One push per refresh run, not per patch — refresh_service.run_all_fetchers
collects every patch that was genuinely new in that run and calls
notify_new_patches() once at the end, so a run that finds 12 new KBs sends
exactly one notification listing all 12, not 12 separate pushes.

Configure via NTFY_URL in .env (the full topic URL, e.g.
"https://ntfy.sh/my-private-topic" or a self-hosted
"https://ntfy.example.com/patchwatch"); NTFY_TOKEN and PUBLIC_BASE_URL are
optional. Leaving NTFY_URL unset disables notifications entirely — every
function here becomes a no-op.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import get_settings

logger = logging.getLogger("patchwatch.notifier")

# ntfy's own priority names (https://ntfy.sh/docs/publish/#message-priority) —
# only "urgent"/"high" are picked based on severity, everything else stays default.
_PRIORITY_BY_SEVERITY = {"critical": "urgent", "important": "high"}
_DEFAULT_PRIORITY = "default"

# Keep the push body readable even when a refresh (e.g. a new product's first
# fetch) turns up a lot of patches at once; the rest is summarized as "+N more".
_MAX_LINES = 20


@dataclass(slots=True)
class NewPatchNotice:
    """Just enough about one newly-inserted Patch row to render a
    notification line — decoupled from the ORM model so this module doesn't
    need a DB session."""

    product_display_name: str
    kb_number: str
    build: str
    title: str | None
    severity: str | None
    update_type: str | None


def _format_line(notice: NewPatchNotice) -> str:
    identifier = notice.kb_number or notice.build or "—"
    label = notice.title or notice.update_type or "Update"
    severity = f" [{notice.severity}]" if notice.severity else ""
    return f"• {notice.product_display_name}: {identifier} — {label}{severity}"


def _worst_severity(notices: list[NewPatchNotice]) -> str | None:
    worst: str | None = None
    for notice in notices:
        sev = (notice.severity or "").lower()
        if sev == "critical":
            return "critical"
        if sev == "important":
            worst = "important"
    return worst


async def notify_new_patches(notices: list[NewPatchNotice]) -> None:
    """Sends one ntfy push summarizing every patch that was new in this
    refresh run. No-op if NTFY_URL isn't configured or the list is empty.

    Any send failure is logged and swallowed — a misconfigured or unreachable
    notification target must never fail the refresh itself (see call site in
    refresh_service.run_all_fetchers)."""
    settings = get_settings()
    if not settings.ntfy_url or not notices:
        return

    lines = [_format_line(n) for n in notices[:_MAX_LINES]]
    if len(notices) > _MAX_LINES:
        lines.append(f"… and {len(notices) - _MAX_LINES} more")
    body = "\n".join(lines)

    worst = _worst_severity(notices)
    count_label = "patch" if len(notices) == 1 else "patches"
    headers = {
        "Title": f"MicrosoftPatchWatch: {len(notices)} new {count_label}",
        "Priority": _PRIORITY_BY_SEVERITY.get(worst, _DEFAULT_PRIORITY),
        "Tags": "warning" if worst else "package",
    }
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    if settings.public_base_url:
        headers["Click"] = settings.public_base_url

    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.post(settings.ntfy_url, content=body.encode("utf-8"), headers=headers)
            response.raise_for_status()
    except Exception:  # noqa: BLE001 - a broken notify target must not break refreshes
        logger.exception("Failed to send ntfy notification for %s new patch(es)", len(notices))
