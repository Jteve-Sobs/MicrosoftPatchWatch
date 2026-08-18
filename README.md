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

## Backups

Der `db-backup`-Service (`prodrigestivill/postgres-backup-local`) läuft
automatisch mit und legt gzip'te `pg_dump`-Dateien unter `./backups/` ab —
Standard ist ein täglicher Dump, mit Rotation (Default: 30 Tage, 12 Wochen,
12 Monate, konfigurierbar über `BACKUP_KEEP_*` / `BACKUP_SCHEDULE` in `.env`).
Es gibt Unterordner `daily/`, `weekly/`, `monthly/` sowie `last/` mit dem
jeweils neuesten Dump. Die Dateien sind reine `.sql.gz`, also unabhängig vom
Backup-Image mit jedem Postgres restorebar.

**Restore** (überschreibt die laufende DB — Container vorher stoppen oder auf
eine leere DB zielen):

```bash
gunzip -c backups/daily/patchwatch-<TIMESTAMP>.sql.gz | \
  docker compose exec -T db psql -U ${POSTGRES_USER:-patchwatch} -d ${POSTGRES_DB:-patchwatch}
```

`./backups/` liegt nur lokal auf dieser Maschine (nicht in Git, siehe
`.gitignore`) — für eine Offsite-Kopie den Ordner selbst z.B. per `rclone`
oder `restic` an ein weiteres Ziel syncen.

## JSON-API

- `GET /api/products` — Liste aller erkannten Produkte/Versionen
- `GET /api/products/{key}/patches` — volle Historie eines Produkts
- `POST /refresh` — manuellen Refresh anstoßen (debounced)
- `GET /export/json?scope=all|month` — alle Patches (bzw. nur die des laufenden
  Kalendermonats) als JSON, gruppiert nach Produkt. Auf der Startseite über die
  Buttons "Alles exportieren" / "Aktuellen Monat exportieren" erreichbar, die
  das Ergebnis direkt in die Zwischenablage kopieren statt es herunterzuladen.

## Admin-Bereich

`/admin` (Link im Footer) — für die Fälle, in denen ein Scraper mal
danebenliegt: einzelne Patch-Einträge von Hand anlegen, korrigieren oder
löschen. Geschützt per HTTP-Basic-Auth (Nutzername `admin`, Passwort aus
`ADMIN_PASSWORD` in `.env` — unbedingt vor jedem öffentlichen Deploy ändern,
Default ist `change-me`).

Ein Eintrag, der hier bearbeitet oder neu angelegt wird, bekommt
`manually_edited = true` und wird von künftigen automatischen Refreshes nicht
mehr überschrieben (nur `last_seen_at` wird weiter aktualisiert) — sonst würde
der nächste Scraper-Lauf die Korrektur einfach wieder zurücksetzen.

`POST /admin/refresh` stößt — anders als der normale "Jetzt
aktualisieren"-Button — sofort einen Refresh an, auch innerhalb des
`MIN_REFRESH_INTERVAL_MINUTES`-Debounce-Fensters, damit man eine Korrektur
gleich gegenprüfen kann.

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
- **Alembic-Migrationen**, sobald das Schema wächst
- **Auth** fürs Adminer/`POST /refresh`-Endpoint, falls die Seite mal
  öffentlich erreichbar wird (`/admin` hat seit kurzem eigenes Basic-Auth,
  Adminer und der öffentliche Refresh-Button aber noch nicht)

## Tests

Parser-Tests laufen gegen eingefrorene, echte (aber gekürzte) HTML/JSON-
Antworten der Quellen unter `tests/fixtures/` — kein Netzwerkzugriff nötig,
kein laufender Stack nötig. Zweck: wenn Microsoft/dotnet mal die Seiten-/
Feed-Struktur ändert, merkt man das daran, dass der *echte* Fetcher im
laufenden Betrieb anfängt zu scheitern (Fehler landen im `FetchRun`),
während diese Tests weiter grün gegen die alte, eingefrorene Struktur
laufen — die Diskrepanz zeigt genau, was sich geändert hat. Siehe
`tests/fixtures/README.md` für die Herkunft jedes Fixtures.

```bash
pip install -r requirements-dev.txt
pytest
```

(Die Test-Dependencies sind bewusst nicht im Docker-Image — `requirements.txt`
bleibt schlank für den Produktivbetrieb.)

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
tests/
  fixtures/                    # eingefrorenes, echtes HTML/JSON je Quelle
  test_*.py                     # ein Testmodul je Fetcher
```
