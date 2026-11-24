# CattleCare — Herd Management System & Health Passport Portal

A full-stack livestock records app with two sides: a **Farmer Workspace** for registering cattle, recording vaccinations and clinical notes, generating QR tags and downloading backups; and a **Public/Vet Portal** that renders those records as a printable health passport with live vaccination countdowns. The backend is a multithreaded Python standard-library HTTP server backed by SQLite (WAL mode). There are no third-party Python packages.

---

## Project structure

```
catil helth/
├── index.html        Farmer workspace (SPA shell)
├── style.css         Farmer workspace styles
├── script.js         Farmer workspace client logic
├── portal.html       Public/vet verification portal
├── portal.css        Portal styles and print stylesheet
├── portal.js         Portal search, filters and passport renderer
├── server.py         REST API + static file server
├── test_server.py    20 automated tests (unittest, no dependencies)
├── cattlecare.db     SQLite data (git-ignored)
├── uploads/          Animal photos saved as files (git-ignored)
├── .env.example      Environment variable reference
├── run_server.bat    Windows launcher
├── LICENSE           MIT
└── README.md         This file
```

---

## Quick start (under 5 minutes)

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
- Generate and download a QR code per animal; export a JSON backup.

### Public/Vet Portal (`portal.html`)
- Search by tag ID, name, breed or notes; filter by health or vaccine-due status.
- Printable animal passport with age, vaccination countdown and clinical notes.
- **Verification is explicit:** the certificate says "Vet Verified Record" only when a vet-token holder has called the verify endpoint. Otherwise it clearly says "Farmer entry — not vet-verified". Every verification writes an audit row.

When you save, delete or back up in the Farmer Workspace, the app uses the admin passcode from the top-bar field and caches it in `sessionStorage` for that browser session. A wrong passcode clears the cache and asks again.

---

## REST API

All responses are JSON. Writes and exports require the `X-Admin-Token` header (or `Authorization: Bearer <token>`).

| Method | Endpoint | Auth | Purpose |
|--------|----------|------|---------|
| GET | `/api/health` | No | Liveness check |
| GET | `/api/animals` | No | List; `?search=`, `?tag=`, `?health=`, `?summary=true`, `?limit=` (1–200), `?offset=` |
| GET | `/api/animals/<id-or-tag>` | No | Single record, including photo path |
| POST | `/api/animals` | Admin | Create; validates fields, generates id/timestamp server-side |
| PUT | `/api/animals/<id>` | Admin | Update; preserves photo and verification when omitted |
| DELETE | `/api/animals/<id>` | Admin | Delete |
| POST | `/api/animals/<id>/verify` | **Vet** | Set `verified`, `verifiedBy`, `verifiedAt` and write audit log |
| GET | `/api/stats` | No | Dashboard aggregates (no photo/notes columns loaded) |
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
- Offline editing is disabled on purpose: local-only edits would be silently lost when the server reconnects. The app still shows records from a local cache when offline, read-only.
- Requests are capped at 32 concurrent handlers with a 10-second socket timeout. This is basic protection, not a substitute for a reverse proxy.
- The server binds to loopback. For public access, put it behind a TLS reverse proxy and restrict who can reach admin/vet routes.

---

## Testing

```bat
python -m unittest test_server.py -v
```
20 tests cover date logic, schema/indexes, list/search/filter endpoints, pagination, full CRUD with file-backed photos, backup auth, payload limits, blocked files, invalid auth (including the old hardcoded token), unsafe photo rejection, input validation, vet-only verification with audit logging, admin-token rejection on `/verify`, absence of wildcard CORS, the client backup-token contract, real socket behaviour and demo-data opt-in.

---

## Deployment

Local use is the `.bat` launcher. A `Dockerfile` and `.github/workflows/tests.yml` are included: CI runs the 20 tests on every push/PR, and Docker builds a minimal image. Example:
```bat
docker build -t cattlecare .
docker run -e FARM_ADMIN_TOKEN=... -e FARM_VET_TOKEN=... -p 8000:8000 cattlecare
```
For real deployment, still add a TLS reverse proxy, restrict admin/vet routes, mount persistent volumes for `cattlecare.db` and `uploads/`, and schedule regular backups via `/api/export`. `ensure_columns()` migrates older databases automatically.

## License

MIT — see `LICENSE`.
