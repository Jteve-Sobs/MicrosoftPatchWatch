# WindowsPatchWatch

Webseite, die den aktuellen Patch-Stand für **Windows 10/11 (inkl. LTSB/LTSC),
Windows Server, .NET Framework und .NET** anzeigt — inkl. Historie, mit
automatischer Hintergrund-Aktualisierung.

## Wie es funktioniert

- **Beim Seitenaufruf**: Die Seite zeigt sofort den zuletzt bekannten Stand aus
  der Datenbank (kein Warten auf externe Quellen). Im Hintergrund wird per
  HTMX ein Refresh angestoßen (debounced, siehe `MIN_REFRESH_INTERVAL_MINUTES`)
  — sobald neue Daten da sind, aktualisiert sich die Tabelle automatisch ohne
  Reload.
- **Von selbst**: Ein APScheduler-Job prüft unabhängig von Besuchern alle
  `FETCH_INTERVAL_HOURS` Stunden (Standard: 6) auf neue Stände.
- **Historie**: Jeder gefundene Patch wird als eigene Zeile gespeichert und nie
  gelöscht — "Verlauf" pro Produkt ist einfach die Liste aller je gesehenen
  Patches, sortiert nach Datum.

## Datenquellen

Microsofts RSS-Feed für Sicherheitsupdates ist abgeschaltet. Stattdessen:

| Quelle | Liefert | Methode |
|---|---|---|
| [Windows Release Health](https://learn.microsoft.com/en-us/windows/release-health/) (Microsoft Learn) | Windows 10/11 (inkl. LTSB/LTSC) & Windows Server: KB, Build, Datum | HTML-Scraping der "Update history"-Tabellen |
| [MSRC Security Update API](https://api.msrc.microsoft.com/cvrf/v2.0/updates) | .NET Framework Sicherheitsupdates (KB je Version) | Offizielles JSON-API (CVRF) |
| [dotnet/core releases-index.json](https://github.com/dotnet/core) | .NET (Core 5+) Releases inkl. Historie | Offizielles JSON auf GitHub |

Siehe `app/fetchers/` — jede Quelle ist ein eigener Fetcher mit eigenem
Docstring, der Annahmen und Grenzen der jeweiligen Quelle erklärt.

## Starten

```bash
cp .env.example .env   # Passwort anpassen
docker compose up -d --build
```

- App: http://localhost:8000
- Adminer (DB-Browser, optional): http://localhost:8081 — Server `db`, Login
  aus `.env`

Der erste Durchlauf startet automatisch beim Container-Start
(`FETCH_ON_STARTUP=true`) und befüllt die Datenbank; das dauert je nach
Quellen ein bis zwei Minuten.

## JSON-API

- `GET /api/products` — Liste aller erkannten Produkte/Versionen
- `GET /api/products/{key}/patches` — volle Historie eines Produkts
- `POST /refresh` — manuellen Refresh anstoßen (debounced)

## Bekannte Grenzen (bewusste Scope-Entscheidungen für v1)

- **Kein Alembic**: Das DB-Schema wird beim Start per `create_all` angelegt.
  Reicht für v1; sobald sich das Schema auf einer laufenden Instanz ändern
  soll, lohnt sich ein Umstieg auf echte Migrationen.
- **.NET Framework-Daten sind best-effort**: MSRC deckt nur *Security*-Updates
  ab, keine reinen Qualitäts-Rollups. Die Zuordnung Version → KB basiert auf
  Text-Parsing der MSRC-Produktnamen.
- **Scraping ist fragil**: Wenn Microsoft die Tabellenstruktur der
  Release-Health-Seiten ändert, findet der Fetcher nichts mehr für die
  betroffene Seite (wird als Fehler im `FetchRun` protokolliert, bricht aber
  nicht die ganze Aktualisierung ab). Ein Blick in die Logs / `/api/products`
  zeigt das schnell.
- **Severity/CVE** wird aktuell nicht mit den Windows-KB-Einträgen verknüpft
  (nur bei .NET Framework indirekt über MSRC vorhanden).

## Ideen für später

- **CVE/Severity-Anreicherung** der Windows-Einträge über MSRC verknüpfen
  (Build/KB → CVE-Liste, Schweregrad als zusätzliche Spalte/Badge)
- **Benachrichtigungen**: Webhook / E-Mail / Discord / ntfy bei neuen Patches
  für abonnierte Produkte
- **Diff-Ansicht**: "Was hat sich seit Build X geändert" zwischen zwei
  Ständen
- **Lifecycle/EOL-Daten** einblenden (Support-Ende je Version, farblich
  markieren wenn < 90 Tage)
- **Known-Issues-Rollup** je Version (aus den "Known issues"-Seiten von
  Microsoft Learn)
- **Export**: eigener RSS/Atom-Feed oder CSV-Export je Produkt, als Ersatz für
  den abgeschalteten Microsoft-Feed
- **Vergleichsansicht** mehrerer Versionen nebeneinander (z. B. alle
  Server-Versionen im Vergleich)
- **Suche/Filter serverseitig** statt nur client-seitig, plus Filter nach
  Update-Typ (Security/Preview/OOB)
- **Admin-Bereich** zum manuellen Nachtragen/Korrigieren einzelner Einträge
  (für die Fälle, in denen ein Scraper mal danebenliegt)
- **Tests** für die Parser (Fixtures mit eingefrorenem HTML/JSON der Quellen,
  damit ein Seitenumbau bei Microsoft schnell auffällt)
- **Alembic-Migrationen**, sobald das Schema wächst
- **Auth** fürs Adminer/Refresh-Endpoint, falls die Seite mal öffentlich
  erreichbar wird

## Projektstruktur

```
app/
  fetchers/            # eine Datei pro Datenquelle
  routers/             # web.py (HTML/HTMX), api.py (JSON)
  templates/            # Jinja2 + HTMX-Partials
  static/                # CSS/JS
  models.py               # SQLAlchemy: Product, Patch, FetchRun
  refresh_service.py       # Orchestrierung + Upsert-Logik
  scheduler.py               # APScheduler-Job
  main.py                     # FastAPI-App + Lifespan
```
