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
├── test_server.py    17 automated tests (unittest, no dependencies)
├── cattlecare.db     SQLite data (git-ignored)
├── .env.example      Environment variable reference
├── run_server.bat    Windows launcher
├── LICENSE           MIT
└── README.md         This file
```

---

## Quick start (under 5 minutes)

**1. Set the admin token.** It is required for all writes and backups; there is no default credential.

PowerShell:
```powershell
$env:FARM_ADMIN_TOKEN = "choose-a-long-random-string"
```

Command Prompt:
```bat
set FARM_ADMIN_TOKEN=choose-a-long-random-string
```

`run_server.bat` will prompt for one if you skip this step.

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
- **Verification is explicit:** the certificate says "Vet Verified Record" only when an admin has called the verify endpoint. Otherwise it clearly says "Farmer entry — not vet-verified".

When you save or delete in the Farmer Workspace, the browser asks for the admin passcode once per session (`sessionStorage`).

---

## REST API

All responses are JSON. Writes and exports require the `X-Admin-Token` header (or `Authorization: Bearer <token>`).

| Method | Endpoint | Auth | Purpose |
|--------|----------|------|---------|
| GET | `/api/health` | No | Liveness check |
| GET | `/api/animals` | No | List; supports `?search=`, `?tag=`, `?health=`, `?summary=true` (omits photo blobs) |
| GET | `/api/animals/<id-or-tag>` | No | Single record, including photo |
| POST | `/api/animals` | Yes | Create; validates fields, generates id/timestamp server-side |
| PUT | `/api/animals/<id>` | Yes | Update; preserves photo and verification when omitted |
| DELETE | `/api/animals/<id>` | Yes | Delete |
| POST | `/api/animals/<id>/verify` | Yes | Set `verified`, `verifiedBy`, `verifiedAt` |
| GET | `/api/stats` | No | Dashboard aggregates |
| GET | `/api/export` | Yes | Full JSON backup |

Example verification call:
```powershell
$body = @{ verifiedBy = "Dr Rao" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/animals/COW-101/verify" `
  -Headers @{ "X-Admin-Token" = $env:FARM_ADMIN_TOKEN } -Body $body -ContentType "application/json"
```

To remove demo cows from the database:
```powershell
python -c "import sqlite3; c=sqlite3.connect('cattlecare.db'); c.execute(\"DELETE FROM animals WHERE id LIKE 'seed-%'\"); c.commit()"
```

---

## Security notes

- `FARM_ADMIN_TOKEN` must be set at startup; the server exits without it. Use a long random value and change it if it was ever committed.
- CORS is intentionally absent: the pages and API are same-origin. Do not add `Access-Control-Allow-Origin: *` to write endpoints.
- `/api/export` returns medical notes and photos, so it requires the admin token.
- Photo input is restricted to small base64 image data URIs, and all user fields are HTML-escaped before rendering.
- Offline editing is disabled on purpose: local-only edits would be silently lost when the server reconnects. The app still shows records from a local cache when offline, read-only.
- The verification stamp reflects a stored flag, not merely the fact that a page was opened. For a real deployment you would also add an audit log and a signed/tamper-evident certificate.
- The server binds to loopback. For public access, put it behind a TLS reverse proxy and restrict who can reach admin routes.

---

## Testing

```bat
python -m unittest test_server.py -v
```
17 tests cover date logic, schema/indexes, list/search/filter endpoints, full CRUD, backup auth, payload limits, blocked files, invalid auth (including the old hardcoded token), unsafe photo rejection, input validation, explicit verification, absence of wildcard CORS, real socket behaviour and demo-data opt-in.

---

## Deployment

This is currently a local/demo setup: one `.bat` launcher, no Docker or CI. To deploy, add a service manager, a reverse proxy with HTTPS, a dedicated `FARM_ADMIN_TOKEN` in the host environment, regular backups via `/api/export`, and a migration step for `ensure_columns()` when upgrading older databases.

## License

MIT — see `LICENSE`.
