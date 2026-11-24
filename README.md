# CattleCare — Sustainable Herd Management System & Public Verification Portal

A complete, production-hardened full-stack livestock management ecosystem featuring an administrative workspace for farmers and a public/veterinarian verification portal for inspecting cattle health passports.

---

## 📁 Project Structure

```
catil helth/
│
├── index.html        # Farmer Workspace (Single-page app for herd management & registration)
├── style.css         # Solarpunk design system & responsive styles for the farmer workspace
├── script.js         # Frontend client logic & REST API integration for farmer workspace
│
├── portal.html       # Public & Vet Verification Portal (read/inspect animal passports)
├── portal.css        # Clean styles, cards, and print stylesheet for the portal
├── portal.js         # Search, filter pills, URL routing & passport renderer
│
├── server.py         # Multi-threaded Python REST API + SQLite WAL database + static files
├── test_server.py    # Automated test suite (8 integration & unit tests, 100% pass)
├── cattlecare.db     # SQLite database storing herd records permanently (git-ignored)
├── .gitignore        # Ignores runtime databases, bytecode, and IDE configurations
├── run_server.bat    # One-click Windows launcher to start the backend server
└── README.md         # Documentation & user manual
```

---

## 🏛 Architecture Diagram

```
 ┌──────────────────────────────────────┐        ┌──────────────────────────────────────┐
 │     1. Farmer Workspace              │        │  2. Public & Vet Inspection Portal   │
 │        (index.html)                  │        │     (portal.html / ?tag=COW-101)     │
 │  • Register new animals              │        │  • Search herd by ID, Name, Breed    │
 │  • Record vaccines & dates           │        │  • Inspect verified health passports │
 │  • Write clinical doctor notes       │        │  • Check immunization countdowns     │
 │  • Generate printable QR tags        │        │  • Print official health certificate │
 └──────────────────┬───────────────────┘        └──────────────────┬───────────────────┘
                    │                                               │
                    │         REST API (GET / POST / PUT)           │
                    └───────────────────┬───────────────────────────┘
                                        ▼
                        ┌───────────────────────────────┐
                        │   Multi-Threaded Server       │
                        │    (ThreadingHTTPServer)      │
                        │    • Anti-DoS 5MB Guard       │
                        │    • Token-Protected Writes   │
                        │               ▲               │
                        │               ▼               │
                        │   Persistent SQLite Database  │
                        │    • WAL Mode Concurrency     │
                        │    • Indexed createdAt/health │
                        └───────────────────────────────┘
```

---

## 🌐 The Two Web Portals

### 1. Farmer Management Workspace (`index.html`)
👉 **URL:** `http://localhost:8000` or `http://localhost:8000/index.html`
* **Purpose:** For the farm manager or cattle owner.
* **Capabilities:**
  * Register new cattle with photos and medical history.
  * Update health status (`Healthy` vs `Needs attention`).
  * Record vaccines given and schedule next vaccination dates.
  * Write and maintain clinical doctor observations and feeding notes.
  * Generate and download scannable QR codes for ear tags and barn clipboards.
  * Manage herd statistics and overdue vaccination reminders.

### 2. Public & Veterinarian Health Portal (`portal.html`)
👉 **URL:** `http://localhost:8000/portal.html`
* **Purpose:** For veterinarians, cattle inspectors, buyers, and dairy cooperatives to access and verify records entered by the farmer.
* **Capabilities:**
  * **Tag ID & Name Search:** Quickly lookup any cow by Tag ID (e.g. `COW-101`), Name, or Breed.
  * **Filter by Condition:** Filter herd by Healthy, Needs Attention / Quarantine, or Vaccinations Due.
  * **Digital Cattle Passport / Certificate (`portal.html?tag=COW-101`):**
    * Displays official verified pedigree, age, photo, and registration date.
    * Real-time vaccination countdown (e.g. "Due in 14 days", "Overdue by 4 days").
    * Complete veterinary clinical notes and treatment history entered by the farmer.
    * **Print / Export PDF:** Single-click printable medical certificate with clean print styling.
    * **Share Link:** One-click copy of the animal's permanent passport link.
  * **Direct QR Code Access:** Scanning any animal's QR code automatically opens its passport in this portal!

---

## 🧪 Automated Testing

Run the included automated integration and unit test suite:
```bash
python -m unittest test_server.py -v
```
All 8 test suites verify:
1. Vaccine status and date difference calculations.
2. SQLite schema creation, WAL journal mode, and performance indexes.
3. `/api/health` system availability.
4. `/api/stats` aggregation metrics.
5. `/api/animals` list, search queries, and health filtering.
6. Full CRUD lifecycle: Create, Duplicate Tag rejection, Read by ID/Tag, Update with photo preservation, Delete, and 404 confirmation.
7. `/api/export` JSON database backup generation.
8. Anti-DoS payload size enforcement (HTTP 413 for payloads > 5MB).

---

## 🚀 How to Run

### 1. Start the Server
Double-click **`run_server.bat`** in the project folder.

### 2. Open in Browser
* Open **`http://localhost:8000`** for the **Farmer Workspace**.
* Open **`http://localhost:8000/portal.html`** for the **Public/Vet Inspection Portal**.
* Or open a specific animal directly:
  👉 **`http://localhost:8000/portal.html?tag=COW-101`**
