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
        "filter.placeholder": "Filter… e.g. 24H2, Server 2022, KB512…",
        "nav.hidden": "Hidden",
        "nav.hidden_none": "No products hidden.",
        "nav.show": "Show",
        "table.version": "Version",
        "table.latest_kb": "Latest KB",
        "table.build": "Build",
        "table.type": "Type",
        "table.date": "Date",
        "table.history": "History",
        "table.hide": "Hide product",
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
    },
    "de": {
        "app.title": "WindowsPatchWatch",
        "intro.title": "Aktueller Patch-Stand",
        "intro.subtitle": (
            "Windows 10 / 11 (inkl. LTSB/LTSC), Windows Server sowie .NET Framework und .NET "
            "— wird automatisch im Hintergrund aktualisiert."
        ),
        "btn.refresh": "Jetzt aktualisieren",
        "filter.placeholder": "Filtern… z. B. 24H2, Server 2022, KB512…",
        "nav.hidden": "Ausgeblendet",
        "nav.hidden_none": "Keine Produkte ausgeblendet.",
        "nav.show": "Einblenden",
        "table.version": "Version",
        "table.latest_kb": "Aktuelles KB",
        "table.build": "Build",
        "table.type": "Typ",
        "table.date": "Datum",
        "table.history": "Verlauf",
        "table.hide": "Produkt ausblenden",
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
