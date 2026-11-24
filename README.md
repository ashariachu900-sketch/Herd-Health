# CattleCare — Asharis Team

**EMBEDTHON'26**
Embed Club, Department of CSE, P.A. College of Engineering, Mangaluru

---

## Team

| # | GitHub | Role |
|---|--------|------|
| 1 | [@ashariachu900-sketch](https://github.com/ashariachu900-sketch) | Team Lead |
| 2 | [@hayafiroz642-ux](https://github.com/hayafiroz642-ux) | Member |
| 3 | [@ghub18963-bot](https://github.com/ghub18963-bot) | Member |

## Problem Statement

Paper herd registers make vaccination dates easy to miss and make health claims impossible to verify at a glance. CattleCare gives farmers one offline-friendly workspace for records and care events, and gives buyers and veterinarians a QR-linked passport that shows only safe, explicitly verified data.

## Timeline

| | |
|---|---|
| Venue | Seminar Hall, P.A. College of Engineering, Mangaluru |
| Start | 3:00PM IST, 24th November 2026 |
| **Deadline** | **1:30PM IST, 25th November 2026** |

Push access to this repository is closed at the deadline. Anything not pushed by then will not be evaluated.

## Getting Started

```bash
git clone https://github.com/Embed-Club/Embers-16-AsharisTeam.git
cd Embers-16-AsharisTeam
```

Commit and push regularly. Your commit history is how progress is tracked.

## Rules

1. Work only inside this repository.
2. Do not force-push or rewrite history.
3. Commit small and often, with clear messages.
4. Every member should commit at least once.
5. Fill in the Project Details section below before the deadline.

---

## Project Details

CattleCare — Herd Management System & Health Passport Portal.

**One-line description:**
A dependency-free Python/SQLite web app for registering cattle, recording structured care events, printing health passports and verifying vet records via QR links.

**Tech stack:**
Python 3 standard library (`http.server`, `sqlite3`), vanilla HTML/CSS/JavaScript, SQLite WAL, Docker Compose + Caddy for deployment, unittest for backend tests.

**How to run:**

```bash
export FARM_ADMIN_TOKEN=choose-a-long-random-string
export FARM_VET_TOKEN=choose-a-different-long-random-string
python server.py
```

Open `http://localhost:8000/` for the Farmer Workspace and `http://localhost:8000/portal.html` for the Public/Vet Portal. For full deployment, env table, backups and API docs, see [`PROJECT_README.md`](PROJECT_README.md).

**Demo link / screenshots:**
Local demo at `http://localhost:8000/`. Screenshot placeholders: `docs/screenshots/dashboard.png`, `docs/screenshots/care-log.png`, `docs/screenshots/passport.png`.

## Submission Checklist

- [ ] Code pushed to `main`
- [x] Project Details above completed
- [ ] README runs from a fresh clone
- [ ] Demo ready

---

Need help? 1. Rafan Ahamad Sheik - +91 90361 92359, 2. Fizan Feroz - +91 94464 51783
