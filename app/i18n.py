"""Minimal, dependency-free i18n: a plain dict of strings per locale plus a
locale-aware date formatter. Two languages is small enough that pulling in
gettext/Babel would be more ceremony than value — this is just a lookup.

Locale resolution order: explicit cookie (set by /lang/{code}) > browser's
Accept-Language header > default (English).
"""

from __future__ import annotations

import datetime as dt

from fastapi import Request

SUPPORTED_LOCALES = ("en", "de")
DEFAULT_LOCALE = "en"

# Per-locale strftime pattern — this is the "date format is also relevant"
# part: English shows "Aug 11, 2026", German shows "11.08.2026".
DATE_FORMATS = {"en": "%b %d, %Y", "de": "%d.%m.%Y"}

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "app.title": "WindowsPatchWatch",
        "intro.title": "Current patch status",
        "intro.subtitle": (
            "Windows 10 / 11 (incl. LTSB/LTSC), Windows Server, and .NET Framework / .NET "
            "— refreshed automatically in the background."
        ),
        "btn.refresh": "Refresh now",
        "filter.placeholder": "Filter… e.g. 24H2, KB512…, Security, or any past KB/build/title",
        "filter.clear": "Clear search",
        "nav.hidden": "Hidden",
        "nav.hidden_none": "No products hidden.",
        "nav.show": "Show",
        "table.version": "Version",
        "table.latest_kb": "Latest KB",
        "table.build": "Build",
        "table.type": "Type",
        "table.date": "Date",
        "table.eol": "End of life",
        "table.history": "History",
        "table.hide": "Hide product",
        "eol.ended": "Support ended",
        "eol.ended_label": "Ended",
        "eol.ending_soon": "Support ends soon",
        "history.date": "Date",
        "history.kb": "KB",
        "history.build": "Build",
        "history.type": "Type",
        "history.title": "Title",
        "history.empty": "No history yet.",
        "status.checking": "Checking…",
        "status.last_check": "Last check",
        "status.none": "No check has run yet.",
        "status.new": "new",
        "status.refresh_running": "A check is already running…",
        "status.refresh_debounced": "Already checked recently — try again shortly.",
        "family.windows_client": "Windows (Client)",
        "family.windows_server": "Windows Server",
        "family.dotnet_framework": ".NET Framework",
        "family.dotnet": ".NET",
        "footer.sources": "Sources",
        "footer.disclaimer": "Not an official Microsoft product.",
        "empty.no_data": (
            "No data yet — the first check is running in the background. "
            "This page updates automatically once results are in."
        ),
        "badge.security": "Security",
        "badge.preview": "Preview",
        "badge.out-of-band": "Out-of-Band",
        "badge.update": "Update",
        "badge.ltsc": "LTSC",
        "badge.server": "Server",
        "nav.admin": "Admin",
        "admin.title": "Admin",
        "admin.intro": (
            "Manually correct, add, or delete individual patch entries — for "
            "when a scraper gets one wrong. Changes here are protected from "
            "being overwritten by the next automatic refresh."
        ),
        "admin.force_refresh": "Refresh now (skip debounce)",
        "admin.refresh_started": "Refresh started.",
        "admin.back": "← Back to admin overview",
        "admin.new_patch": "+ New entry",
        "admin.edit": "Edit",
        "admin.delete": "Delete",
        "admin.delete_confirm": "Delete this entry? This cannot be undone.",
        "admin.manual_badge": "Manual",
        "admin.save": "Save",
        "admin.cancel": "Cancel",
        "admin.no_patches": "No entries yet.",
        "admin.patches_suffix": "entries",
        "admin.field.date": "Date",
        "admin.field.kb": "KB number",
        "admin.field.build": "Build",
        "admin.field.title": "Title",
        "admin.field.type": "Type",
        "admin.field.severity": "Severity",
        "admin.field.kb_url": "KB link",
        "admin.field.date_hint": "YYYY-MM-DD, leave empty if unknown",
        "admin.manually_edited_label": "Protect from automatic sync",
        "admin.manually_edited_hint": (
            "While checked, the next scraper refresh won't overwrite this entry's "
            "title/severity. Uncheck to let it sync normally again."
        ),
    },
    "de": {
        "app.title": "WindowsPatchWatch",
        "intro.title": "Aktueller Patch-Stand",
        "intro.subtitle": (
            "Windows 10 / 11 (inkl. LTSB/LTSC), Windows Server sowie .NET Framework und .NET "
            "— wird automatisch im Hintergrund aktualisiert."
        ),
        "btn.refresh": "Jetzt aktualisieren",
        "filter.placeholder": "Filtern… z. B. 24H2, KB512…, Sicherheit, oder jedes frühere KB/Build/Titel",
        "filter.clear": "Suche leeren",
        "nav.hidden": "Ausgeblendet",
        "nav.hidden_none": "Keine Produkte ausgeblendet.",
        "nav.show": "Einblenden",
        "table.version": "Version",
        "table.latest_kb": "Aktuelles KB",
        "table.build": "Build",
        "table.type": "Typ",
        "table.date": "Datum",
        "table.eol": "Support-Ende",
        "table.history": "Verlauf",
        "table.hide": "Produkt ausblenden",
        "eol.ended": "Support beendet",
        "eol.ended_label": "Beendet",
        "eol.ending_soon": "Support endet bald",
        "history.date": "Datum",
        "history.kb": "KB",
        "history.build": "Build",
        "history.type": "Typ",
        "history.title": "Titel",
        "history.empty": "Noch keine Historie vorhanden.",
        "status.checking": "Aktualisiere…",
        "status.last_check": "Letzte Prüfung",
        "status.none": "Noch keine Prüfung durchgeführt.",
        "status.new": "neu",
        "status.refresh_running": "Es läuft schon eine Prüfung…",
        "status.refresh_debounced": "Gerade erst geprüft — bitte gleich nochmal versuchen.",
        "family.windows_client": "Windows (Client)",
        "family.windows_server": "Windows Server",
        "family.dotnet_framework": ".NET Framework",
        "family.dotnet": ".NET",
        "footer.sources": "Quellen",
        "footer.disclaimer": "Kein offizielles Microsoft-Produkt.",
        "empty.no_data": (
            "Noch keine Daten vorhanden — die erste Prüfung läuft im Hintergrund. "
            "Diese Seite aktualisiert sich automatisch, sobald Ergebnisse da sind."
        ),
        "badge.security": "Sicherheit",
        "badge.preview": "Vorschau",
        "badge.out-of-band": "Außerplanmäßig",
        "badge.update": "Update",
        "badge.ltsc": "LTSC",
        "badge.server": "Server",
        "nav.admin": "Admin",
        "admin.title": "Admin",
        "admin.intro": (
            "Einzelne Patch-Einträge manuell korrigieren, hinzufügen oder löschen — "
            "für die Fälle, in denen ein Scraper mal danebenliegt. Änderungen "
            "hier werden vor dem nächsten automatischen Refresh geschützt."
        ),
        "admin.force_refresh": "Jetzt aktualisieren (Debounce ignorieren)",
        "admin.refresh_started": "Aktualisierung gestartet.",
        "admin.back": "← Zurück zur Admin-Übersicht",
        "admin.new_patch": "+ Neuer Eintrag",
        "admin.edit": "Bearbeiten",
        "admin.delete": "Löschen",
        "admin.delete_confirm": "Diesen Eintrag löschen? Das kann nicht rückgängig gemacht werden.",
        "admin.manual_badge": "Manuell",
        "admin.save": "Speichern",
        "admin.cancel": "Abbrechen",
        "admin.no_patches": "Noch keine Einträge.",
        "admin.patches_suffix": "Einträge",
        "admin.field.date": "Datum",
        "admin.field.kb": "KB-Nummer",
        "admin.field.build": "Build",
        "admin.field.title": "Titel",
        "admin.field.type": "Typ",
        "admin.field.severity": "Schweregrad",
        "admin.field.kb_url": "KB-Link",
        "admin.field.date_hint": "JJJJ-MM-TT, leer lassen wenn unbekannt",
        "admin.manually_edited_label": "Vor automatischer Synchronisierung schützen",
        "admin.manually_edited_hint": (
            "Solange aktiv, überschreibt der nächste Scraper-Refresh Titel/Schweregrad "
            "dieses Eintrags nicht. Häkchen entfernen, damit er wieder normal "
            "synchronisiert wird."
        ),
    },
}


def translate(locale: str, key: str) -> str:
    return TRANSLATIONS.get(locale, {}).get(key) or TRANSLATIONS[DEFAULT_LOCALE].get(key, key)


def format_date(locale: str, value: dt.date | None) -> str:
    if value is None:
        return "–"
    return value.strftime(DATE_FORMATS.get(locale, DATE_FORMATS[DEFAULT_LOCALE]))


def format_datetime(locale: str, value: dt.datetime | None) -> str:
    if value is None:
        return "–"
    pattern = DATE_FORMATS.get(locale, DATE_FORMATS[DEFAULT_LOCALE]) + " %H:%M"
    return value.strftime(pattern)


def eol_status(value: dt.date | None, *, today: dt.date | None = None) -> str | None:
    """Classifies a support_end_date for UI coloring: "ended" (already past),
    "soon" (within ~90 days) or "ok" (further out). None if unknown."""
    if value is None:
        return None
    today = today or dt.date.today()
    if value < today:
        return "ended"
    if (value - today).days <= 90:
        return "soon"
    return "ok"


def resolve_locale(request: Request) -> str:
    cookie_lang = request.cookies.get("lang")
    if cookie_lang in SUPPORTED_LOCALES:
        return cookie_lang

    accept_language = request.headers.get("accept-language", "")
    for part in accept_language.split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in SUPPORTED_LOCALES:
            return code

    return DEFAULT_LOCALE
