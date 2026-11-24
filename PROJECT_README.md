# CattleCare — Herd Management System & Health Passport Portal

A full-stack livestock records app with two sides: a **Farmer Workspace** for registering cattle, recording vaccinations and clinical notes, generating QR tags and downloading backups; and a **Public/Vet Portal** that renders those records as a printable health passport with live vaccination countdowns. The backend is a multithreaded Python standard-library HTTP server backed by SQLite (WAL mode). There are no third-party Python packages.

> **Deployment status:** the app is ready for a small, private deployment behind a TLS reverse proxy. It is not a substitute for a regulated veterinary records system. If you enter real personally identifiable medical data, you own the legal, contractual and operational requirements (consent, retention, encryption at rest, incident response).

---

## Project structure

```
catil helth/
├── index.html          Farmer workspace (SPA shell)
├── style.css           Farmer workspace styles
├── script.js           Farmer workspace client logic (paged API reads)
├── portal.html         Public/vet verification portal
├── portal.css          Portal styles and print stylesheet
├── portal.js           Portal search, filters, passport renderer (paged API reads)
├── server.py           REST API + static file server
├── test_server.py      35 automated tests (unittest, no dependencies)
├── test_frontend.html  Browser DOM test for the backup button
├── backup.py           Scheduled backup client using /api/export
├── Caddyfile           TLS reverse proxy configuration
├── docker-compose.yml  App + proxy with named persistent volumes
├── vendor/             Local QR library (no CDN dependency)
├── cattlecare.db       SQLite data (git-ignored)
├── uploads/            Animal photos saved as files (git-ignored)
├── .env.example        Environment variable reference
├── run_server.bat      Windows launcher
├── LICENSE             MIT
└── README.md           This file
```

---

## Quick start (local demo)

**1. Set two tokens.** The admin token protects writes/backups; a **different** vet token protects verification. There are no default credentials.

PowerShell:
```powershell
$env:FARM_ADMIN_TOKEN = "choose-a-long-random-string"
$env:FARM_VET_TOKEN   = "choose-a-different-long-random-string"
```

Command Prompt:
```bat
set FARM_ADMIN_TOKEN=choose-a-long-random-string
set FARM_VET_TOKEN=choose-a-different-long-random-string
```

`run_server.bat` prompts for both if you skip this step. Enter the **admin** token in the browser's “Admin passcode” field (top bar) when saving or backing up.

**2. Start the server.** Double-click `run_server.bat`, or run:
```bat
python server.py
```
The server binds to `127.0.0.1:8000` only. Set `PORT` to use a different port.

**3. Open the app.**
- Farmer Workspace: http://localhost:8000/
- Public/Vet Portal: http://localhost:8000/portal.html
- A specific passport: http://localhost:8000/portal.html?tag=COW-101

New databases start empty. If you want the three sample cows for a demo, set `SEED_DEMO_DATA=1` **before the first run**. Never use demo data for real records.

---

## The two portals

### Farmer Workspace (`index.html`)
- Register/edit cattle with a resized photo (max 850px, JPEG quality 0.76).
- Set health status, vaccine name and last/next vaccination dates.
- Write clinical notes and see overdue/upcoming care on the dashboard.
- Generate and download a QR code per animal (vendored `vendor/qrcode.min.js`, no CDN).
- Page through the herd 50 records at a time; search and health filters run server-side.
- **Care Log:** record vaccination, treatment, health check, observation and movement events against an animal instead of burying them in free text.
- **Audit trail:** create/update/delete/verify/event actions are appended to `audit_log` and shown in the Care Log page.
- **Insights:** SQL-backed health mix, vaccine status, event counts, verified count and top breeds.
- Export a JSON backup (admin token required).

### Public/Vet Portal (`portal.html`)
- Search by tag ID, name, breed or notes; filter by health or vaccine-due status, paged 50 at a time.
- Printable animal passport with age, vaccination countdown and clinical notes.
- **Public-safe view:** a checkbox switches list and passport requests to server-enforced safe endpoints (`/api/animals?public=true` and `/api/passport/<id>`) that omit notes/photos and exclude notes from search. Shared links carry `&public=1`.
- Direct passport links work for animals on any page.
- **Verification is explicit:** the certificate says "Vet Verified Record" only when a vet-token holder has called the verify endpoint. Otherwise it clearly says "Farmer entry — not vet-verified". Every verification writes an audit row.

When you save, delete or back up in the Farmer Workspace, the app uses the admin passcode from the top-bar field and caches it in `sessionStorage` for that browser session. A wrong passcode clears the cache and asks again.

---

## REST API

All responses are JSON. Writes and exports require the `X-Admin-Token` header (or `Authorization: Bearer <token>`).

| Method | Endpoint | Auth | Purpose |
|--------|----------|------|---------|
| GET | `/api/health` | No | Liveness check |
| GET | `/api/animals` | No | List; `?search=`, `?tag=`, `?health=`, `?summary=true`, `?public=true` (server strips notes/photos and excludes notes from search), `?limit=` (1–200), `?offset=`; total in `X-Total-Count` |
| GET | `/api/animals/<id-or-tag>` | No | Single record, including photo path |
| POST | `/api/animals` | Admin | Create; validates fields, generates id/timestamp server-side |
| PUT | `/api/animals/<id>` | Admin | Update; preserves photo and verification when omitted |
| DELETE | `/api/animals/<id>` | Admin | Delete record and its uploaded photo |
| POST | `/api/animals/<id>/verify` | **Vet** | Set `verified`, `verifiedBy`, `verifiedAt` and write audit log |
| GET | `/api/stats` | No | Dashboard aggregates from SQL, cached 2s and invalidated on writes |
| GET | `/api/analytics` | No | Safe aggregate metrics for Insights; no notes or full records |
| GET | `/api/audit` | Admin | Last 100 audited actions |
| GET | `/api/passport/<id-or-tag>` | No | Public-safe passport; excludes notes and photos |
| GET | `/api/animals/<id>/events` | Admin | Care timeline for an animal |
| POST | `/api/animals/<id>/events` | Admin | Add a validated care event |
| GET | `/api/export` | Admin | Full JSON backup |
| GET | `/uploads/<file>` | No | Uploaded photos |

Example verification call (uses the vet token, not the admin token):
```powershell
$body = @{ verifiedBy = "Dr Rao" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/animals/COW-101/verify" `
  -Headers @{ "X-Vet-Token" = $env:FARM_VET_TOKEN } -Body $body -ContentType "application/json"
```

To remove demo cows from the database:
```powershell
python -c "import sqlite3; c=sqlite3.connect('cattlecare.db'); c.execute(\"DELETE FROM animals WHERE id LIKE 'seed-%'\"); c.commit()"
```

---

## Security notes

- Both `FARM_ADMIN_TOKEN` and `FARM_VET_TOKEN` must be set at startup; the server exits if either is missing or if they are identical.
- The admin token cannot verify records. Verification needs the separate vet token and appends to `verification_log`.
- CORS is intentionally absent: the pages and API are same-origin. Do not add `Access-Control-Allow-Origin: *` to write endpoints.
- `/api/export` returns medical notes, so it requires the admin token; the client sends it and fails loudly on 401.
- Photos are decoded from base64, saved as files under `uploads/`, and referenced by path. List endpoints use `?summary=true` and load full photos only for visible cards/passports.
- Offline editing is disabled on purpose: local-only edits would be silently lost when the server reconnects. The app still shows the last viewed page from a local cache when offline, read-only. Offline browser backup is also disabled because that cache is only one page; use the server export/backup client for complete backups.
- Five wrong admin/vet tokens from one client IP in five minutes are throttled with HTTP 429; a correct token resets the counter. This is per-process, so multiple app replicas do not share counters.
- Requests are capped at 32 concurrent handlers with a 10-second socket timeout. Access and rejection logs use `logging`; set `CATTLECARE_LOG_DIR` for a rotating `server.log`.
- The server binds to loopback by default. `BIND_HOST=0.0.0.0` is intended for the private Docker network only. Never expose port 8000 directly to the internet; terminate TLS at Caddy and open only ports 80/443.
- Passwords/tokens are not encrypted at rest in `.env`. Restrict file permissions and use a secret manager if your host provides one.

---

## Environment reference

| Variable | Dev default | Deployment value | Purpose |
|----------|-------------|------------------|---------|
| `FARM_ADMIN_TOKEN` | required | strong random secret | Write/export auth |
| `FARM_VET_TOKEN` | required | different strong random secret | Verification auth |
| `PORT` | `8000` | `8000` (internal) | HTTP port |
| `BIND_HOST` | `127.0.0.1` | `0.0.0.0` in container | Bind address; only expose behind proxy |
| `CATTLECARE_DB` | `./cattlecare.db` | `/app/data/cattlecare.db` | SQLite path; parent dir auto-created |
| `CATTLECARE_UPLOADS` | `./uploads` | `/app/uploads` | Photo directory |
| `CATTLECARE_LOG_DIR` | unset | `/app/logs` | Enables rotating file log |
| `CADDY_DOMAIN` | `localhost` | your DNS name | Automatic HTTPS certificate |
| `SEED_DEMO_DATA` | `0` | `0` | Opt-in demo rows on first run only |
| `APP_URL` | — | `https://your-domain` | Backup target URL |
| `BACKUP_DIR` | `./backups` | host backup mount | Backup output |
| `BACKUP_RETENTION_DAYS` | `30` | as required | Local backup cleanup |

---

## Production deployment (Docker Compose)

1. Copy `.env.example` to `.env` and replace **both** tokens and `CADDY_DOMAIN`. Point DNS at the host and allow ports 80/443.
2. Run:
   ```bat
   docker compose up -d --build
   ```
3. Open `https://<CADDY_DOMAIN>/` and `https://<CADDY_DOMAIN>/portal.html`.
4. Check logs with `docker compose logs -f app` and `docker compose logs -f proxy`.

Data lives in named volumes `cattlecare-data`, `cattlecare-uploads` and `cattlecare-logs`; Caddy certificates live in `caddy-data`/`caddy-config`. `docker compose down` does not delete them. Back them up with the procedure below. The app container is only reachable from the proxy on the compose network; Caddy adds HSTS and other security headers.

If you must run without Docker, set the two tokens plus `BIND_HOST=0.0.0.0`, terminate TLS in a proxy of your choice, create persistent directories, and set `CATTLECARE_DB`, `CATTLECARE_UPLOADS`, `CATTLECARE_LOG_DIR` to paths on persistent storage. Do not expose the Python port directly.

### Database migration

`ensure_columns()` adds `verified`, `verifiedBy` and `verifiedAt` to an older `animals` table. `init_db()` then uses `PRAGMA user_version`: version 2 creates `care_events`, `audit_log` and their indexes on first start. Take a file backup before upgrading, verify a test record and one care event after the first start. Rollback means restoring the previous database file; the app never downgrades `user_version`.

---

## Backups

`backup.py` calls `/api/export` with the admin token, writes a timestamped JSON file, and deletes files older than `BACKUP_RETENTION_DAYS`.

```bat
set FARM_ADMIN_TOKEN=<same-admin-token>
set APP_URL=https://your-domain
python backup.py
```

Schedule it with cron or a systemd timer, for example a daily 02:00 cron entry:
```cron
0 2 * * * cd /path/to/catil-helth && FARM_ADMIN_TOKEN=... APP_URL=https://your-domain /usr/bin/python3 backup.py >> backups/cron.log 2>&1
```

A local backup directory is **not** disaster recovery. Copy the JSON files (and the SQLite file / uploads if you use volumes) to encrypted off-host storage and test a restore. Exports contain medical notes: restrict access to the token and the backup location.

---

## Testing

```bat
python -m unittest test_server.py -v
```
35 tests cover date logic, schema/indexes/migration, list/search/filter endpoints (including notes), server-side paging totals, SQL-backed stats with cache invalidation, full CRUD with file-backed photos and cleanup, backup auth and client contract, payload limits, blocked files, invalid auth and per-IP throttling, unsafe photo rejection, input validation, vet-only verification with audit logging, admin-token rejection on `/verify`, absence of wildcard CORS, real socket behaviour, demo-data opt-in, env-configured paths, care-event validation and ordering, audit logging, public-safe list/passport field exclusion and note-search suppression, and aggregate-only analytics.

CI (`.github/workflows/tests.yml`) runs the tests, compiles the Python entrypoints and validates `docker compose config` on every push/PR.

For the browser-only DOM test of the real backup button handler (mock fetch, no data written):
```bat
python -m http.server 8765
```
Open http://localhost:8765/test_frontend.html and confirm the page says **PASS**. It checks that clicking Export sends `X-Admin-Token`, downloads a correctly named JSON file, contains the export payload, and refuses to fake a backup while offline. Node/jsdom is not required.

Additional frontend smoke checks: open both pages at 1280px and 390px, create 51 records to confirm Previous/Next controls, verify a paged passport deep link, print one passport, add a care event and confirm it appears in the timeline and audit trail, open Insights and confirm the bars render, toggle Public-safe view and confirm notes disappear, and confirm QR codes render with the network offline after a first load.

Screenshot placeholders: add `docs/screenshots/dashboard.png`, `docs/screenshots/care-log.png` and `docs/screenshots/passport.png` when you have a representative deployment; they are intentionally not committed here.

---

## Contributing

Keep changes dependency-free on the backend, run the unittest suite, validate compose changes with `docker compose config`, and include a short note in the PR describing threat-model impact (auth, data exposure or persistence). Do not commit `.env`, tokens, real animal records, or generated backups.

---

## License

MIT — see `LICENSE`.
