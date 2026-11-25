# CattleCare — Herd Management System & Health Passport Portal

A full-stack livestock records app with two sides: a **Farmer Workspace** for registering cattle, recording care events and clinical notes, generating QR tags and downloading complete backups; and a **Public/Vet Portal** that renders only safe passport fields with live vaccination countdowns and explicit vet-verification status. The backend is a multithreaded Python standard-library HTTP server backed by SQLite (WAL mode). There are no third-party Python packages.

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
├── test_server.py      49 automated tests (unittest, no dependencies)
├── backup.py           Scheduled backup client using /api/export
├── restore.py          Rebuilds a database and photos from an export
├── Caddyfile           TLS reverse proxy configuration
├── docker-compose.yml  App + proxy with named persistent volumes
├── vendor/             Local QR library + licence (no CDN dependency)
├── cattlecare.db       SQLite data (git-ignored)
├── uploads/            Animal photos saved as files (git-ignored)
├── .env.example        Environment variable reference
├── run_server.bat      Windows launcher
├── LICENSE             MIT
├── PROJECT_README.md   Full technical guide (this file)
└── README.md           Competition submission summary
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

`run_server.bat` prompts for both if you skip this step. The farmer workspace exchanges the configured admin or vet passcode for a secure, CSRF-protected session before mutations.

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
- New cattle receive server-generated `COW-0001`, `COW-0002`, and subsequent permanent IDs. Existing custom tags are preserved, updates cannot change an ID, and deleting a record never reuses its ID.
- Cow cards, profiles, QR labels, vaccination records and care events display the same unique ID.
- Vaccination care events accept a vaccination name and synchronize `vaccineName`, `lastVaccineDate` and optional `nextVaccineDate` on the linked cow.
- Set health status, vaccine name and last/next vaccination dates.
- Write clinical notes and see overdue/upcoming care on the dashboard.
- Generate and download a QR code per animal (vendored `vendor/qrcode.min.js`, no CDN).
- Page through the herd 50 records at a time; search and health filters run server-side.
- **Insights:** SQL-backed health mix, vaccine status, event counts, verified count and top breeds.
- Export a complete JSON backup (admin session or token required).

### Public/Vet Portal (`portal.html`)
- Search by tag ID, name or breed; filter by health or vaccine-due status, paged 50 at a time. Notes are excluded from public search.
- Printable animal passport with age, vaccination countdown and recent public care highlights; clinical notes and photo paths are never sent to this page.
- **Public-safe is the only mode:** every list request uses `?public=true` and every passport uses `/api/passport/<id>`. The checkbox is checked and disabled so nobody can accidentally reveal notes. Shared links and QR tags carry `&public=1`.
- Direct passport links work for animals on any page.
- **Verification is explicit:** the certificate says "Vet Verified Record" only when a vet-token holder has called the verify endpoint. Otherwise it clearly says "Farmer entry — not vet-verified". Every verification writes an audit row.

When you sign in, the passcode is exchanged for a short-lived HttpOnly cookie (8 hours by default). Writes from the browser also send a CSRF token returned by `/api/session`; the passcode itself is never stored in JavaScript. A wrong passcode leaves you in read-only view. The optional operator field is recorded as the actor in the audit trail.

---

## REST API

All responses are JSON. Browsers sign in with `POST /api/session` and then use the HttpOnly cookie plus `X-CSRF-Token` on writes. CLI clients can still send `X-Admin-Token` / `X-Vet-Token` (or `Authorization: Bearer <token>`); header tokens do not require CSRF.

| Method | Endpoint | Auth | Purpose |
|--------|----------|------|---------|
| GET | `/api/health` | No | Liveness check |
| POST | `/api/session` | Token in body | Exchange admin/vet token for an HttpOnly cookie, actor and CSRF token |
| GET | `/api/session` | Session | Restore the current browser session (no token) |
| POST | `/api/logout` | Session | Clear cookie and server-side session |
| GET | `/api/animals` | No (safe by default) | List; `?search=`, `?tag=`, `?health=`, `?summary=true`, `?private=true` (401 unless admin), `?public=true`, `?limit=` (1–200), `?offset=`; total in `X-Total-Count`. Anonymous and `?public=true` responses strip notes/photos and exclude notes from search. |
| GET | `/api/animals/<id-or-tag>` | Admin for full record | Unauthenticated callers receive a safe passport-shaped payload (no notes/photo) |
| POST | `/api/animals` | Admin | Create; validates fields, generates id/timestamp server-side |
| PUT | `/api/animals/<id>` | Admin | Update; preserves photo and verification when omitted |
| DELETE | `/api/animals/<id>` | Admin | Delete record, its care events and verification rows; audit entry is kept |
| POST | `/api/animals/<id>/verify` | **Vet** | Set `verified`, `verifiedBy`, `verifiedAt` and append audit/verification rows |
| GET | `/api/stats` | No | Dashboard aggregates from SQL, cached 2s and invalidated on writes |
| GET | `/api/analytics` | No | Safe aggregate metrics for Insights; no notes or full records |
| GET | `/api/audit` | Admin | Append-only action log (last 100) |
| GET | `/api/passport/<id-or-tag>` | No | Public-safe passport; excludes notes and photos |
| GET | `/api/animals/<id>/events` | Admin | Care timeline for an animal |
| POST | `/api/animals/<id>/events` | Admin | Add a validated care event |
| GET | `/api/export` | Admin | Complete JSON backup: all tables plus base64 photo bytes |
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
- The admin token cannot verify records. Verification needs the separate vet token and appends to `verification_log`. Verification logs store the real animal ID, not the tag or ID string from the URL.
- Browsers receive an HttpOnly, `SameSite=Strict` session cookie (Secure behind HTTPS or when `COOKIE_SECURE=1`) and a CSRF token in the JSON body. Cookie-authenticated writes require `X-CSRF-Token`; header-token writes from CLI tools do not.
- Safe-by-default reads: unauthenticated `/api/animals`, `/api/animals/<id>` and the portal never return notes or photo paths, and anonymous search cannot match notes. Full records require an admin session/token (`?private=true` explicitly asks for them and returns 401 when anonymous).
- The public portal cannot be switched into a private view: the checkbox is checked and disabled, and `/api/passport/<id>` is the only source for passport fields. QR tags and Passport links always append `&public=1`.
- CORS is intentionally absent: the pages and API are same-origin. Do not add `Access-Control-Allow-Origin: *` to write endpoints. A same-origin CSP, `X-Frame-Options: DENY`, `nosniff` and `no-referrer` are sent on API and static responses.
- `/api/export` returns medical notes, so it requires admin auth and includes every table plus base64-encoded photo files. The browser refuses to fake an offline backup; use `backup.py` for scheduled complete exports.
- Photos are decoded from base64, saved as files under `uploads/`, and referenced by path. List endpoints use `?summary=true` and load full photos only for visible cards/passports.
- Offline editing is disabled on purpose: local-only edits would be silently lost when the server reconnects. The app still shows the last viewed page from a local cache when offline, read-only.
- Five wrong admin/vet tokens from one client IP in five minutes are throttled with HTTP 429; a correct token resets the counter. Expired IP and session records are pruned. These counters are per-process, so multiple app replicas do not share them.
- Requests are capped at 32 concurrent handlers with a 10-second socket timeout. Access and rejection logs use `logging`; set `CATTLECARE_LOG_DIR` for a rotating `server.log`.
- `audit_log` is append-only at the SQLite trigger level. Deleting an animal removes its care events and verification rows but keeps the audit entries.
- The server binds to loopback by default. `BIND_HOST=0.0.0.0` is intended for the private Docker network only. Never expose port 8000 directly to the internet; terminate TLS at Caddy and open only ports 80/443. HSTS is only sent for real hostnames, never for `localhost`.
- Docker containers run as an unprivileged user with `no-new-privileges`, dropped capabilities and memory/CPU limits.
- Passwords/tokens are not encrypted at rest in `.env`. Restrict file permissions and use a secret manager if your host provides one.

---

## Environment reference

| Variable | Dev default | Deployment value | Purpose |
|----------|-------------|------------------|---------|
| `FARM_ADMIN_TOKEN` | required | strong random secret | Write/export auth |
| `FARM_VET_TOKEN` | required | different strong random secret | Verification auth |
| `PORT` | `8000` | `8000` (internal) | HTTP port |
| `BIND_HOST` | `127.0.0.1` | `0.0.0.0` in container | Bind address; only expose behind proxy |
| `COOKIE_SECURE` | `0` | `1` | Add `Secure` to session cookies (also automatic behind HTTPS proxy) |
| `SESSION_TTL_SECONDS` | `28800` | `28800` or shorter | Browser session lifetime |
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

Data lives in named volumes `cattlecare-data`, `cattlecare-uploads` and `cattlecare-logs`; Caddy certificates live in `caddy-data`/`caddy-config`. `docker compose down` does not delete them. Back them up with the procedure below. The app container is only reachable from the proxy on the compose network; both containers run as unprivileged users with dropped capabilities and resource limits. Caddy adds a CSP and other security headers, and only sends HSTS when `CADDY_DOMAIN` is a real hostname. Set `COOKIE_SECURE=1` in production.

If you must run without Docker, set the two tokens plus `BIND_HOST=0.0.0.0`, terminate TLS in a proxy of your choice, create persistent directories, and set `CATTLECARE_DB`, `CATTLECARE_UPLOADS`, `CATTLECARE_LOG_DIR` to paths on persistent storage. Do not expose the Python port directly.

### Database migration

`ensure_columns()` adds `verified`, `verifiedBy` and `verifiedAt` to an older `animals` table. `init_db()` then uses `PRAGMA user_version`: version 2 creates `care_events`, `audit_log` and their indexes on first start. The append-only `audit_log` triggers are (re)created on every start, including for databases migrated by an earlier version. Take a file backup before upgrading, verify a test record and one care event after the first start. Rollback means restoring the previous database file; the app never downgrades `user_version`. For a full logical restore, use `restore.py` with an export produced by `backup.py`.

---

## Backups

`backup.py` calls `/api/export` with the admin token, writes a timestamped JSON file, and deletes files older than `BACKUP_RETENTION_DAYS`. Exports are schema version 3 and contain **all four tables** (`animals`, `care_events`, `verification_log`, `audit_log`) plus base64-encoded photo bytes, so a single file is a complete logical backup.

```bat
set FARM_ADMIN_TOKEN=<same-admin-token>
set APP_URL=http://127.0.0.1:8000
python backup.py
```

Schedule it with cron or a systemd timer, for example a daily 02:00 cron entry:
```cron
0 2 * * * cd /path/to/catil-helth && FARM_ADMIN_TOKEN=... APP_URL=https://your-domain /usr/bin/python3 backup.py >> backups/cron.log 2>&1
```

Restore into a fresh database (the target must be empty unless you deliberately pass `--force`, which first takes its own file backup):

```bat
python restore.py backups\cattlecare_backup_20261124_020000.json --db C:\path\restored.db --uploads C:\path\uploads
```

A local backup directory is **not** disaster recovery. Copy the JSON files to encrypted off-host storage and test a restore regularly. Exports contain medical notes and photo bytes: restrict access to the token and the backup location, and keep backup files on an encrypted volume (the client sets mode `0600` where the OS supports it).

---

## Testing

```bat
python -m unittest test_server.py -v
```
49 tests cover date logic, schema/indexes/migration, generated unique tag sequences, vaccination/cow synchronization, list/search/filter endpoints, server-side paging totals, SQL-backed stats, protected CRUD with file-backed photos, care events and audit logs, public-safe passports, backup auth, payload limits, invalid auth and per-IP throttling, unsafe photo rejection, vet-only verification, CSRF sessions, append-only audit triggers, demo-credential rejection, sensitive-route protection and deployment hardening.

CI (`.github/workflows/tests.yml`) runs the tests, compiles the Python entrypoints, parses both browser scripts with `node --check`, and validates `docker compose config` on every push/PR.

Additional frontend smoke checks: open both pages at 1280px and 390px, create 51 records to confirm Previous/Next controls, verify a paged passport deep link, print one passport, open Insights and confirm the bars render, confirm the portal always hides notes, and confirm QR codes render with the network offline after a first load.

Screenshot placeholders: add `docs/screenshots/dashboard.png`, `docs/screenshots/care-log.png` and `docs/screenshots/passport.png` when you have a representative deployment; they are intentionally not committed here.

---

## Contributing

Keep changes dependency-free on the backend, run the unittest suite, validate compose changes with `docker compose config`, and include a short note in the PR describing threat-model impact (auth, data exposure or persistence). Do not commit `.env`, tokens, real animal records, or generated backups.

---

## License

MIT — see `LICENSE`.
