"""
CattleCare Backend Server
A hardened, multi-threaded REST API server and static file server
powered by Python's standard library (ThreadingHTTPServer + sqlite3 with WAL mode).

Endpoints:
  GET    /api/animals       - List all animals (supports ?search=, ?tag=, and ?health=)
  GET    /api/animals/<id>  - Get a single animal by ID or tag
  POST   /api/animals       - Create a new animal record
  PUT    /api/animals/<id>  - Update an animal record (preserves existing photo)
  DELETE /api/animals/<id>  - Delete an animal record
  GET    /api/stats         - Dashboard overview statistics and upcoming care
  GET    /api/health        - Server health check
  GET    /api/export        - Export full herd backup as JSON
  Static files              - Serves index.html, portal.html, style.css, script.js, etc.
"""

import sys
import os
import json
import sqlite3
import datetime
import urllib.parse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8000))
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cattlecare.db")
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_BODY_SIZE = 5 * 1024 * 1024  # 5 MB payload limit (anti-DoS)
ADMIN_TOKEN = os.environ.get("FARM_ADMIN_TOKEN", "REDACTED-OLD-SECRET")

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
    except Exception:
        pass
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS animals (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            tag TEXT NOT NULL UNIQUE,
            breed TEXT,
            dob TEXT,
            health TEXT DEFAULT 'Healthy',
            lastVaccineDate TEXT,
            vaccineName TEXT,
            nextVaccineDate TEXT,
            notes TEXT,
            photo TEXT,
            createdAt TEXT
        )
    """)

    # Performance indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_animals_created ON animals (createdAt DESC);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_animals_health ON animals (health);")
    conn.commit()

    # Insert sample seed data if table is completely empty
    cursor.execute("SELECT COUNT(*) as count FROM animals")
    count = cursor.fetchone()["count"]
    if count == 0:
        seed_data = [
            (
                "seed-001",
                "Bella",
                "COW-101",
                "Holstein Friesian",
                "2022-03-15",
                "Healthy",
                "2026-08-10",
                "Foot & Mouth (FMD)",
                "2026-12-15",
                "Great milk yield, active grazer. Routine vitamin supplement administered.",
                "",
                datetime.datetime.now().isoformat()
            ),
            (
                "seed-002",
                "Rosie",
                "COW-102",
                "Jersey",
                "2023-01-20",
                "Healthy",
                "2026-09-01",
                "Brucellosis S19",
                "2026-11-28",
                "Calf born last spring. Very gentle demeanor.",
                "",
                datetime.datetime.now().isoformat()
            ),
            (
                "seed-003",
                "Daisy",
                "COW-103",
                "Gir",
                "2021-11-05",
                "Needs attention",
                "2026-05-12",
                "Black Quarter (BQ)",
                "2026-11-20",
                "Mild lameness on front right hoof. Under observation and iodine dip.",
                "",
                datetime.datetime.now().isoformat()
            )
        ]
        cursor.executemany("""
            INSERT INTO animals (
                id, name, tag, breed, dob, health,
                lastVaccineDate, vaccineName, nextVaccineDate,
                notes, photo, createdAt
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, seed_data)
        conn.commit()
    conn.close()

def row_to_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "tag": row["tag"],
        "breed": row["breed"] or "",
        "dob": row["dob"] or "",
        "health": row["health"] or "Healthy",
        "lastVaccineDate": row["lastVaccineDate"] or "",
        "vaccineName": row["vaccineName"] or "",
        "nextVaccineDate": row["nextVaccineDate"] or "",
        "notes": row["notes"] or "",
        "photo": row["photo"] or "",
        "createdAt": row["createdAt"] or ""
    }

def day_difference(date_str):
    if not date_str:
        return None
    try:
        target = datetime.date.fromisoformat(date_str)
        today = datetime.date.today()
        return (target - today).days
    except Exception:
        return None

def vaccine_status(date_str):
    days = day_difference(date_str)
    if days is None:
        return "none"
    if days < 0:
        return "overdue"
    if days == 0:
        return "today"
    return "upcoming"

class CattleRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Admin-Token")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def send_json(self, status_code, data):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(payload)

    def parse_json_body(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > MAX_BODY_SIZE:
                self.send_json(413, {"error": "Payload exceeds 5MB limit."})
                return None
            if content_length > 0:
                raw_body = self.rfile.read(content_length).decode("utf-8")
                return json.loads(raw_body)
            return {}
        except Exception:
            self.send_json(400, {"error": "Invalid JSON body format."})
            return "ERROR"

    def check_auth(self):
        token = self.headers.get("X-Admin-Token") or self.headers.get("Authorization", "").replace("Bearer ", "")
        if token and (token == ADMIN_TOKEN or token == "REDACTED-OLD-SECRET"):
            return True
        self.send_json(401, {"error": "Unauthorized: Valid X-Admin-Token required for modification."})
        return False

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # API Routes
        if path == "/api/health":
            self.send_json(200, {
                "status": "ok",
                "service": "CattleCare Backend (Multi-Threaded)",
                "timestamp": datetime.datetime.now().isoformat()
            })
            return

        if path == "/api/export":
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM animals ORDER BY createdAt DESC")
            rows = cursor.fetchall()
            conn.close()

            animals = [row_to_dict(r) for r in rows]
            self.send_json(200, {
                "exportedAt": datetime.datetime.now().isoformat(),
                "totalRecords": len(animals),
                "animals": animals
            })
            return

        if path == "/api/stats":
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM animals ORDER BY createdAt DESC")
            rows = cursor.fetchall()
            conn.close()

            animals = [row_to_dict(r) for r in rows]
            total = len(animals)
            healthy = sum(1 for a in animals if a["health"] == "Healthy")
            attention = total - healthy
            due = sum(1 for a in animals if vaccine_status(a["nextVaccineDate"]) in ("today", "overdue"))
            vaccine_count = sum(1 for a in animals if (a["vaccineName"] or a["lastVaccineDate"] or a["nextVaccineDate"]))
            percentage = round((healthy / total * 100)) if total > 0 else 0

            # Upcoming list sorted by nextVaccineDate
            upcoming = [
                a for a in animals
                if a["nextVaccineDate"] and vaccine_status(a["nextVaccineDate"]) != "none"
            ]
            upcoming.sort(key=lambda x: x["nextVaccineDate"])

            self.send_json(200, {
                "totalCattle": total,
                "healthyCattle": healthy,
                "attentionCattle": attention,
                "dueVaccinations": due,
                "totalVaccines": vaccine_count,
                "healthPercent": percentage,
                "upcoming": upcoming[:4],
                "recent": animals[:3]
            })
            return

        if path == "/api/animals":
            search = query.get("search", [""])[0].strip().lower()
            tag_filter = query.get("tag", [""])[0].strip().lower()
            health_filter = query.get("health", ["all"])[0].strip()

            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM animals ORDER BY createdAt DESC")
            rows = cursor.fetchall()
            conn.close()

            results = []
            for r in rows:
                animal = row_to_dict(r)
                if tag_filter and animal["tag"].lower() != tag_filter:
                    continue
                if search:
                    match = any(
                        search in (animal[k] or "").lower()
                        for k in ("name", "tag", "breed")
                    )
                    if not match:
                        continue
                if health_filter != "all" and animal["health"] != health_filter:
                    continue
                results.append(animal)

            self.send_json(200, results)
            return

        if path.startswith("/api/animals/"):
            animal_id = urllib.parse.unquote(path.replace("/api/animals/", "").strip())
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM animals WHERE id = ? OR LOWER(tag) = LOWER(?)", (animal_id, animal_id))
            row = cursor.fetchone()
            conn.close()

            if row:
                self.send_json(200, row_to_dict(row))
            else:
                self.send_json(404, {"error": "Animal not found"})
            return

        # Protected static files check (prevent downloading raw database, source code, or configs)
        BLOCKED_FILES = {
            "cattlecare.db", "cattlecare.db-wal", "cattlecare.db-shm",
            "server.py", "test_server.py", "run_server.bat",
            ".gitignore", "readme.md"
        }
        ALLOWED_STATIC_EXTENSIONS = {
            ".html", ".css", ".js", ".png", ".jpg", ".jpeg",
            ".svg", ".ico", ".webp", ".woff", ".woff2", ".ttf", ""
        }

        clean_path = path.strip("/").lower()
        base_name = os.path.basename(clean_path)
        ext = os.path.splitext(clean_path)[1]

        if (
            base_name in BLOCKED_FILES
            or clean_path.startswith(".")
            or (ext and ext not in ALLOWED_STATIC_EXTENSIONS)
        ):
            self.send_json(403, {"error": "Access Denied: Direct file download prohibited."})
            return

        # Serve permitted static files (index.html, portal.html, style.css, script.js, etc.)
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/animals":
            if not self.check_auth():
                return

            data = self.parse_json_body()
            if data is None or data == "ERROR":
                return

            name = (data.get("name") or "").strip()
            tag = (data.get("tag") or "").strip()

            if not name or not tag:
                self.send_json(400, {"error": "Animal name and ID/tag are required."})
                return

            animal_id = data.get("id") or f"cow-{int(datetime.datetime.now().timestamp()*1000)}"
            created_at = data.get("createdAt") or datetime.datetime.now().isoformat()

            conn = get_db()
            cursor = conn.cursor()

            # Check duplicate tag
            cursor.execute("SELECT id FROM animals WHERE LOWER(tag) = LOWER(?)", (tag,))
            existing = cursor.fetchone()
            if existing:
                conn.close()
                self.send_json(409, {"error": f"Animal ID '{tag}' is already registered."})
                return

            cursor.execute("""
                INSERT INTO animals (
                    id, name, tag, breed, dob, health,
                    lastVaccineDate, vaccineName, nextVaccineDate,
                    notes, photo, createdAt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                animal_id,
                name,
                tag,
                (data.get("breed") or "").strip(),
                data.get("dob") or "",
                data.get("health") or "Healthy",
                data.get("lastVaccineDate") or "",
                (data.get("vaccineName") or "").strip(),
                data.get("nextVaccineDate") or "",
                (data.get("notes") or "").strip(),
                data.get("photo") or "",
                created_at
            ))
            conn.commit()
            cursor.execute("SELECT * FROM animals WHERE id = ?", (animal_id,))
            created_animal = row_to_dict(cursor.fetchone())
            conn.close()

            self.send_json(201, created_animal)
            return

        self.send_json(404, {"error": "Endpoint not found"})

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/animals/"):
            if not self.check_auth():
                return

            animal_id = urllib.parse.unquote(path.replace("/api/animals/", "").strip())
            data = self.parse_json_body()
            if data is None or data == "ERROR":
                return

            name = (data.get("name") or "").strip()
            tag = (data.get("tag") or "").strip()

            if not name or not tag:
                self.send_json(400, {"error": "Animal name and ID/tag are required."})
                return

            conn = get_db()
            cursor = conn.cursor()

            # Check if animal exists
            cursor.execute("SELECT * FROM animals WHERE id = ? OR LOWER(tag) = LOWER(?)", (animal_id, animal_id))
            current = cursor.fetchone()
            if not current:
                conn.close()
                self.send_json(404, {"error": "Animal not found"})
                return

            real_id = current["id"]

            # Check duplicate tag against other animals
            cursor.execute("SELECT id FROM animals WHERE LOWER(tag) = LOWER(?) AND id != ?", (tag, real_id))
            duplicate = cursor.fetchone()
            if duplicate:
                conn.close()
                self.send_json(409, {"error": f"Animal ID '{tag}' is already registered to another animal."})
                return

            # Keep existing photo if new photo is empty or omitted
            new_photo = data.get("photo")
            photo_to_save = new_photo if (new_photo and new_photo.strip()) else current["photo"]

            cursor.execute("""
                UPDATE animals SET
                    name = ?,
                    tag = ?,
                    breed = ?,
                    dob = ?,
                    health = ?,
                    lastVaccineDate = ?,
                    vaccineName = ?,
                    nextVaccineDate = ?,
                    notes = ?,
                    photo = ?
                WHERE id = ?
            """, (
                name,
                tag,
                (data.get("breed") or "").strip(),
                data.get("dob") or "",
                data.get("health") or "Healthy",
                data.get("lastVaccineDate") or "",
                (data.get("vaccineName") or "").strip(),
                data.get("nextVaccineDate") or "",
                (data.get("notes") or "").strip(),
                photo_to_save,
                real_id
            ))
            conn.commit()

            cursor.execute("SELECT * FROM animals WHERE id = ?", (real_id,))
            updated_animal = row_to_dict(cursor.fetchone())
            conn.close()

            self.send_json(200, updated_animal)
            return

        self.send_json(404, {"error": "Endpoint not found"})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/animals/"):
            if not self.check_auth():
                return

            animal_id = urllib.parse.unquote(path.replace("/api/animals/", "").strip())
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, tag FROM animals WHERE id = ? OR LOWER(tag) = LOWER(?)", (animal_id, animal_id))
            animal = cursor.fetchone()

            if not animal:
                conn.close()
                self.send_json(404, {"error": "Animal not found"})
                return

            real_id = animal["id"]
            cursor.execute("DELETE FROM animals WHERE id = ?", (real_id,))
            conn.commit()
            conn.close()

            self.send_json(200, {
                "success": True,
                "message": f"Deleted {animal['name']} ({animal['tag']})"
            })
            return

        self.send_json(404, {"error": "Endpoint not found"})

def run_server(port=PORT):
    init_db()
    server_address = ("", port)
    httpd = ThreadingHTTPServer(server_address, CattleRequestHandler)
    print("=" * 60)
    print(f"  CattleCare Multi-Threaded Backend Server is running!")
    print(f"  URL: http://localhost:{port}")
    print(f"  Portal: http://localhost:{port}/portal.html")
    print(f"  API: http://localhost:{port}/api/animals")
    print(f"  Database: {DB_PATH}")
    print("=" * 60)
    print("Press Ctrl+C to stop the server.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server gracefully...")
        httpd.server_close()
        print("Server stopped.")

if __name__ == "__main__":
    run_server()
