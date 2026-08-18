# WindowsPatchWatch

Website that shows the current patch status for **Windows 10/11 (incl.
LTSB/LTSC), Windows Server, SQL Server, .NET Framework, and .NET** —
including history, with automatic background refresh.

## How it works

- **On page load**: The page immediately shows the last known state from the
  database (no waiting on external sources). In the background, HTMX kicks
  off a refresh (debounced, see `MIN_REFRESH_INTERVAL_MINUTES`) — once new
  data is in, the table updates automatically without a reload.
- **On its own**: An APScheduler job checks all sources for new versions every
  `FETCH_INTERVAL_HOURS` hours (default: 6), independent of visitors.
- **History**: Every patch found is stored as its own row and never deleted —
  "History" per product is simply the list of every patch ever seen, sorted
  by date.

## Data sources

Microsoft's RSS feed for security updates has been shut down. Instead:

| Source | Provides | Method |
|---|---|---|
| [Windows Release Health](https://learn.microsoft.com/en-us/windows/release-health/) (Microsoft Learn) | Windows 10/11 (incl. LTSB/LTSC) & Windows Server: KB, build, date | HTML scraping of the "Update history" tables |
| [SQL Server build versions](https://learn.microsoft.com/en-us/troubleshoot/sql/releases/) (Microsoft Learn) | SQL Server 2016/2017/2019/2022/2025: CU/GDR build, KB, date | HTML scraping of the per-version "build versions" tables |
| [MSRC Security Update API](https://api.msrc.microsoft.com/cvrf/v2.0/updates) | .NET Framework security updates (KB per version) | Official JSON API (CVRF) |
| [dotnet/core releases-index.json](https://github.com/dotnet/core) | .NET (Core 5+) releases including history | Official JSON on GitHub |

See `app/fetchers/` — each source is its own fetcher with its own docstring
explaining that source's assumptions and limitations.

## Getting started

```bash
cp .env.example .env   # adjust the password
docker compose up -d --build
```

- App: http://localhost:8000
- Adminer (DB browser, optional): http://localhost:8081 — server `db`, login
  from `.env`

The first run starts automatically when the container starts
(`FETCH_ON_STARTUP=true`) and populates the database; depending on the
sources, that takes one to two minutes.

## Backups

The `db-backup` service (`prodrigestivill/postgres-backup-local`) runs
alongside automatically and drops gzip'd `pg_dump` files under `./backups/` —
default is a daily dump, with rotation (default: 30 days, 12 weeks, 12
months, configurable via `BACKUP_KEEP_*` / `BACKUP_SCHEDULE` in `.env`).
There are `daily/`, `weekly/`, `monthly/` subfolders as well as `last/` with
the most recent dump. The files are plain `.sql.gz`, so they're restorable
with any Postgres, independent of the backup image.

**Restore** (overwrites the running DB — stop the container first, or target
an empty DB):

```bash
gunzip -c backups/daily/patchwatch-<TIMESTAMP>.sql.gz | \
  docker compose exec -T db psql -U ${POSTGRES_USER:-patchwatch} -d ${POSTGRES_DB:-patchwatch}
```

`./backups/` only lives on this machine (not in Git, see `.gitignore`) — for
an offsite copy, sync the folder itself to another destination yourself,
e.g. via `rclone` or `restic`.

## JSON API

- `GET /api/products` — list of every detected product/version
- `GET /api/products/{key}/patches` — full history of one product
- `POST /refresh` — trigger a manual refresh (debounced)
- `GET /export/json?scope=all|month` — every patch (or just the current
  calendar month's) as JSON, grouped by product. Reachable from the
  dashboard via the "Export all" / "Export current month" buttons, which
  copy the result straight to the clipboard instead of downloading it.

## Admin area

`/admin` (linked in the footer) — for the cases where a scraper gets
something wrong: manually add, correct, or delete individual patch entries.
Protected by HTTP Basic Auth (username `admin`, password from
`ADMIN_PASSWORD` in `.env` — make sure to change it before any public
deploy, default is `change-me`).

An entry edited or created here gets `manually_edited = true` and is no
longer overwritten by future automatic refreshes (only `last_seen_at` keeps
updating) — otherwise the next scraper run would simply revert the
correction.

`POST /admin/refresh` — unlike the normal "Refresh now" button — triggers a
refresh immediately, even within the `MIN_REFRESH_INTERVAL_MINUTES` debounce
window, so a correction can be checked against fresh data right away.

## Known limitations (deliberate scope decisions for v1)

- **No Alembic**: The DB schema is created at startup via `create_all`. Fine
  for v1; once the schema needs to change on a running instance, switching to
  real migrations is worth it.
- **.NET Framework data is best-effort**: MSRC only covers *security*
  updates, not pure quality rollups. The version → KB mapping is based on
  text-parsing MSRC's product names.
- **Scraping is fragile**: If Microsoft changes the table structure of the
  Release Health or SQL Server build-versions pages, the fetcher finds
  nothing for the affected page (logged as an error in `FetchRun`, but
  doesn't break the whole refresh). A look at the logs / `/api/products`
  reveals this quickly.
- **SQL Server has no end-of-life data**: unlike the Windows pages, the build-
  versions pages carry no support-end-date column, so `support_end_date` is
  always empty for SQL Server products (same situation as .NET Framework).
- **Severity/CVE** isn't currently linked to Windows KB entries (only present
  indirectly for .NET Framework, via MSRC).

## Ideas for later

- **CVE/severity enrichment** for Windows entries, linked via MSRC
  (build/KB → CVE list, severity as an extra column/badge)
- **Notifications**: webhook / email / Discord / ntfy on new patches for
  subscribed products
- **Diff view**: "what changed since build X" between two points in time
- **Known-issues rollup** per version (from Microsoft Learn's "Known issues"
  pages)
- **Export**: a dedicated RSS/Atom feed or CSV export per product, as a
  replacement for the discontinued Microsoft feed
- **Comparison view** of multiple versions side by side (e.g. all Server
  versions compared)
- **Server-side search/filter** instead of client-side only, plus filtering
  by update type (Security/Preview/OOB)
- **Alembic migrations**, once the schema grows
- **Auth** for Adminer / the `POST /refresh` endpoint, in case the site ever
  becomes publicly reachable (`/admin` recently got its own Basic Auth,
  Adminer and the public refresh button haven't yet)

## Tests

Parser tests run against frozen, real (but trimmed) HTML/JSON responses from
the sources, under `tests/fixtures/` — no network access needed, no running
stack needed. Purpose: if Microsoft/dotnet ever changes a page's/feed's
structure, that shows up as the *real* fetcher starting to fail in
production (errors land in `FetchRun`), while these tests keep passing
against the old, frozen structure — the discrepancy shows exactly what
changed. See `tests/fixtures/README.md` for where each fixture came from.

The web-route tests (dashboard, `/api/*`, `/export/json`, `/admin` auth), on
the other hand, run against a real but empty, throwaway SQLite file instead
of fixtures — the routers read their DB session directly from
`app.database.async_session_factory`, so there's no clean seam for fake data
the way there is for the fetchers. `tests/conftest.py` points `DATABASE_URL`
at SQLite for this (has to happen before any `import app...`) and
creates/drops the schema fresh per test; the app's lifespan (scheduler, real
fetch on startup) never runs in the process.

```bash
pip install -r requirements-dev.txt
pytest
```

(Test dependencies are deliberately not in the Docker image —
`requirements.txt` stays lean for production.)

## Project structure

```
app/
  fetchers/            # one file per data source
  routers/             # web.py (HTML/HTMX), api.py (JSON)
  templates/            # Jinja2 + HTMX partials
  static/                # CSS/JS
  models.py               # SQLAlchemy: Product, Patch, FetchRun
  refresh_service.py       # orchestration + upsert logic
  scheduler.py               # APScheduler job
  main.py                     # FastAPI app + lifespan
tests/
  fixtures/                    # frozen, real HTML/JSON per source
  test_*.py                     # one test module per fetcher
```
