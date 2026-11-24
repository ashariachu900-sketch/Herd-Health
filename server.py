"""
CattleCare Backend Server
A hardened, multi-threaded REST API server and static file server
powered by Python's standard library (ThreadingHTTPServer + sqlite3 in WAL mode).

Endpoints:
  GET    /api/health              - Liveness check
  GET    /api/animals             - List; ?search=, ?tag=, ?health=, ?summary=true, ?limit=, ?offset=
  GET    /api/animals/<id>        - Get one record by ID or tag (includes photo path)
  POST   /api/animals             - Create (admin token; server-generated id/createdAt)
  PUT    /api/animals/<id>        - Update (admin token; photo/verification preserved when omitted)
  DELETE /api/animals/<id>        - Delete (admin token)
  POST   /api/animals/<id>/verify - Vet verification (vet token; writes audit log)
  GET    /api/stats               - Dashboard aggregates
  GET    /api/export              - Full JSON backup (admin token)
  GET    /uploads/<file>          - Uploaded animal photos
  Static files                   - index.html, portal.html, CSS, JS

FARM_ADMIN_TOKEN and FARM_VET_TOKEN are both required at startup. There are no
default credentials. Failed token guesses are throttled per client IP, requests
are bounded by a semaphore and a 10-second socket timeout, and JSON logs rotate
when CATTLECARE_LOG_DIR is set.

Environment overrides for deployment:
  BIND_HOST            - defaults to 127.0.0.1; set 0.0.0.0 only behind a proxy
  CATTLECARE_DB        - database path (mount a persistent volume)
  CATTLECARE_UPLOADS   - photo directory (mount a persistent volume)
  CATTLECARE_LOG_DIR   - enables a rotating server.log
Terminate TLS at a reverse proxy; never expose port 8000 directly.
"""

import os
import re
import hmac
import json
import time
import uuid
import base64
import logging
import logging.handlers
import threading
import sqlite3
import datetime
import urllib.parse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8000))
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("CATTLECARE_DB", os.path.join(BASE_DIR, "cattlecare.db"))
STATIC_DIR = BASE_DIR
UPLOAD_DIR = os.environ.get("CATTLECARE_UPLOADS", os.path.join(BASE_DIR, "uploads"))
MAX_BODY_SIZE = 5 * 1024 * 1024  # 5 MB payload limit (anti-DoS)

# No hardcoded fallback: tokens must come from the environment (see .env.example).
ADMIN_TOKEN = os.environ.get("FARM_ADMIN_TOKEN")
VET_TOKEN = os.environ.get("FARM_VET_TOKEN")
PHOTO_DATA_RE = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/=]+)$")
PHOTO_PATH_RE = re.compile(r"^/uploads/[0-9a-f-]+\.(png|jpg|jpeg|gif|webp)$")
HEALTH_VALUES = ("Healthy", "Needs attention")
FIELD_LIMITS = {"name": 50, "tag": 30, "breed": 50, "vaccineName": 70, "notes": 1000}
MAX_CONCURRENT_REQUESTS = 32
REQUEST_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)

# Failed-attempt throttling: five bad tokens from one IP in five minutes is enough.
MAX_FAILED_ATTEMPTS = 5
ATTEMPT_WINDOW_SECONDS = 300
FAILED_ATTEMPTS = {}
FAILED_ATTEMPTS_LOCK = threading.Lock()

# Short-lived dashboard cache, invalidated by every write.
STATS_CACHE = {}
STATS_CACHE_TTL = 2

LOGGER = logging.getLogger("cattlecare")

def configure_logging():
    """Log to stdout and, when configured, to a rotating file for deployment."""
    handlers = [logging.StreamHandler()]
    log_dir = os.environ.get("CATTLECARE_LOG_DIR")
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "server.log"), maxBytes=5 * 1024 * 1024, backupCount=3
        ))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )

def client_ip(handler):
    """In-process tests have no socket; use a stable label for them."""
    return handler.client_address[0] if getattr(handler, "client_address", None) else "in-process"

def is_rate_limited(handler):
    ip = client_ip(handler)
    now = time.time()
    with FAILED_ATTEMPTS_LOCK:
        count, last = FAILED_ATTEMPTS.get(ip, (0, 0))
    if count >= MAX_FAILED_ATTEMPTS and now - last <= ATTEMPT_WINDOW_SECONDS:
        LOGGER.warning("Rate-limited auth attempts from %s", ip)
        handler.send_json(429, {"error": "Too many failed attempts. Try again in 5 minutes."})
        return True
    return False

def record_auth_failure(handler):
    ip = client_ip(handler)
    now = time.time()
    with FAILED_ATTEMPTS_LOCK:
        count, last = FAILED_ATTEMPTS.get(ip, (0, 0))
        if now - last > ATTEMPT_WINDOW_SECONDS:
            count = 0
        FAILED_ATTEMPTS[ip] = (count + 1, now)

def clear_auth_failures(handler):
    with FAILED_ATTEMPTS_LOCK:
        FAILED_ATTEMPTS.pop(client_ip(handler), None)

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_columns(conn):
    """Add verification columns to databases created by older versions."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(animals)")}
    additions = {
        "verified": "INTEGER NOT NULL DEFAULT 0",
        "verifiedBy": "TEXT",
        "verifiedAt": "TEXT",
    }
    for name, definition in additions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE animals ADD COLUMN {name} {definition}")

def init_db():
    # Support deployment paths such as /app/data/cattlecare.db on a fresh volume.
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = get_db()
    cursor = conn.cursor()
    try:
        conn.execute("PRAGMA journal_mode = WAL;")  # persistent setting: do this once, not per request
    except sqlite3.Error:
        pass
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
            createdAt TEXT,
            verified INTEGER NOT NULL DEFAULT 0,
            verifiedBy TEXT,
            verifiedAt TEXT
        )
    """)
    ensure_columns(conn)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS verification_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            animalId TEXT NOT NULL,
            verifiedBy TEXT NOT NULL,
            verifiedAt TEXT NOT NULL,
            actor TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_verification_animal ON verification_log (animalId);")

    # Performance indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_animals_created ON animals (createdAt DESC);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_animals_health ON animals (health);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_animals_tag ON animals (tag);")
    conn.commit()

    # Demo data is opt-in only. Real databases must start empty.
    cursor.execute("SELECT COUNT(*) as count FROM animals")
    count = cursor.fetchone()["count"]
    if count == 0 and os.environ.get("SEED_DEMO_DATA") == "1":
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
        "createdAt": row["createdAt"] or "",
        "verified": bool(row["verified"]) if "verified" in row.keys() else False,
        "verifiedBy": (row["verifiedBy"] or "") if "verifiedBy" in row.keys() else "",
        "verifiedAt": (row["verifiedAt"] or "") if "verifiedAt" in row.keys() else ""
    }

def valid_iso_date(value):
    if not value:
        return True
    try:
        datetime.date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False

def clean_animal_fields(data):
    """Validate and normalize client input. Returns (fields, error_message)."""
    fields = {}
    for key, limit in FIELD_LIMITS.items():
        value = data.get(key) or ""
        value = str(value).strip()
        if len(value) > limit:
            return None, f"{key} must be {limit} characters or fewer."
        fields[key] = value

    if not fields["name"] or not fields["tag"]:
        return None, "Animal name and ID/tag are required."

    fields["health"] = str(data.get("health") or "Healthy")
    if fields["health"] not in HEALTH_VALUES:
        return None, "Health must be 'Healthy' or 'Needs attention'."

    for key in ("dob", "lastVaccineDate", "nextVaccineDate"):
        value = str(data.get(key) or "")
        if not valid_iso_date(value):
            return None, f"{key} must be a valid YYYY-MM-DD date or empty."
        fields[key] = value

    fields["photo"] = str(data.get("photo") or "")
    if fields["photo"] and not (
        PHOTO_DATA_RE.fullmatch(fields["photo"]) or PHOTO_PATH_RE.fullmatch(fields["photo"])
    ):
        return None, "Photo must be a small base64 image or an existing /uploads/ path."
    return fields, None

def persist_photo(photo):
    """Write a base64 data URI to disk and return its public path. Existing paths pass through."""
    if not photo:
        return ""
    if PHOTO_PATH_RE.fullmatch(photo):
        return photo
    match = PHOTO_DATA_RE.fullmatch(photo)
    if not match:
        raise ValueError("Invalid photo format")
    mime_ext = {"png": "png", "jpeg": "jpg", "gif": "gif", "webp": "webp"}
    extension = mime_ext[match.group(1)]
    file_name = f"{uuid.uuid4()}.{extension}"
    encoded = match.group(2)
    encoded += "=" * (-len(encoded) % 4)  # tolerate data URIs without padding
    with open(os.path.join(UPLOAD_DIR, file_name), "wb") as handle:
        handle.write(base64.b64decode(encoded, validate=True))
    return f"/uploads/{file_name}"

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

    def setup(self):
        super().setup()
        self.connection.settimeout(10)  # slow clients cannot hold a thread forever

    def handle_one_request(self):
        # Cap concurrent requests; ThreadingHTTPServer otherwise spawns unbounded threads.
        with REQUEST_SEMAPHORE:
            return super().handle_one_request()

    # This app is served from the same origin as its API, so no CORS headers are
    # needed. Wildcard CORS on write endpoints would let other sites call this API.

    def log_message(self, fmt, *args):
        """Route access logs through the rotating logger instead of raw stderr."""
        LOGGER.info("%s %s", client_ip(self), fmt % args)

    def send_json(self, status_code, data, extra_headers=None):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def parse_json_body(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > MAX_BODY_SIZE:
                self.send_json(413, {"error": "Payload exceeds 5MB limit."})
                return None
            if content_length < 0:
                raise ValueError("Negative Content-Length")
            if content_length > 0:
                raw_body = self.rfile.read(content_length).decode("utf-8")
                parsed = json.loads(raw_body)
                if not isinstance(parsed, dict):
                    raise ValueError("JSON body must be an object")
                return parsed
            return {}
        except Exception:
            self.send_json(400, {"error": "Invalid JSON body format."})
            return "ERROR"

    def check_auth(self):
        token = self.headers.get("X-Admin-Token")
        if not token:
            authorization = self.headers.get("Authorization", "")
            token = authorization.removeprefix("Bearer ").strip()
        # Constant-time compare avoids leaking the token through timing differences.
        # A correct token always works (and resets the counter); only guesses are throttled.
        if ADMIN_TOKEN and token and hmac.compare_digest(token, ADMIN_TOKEN):
            clear_auth_failures(self)
            return True
        if not is_rate_limited(self):
            record_auth_failure(self)
            LOGGER.warning("Rejected admin token from %s", client_ip(self))
            self.send_json(401, {"error": "Unauthorized: Valid X-Admin-Token required."})
        return False

    def check_vet_auth(self):
        token = self.headers.get("X-Vet-Token")
        if not token:
            authorization = self.headers.get("Authorization", "")
            token = authorization.removeprefix("Bearer ").strip()
        if VET_TOKEN and token and hmac.compare_digest(token, VET_TOKEN):
            clear_auth_failures(self)
            return True
        if not is_rate_limited(self):
            record_auth_failure(self)
            LOGGER.warning("Rejected vet token from %s", client_ip(self))
            self.send_json(401, {"error": "Unauthorized: Valid X-Vet-Token required."})
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
            if not self.check_auth():
                return
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
            cached = STATS_CACHE.get("data")
            if cached and time.time() - STATS_CACHE.get("at", 0) < STATS_CACHE_TTL:
                self.send_json(200, cached)
                return

            conn = get_db()
            cursor = conn.cursor()
            # Aggregates run in SQLite: no photo/notes blobs and no Python row scan.
            cursor.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN health = 'Healthy' THEN 1 ELSE 0 END) AS healthy,
                       SUM(CASE WHEN date(nextVaccineDate) IS NOT NULL
                                 AND date(nextVaccineDate) <= date('now') THEN 1 ELSE 0 END) AS due,
                       SUM(CASE WHEN vaccineName IS NOT NULL OR lastVaccineDate IS NOT NULL
                                 OR nextVaccineDate IS NOT NULL THEN 1 ELSE 0 END) AS vaccines
                FROM animals
            """)
            agg = cursor.fetchone()
            total = agg["total"] or 0
            healthy = agg["healthy"] or 0

            columns = "id, name, tag, health, vaccineName, lastVaccineDate, nextVaccineDate, createdAt"
            cursor.execute(f"""
                SELECT {columns} FROM animals
                WHERE nextVaccineDate IS NOT NULL AND nextVaccineDate != ''
                  AND date(nextVaccineDate) IS NOT NULL
                ORDER BY date(nextVaccineDate) ASC LIMIT 4
            """)
            upcoming = [dict(r) for r in cursor.fetchall()]
            cursor.execute(f"SELECT {columns} FROM animals ORDER BY createdAt DESC LIMIT 3")
            recent = [dict(r) for r in cursor.fetchall()]
            conn.close()

            payload = {
                "totalCattle": total,
                "healthyCattle": healthy,
                "attentionCattle": total - healthy,
                "dueVaccinations": agg["due"] or 0,
                "totalVaccines": agg["vaccines"] or 0,
                "healthPercent": round(healthy / total * 100) if total else 0,
                "upcoming": upcoming,
                "recent": recent
            }
            STATS_CACHE["at"] = time.time()
            STATS_CACHE["data"] = payload
            self.send_json(200, payload)
            return

        if path == "/api/animals":
            search = query.get("search", [""])[0].strip().lower()
            tag_filter = query.get("tag", [""])[0].strip().lower()
            health_filter = query.get("health", ["all"])[0].strip().lower()
            # ?summary=true — omit large base64 photo blobs from list responses.
            # Callers that need the full photo should fetch /api/animals/<id>.
            summary_mode = query.get("summary", ["false"])[0].strip().lower() == "true"
            limit_raw = query.get("limit", [""])[0]
            offset_raw = query.get("offset", [""])[0]
            limit = min(max(int(limit_raw), 1), 200) if limit_raw.isdigit() else None
            offset = max(int(offset_raw), 0) if offset_raw.isdigit() else 0

            # Filter in SQL so indexes are used and rows aren't all loaded into memory.
            clauses, params = [], []
            if tag_filter:
                clauses.append("LOWER(tag) = LOWER(?)")
                params.append(tag_filter)
            if search:
                escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                like = f"%{escaped}%"
                clauses.append("(LOWER(name) LIKE ? ESCAPE '\\' OR LOWER(tag) LIKE ? ESCAPE '\\' OR LOWER(breed) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(notes, '')) LIKE ? ESCAPE '\\')")
                params.extend([like, like, like, like])
            if health_filter == "due":
                clauses.append("date(nextVaccineDate) IS NOT NULL AND date(nextVaccineDate) <= date('now')")
            elif health_filter != "all":
                clauses.append("health = ?")
                params.append(health_filter)

            where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
            filter_params = list(params)  # count query must not receive LIMIT/OFFSET values
            sql = "SELECT * FROM animals" + where_sql + " ORDER BY createdAt DESC"
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                params.extend([limit, offset])

            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM animals" + where_sql, filter_params)
            total_count = cursor.fetchone()["n"]
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            conn.close()

            results = []
            for r in rows:
                animal = row_to_dict(r)
                if summary_mode:
                    animal["hasPhoto"] = bool(animal["photo"])
                    animal.pop("photo", None)  # strip photo to cut payload size
                results.append(animal)

            self.send_json(200, results, {"X-Total-Count": str(total_count)})
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
            or "__pycache__" in clean_path
            or (path.endswith("/") and path != "/")  # no directory listings
            or (ext and ext not in ALLOWED_STATIC_EXTENSIONS)
        ):
            self.send_json(403, {"error": "Access Denied: Direct file download prohibited."})
            return

        # Serve permitted static files (index.html, portal.html, style.css, script.js, etc.)
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Vet verification requires a separate vet token and writes an audit row.
        if path.startswith("/api/animals/") and path.endswith("/verify"):
            if not self.check_vet_auth():
                return
            animal_id = urllib.parse.unquote(path[len("/api/animals/"):-len("/verify")].strip())
            data = self.parse_json_body()
            if data is None or data == "ERROR":
                return
            verified_by = str(data.get("verifiedBy") or "").strip()[:50]
            if not verified_by:
                self.send_json(400, {"error": "verifiedBy is required for verification."})
                return
            verified_at = datetime.datetime.now().isoformat()
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE animals SET verified = 1, verifiedBy = ?, verifiedAt = ?
                WHERE id = ? OR LOWER(tag) = LOWER(?)
            """, (verified_by, verified_at, animal_id, animal_id))
            if cursor.rowcount == 0:
                conn.close()
                self.send_json(404, {"error": "Animal not found"})
                return
            cursor.execute("""
                INSERT INTO verification_log (animalId, verifiedBy, verifiedAt, actor)
                VALUES (?, ?, ?, ?)
            """, (animal_id, verified_by, verified_at, "vet-token"))
            conn.commit()
            STATS_CACHE.clear()
            cursor.execute("SELECT * FROM animals WHERE id = ? OR LOWER(tag) = LOWER(?)", (animal_id, animal_id))
            updated = row_to_dict(cursor.fetchone())
            conn.close()
            self.send_json(200, updated)
            return

        if path == "/api/animals":
            if not self.check_auth():
                return

            data = self.parse_json_body()
            if data is None or data == "ERROR":
                return

            fields, error = clean_animal_fields(data)
            if error:
                self.send_json(400, {"error": error})
                return

            # IDs and timestamps are generated here, never trusted from the client.
            animal_id = str(uuid.uuid4())
            created_at = datetime.datetime.now().isoformat()
            try:
                photo_path = persist_photo(fields["photo"])
            except (ValueError, OSError) as exc:
                self.send_json(400, {"error": f"Could not store photo: {exc}"})
                return

            conn = get_db()
            cursor = conn.cursor()

            # Check duplicate tag
            cursor.execute("SELECT id FROM animals WHERE LOWER(tag) = LOWER(?)", (fields["tag"],))
            if cursor.fetchone():
                conn.close()
                self.send_json(409, {"error": f"Animal ID '{fields['tag']}' is already registered."})
                return

            try:
                cursor.execute("""
                    INSERT INTO animals (
                        id, name, tag, breed, dob, health,
                        lastVaccineDate, vaccineName, nextVaccineDate,
                        notes, photo, createdAt
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    animal_id,
                    fields["name"], fields["tag"], fields["breed"], fields["dob"],
                    fields["health"], fields["lastVaccineDate"], fields["vaccineName"],
                    fields["nextVaccineDate"], fields["notes"], photo_path, created_at
                ))
                conn.commit()
                STATS_CACHE.clear()
            except sqlite3.IntegrityError:
                conn.close()
                self.send_json(409, {"error": "Record conflicts with an existing ID or tag."})
                return
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

            fields, error = clean_animal_fields(data)
            if error:
                self.send_json(400, {"error": error})
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
            cursor.execute("SELECT id FROM animals WHERE LOWER(tag) = LOWER(?) AND id != ?", (fields["tag"], real_id))
            duplicate = cursor.fetchone()
            if duplicate:
                conn.close()
                self.send_json(409, {"error": f"Animal ID '{fields['tag']}' is already registered to another animal."})
                return

            # Keep existing photo if new photo is empty or omitted; verification is never overwritten here.
            try:
                photo_to_save = persist_photo(fields["photo"]) or current["photo"]
            except (ValueError, OSError) as exc:
                conn.close()
                self.send_json(400, {"error": f"Could not store photo: {exc}"})
                return

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
                fields["name"], fields["tag"], fields["breed"], fields["dob"],
                fields["health"], fields["lastVaccineDate"], fields["vaccineName"],
                fields["nextVaccineDate"], fields["notes"], photo_to_save, real_id
            ))
            conn.commit()
            STATS_CACHE.clear()

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
            cursor.execute("SELECT id, name, tag, photo FROM animals WHERE id = ? OR LOWER(tag) = LOWER(?)", (animal_id, animal_id))
            animal = cursor.fetchone()

            if not animal:
                conn.close()
                self.send_json(404, {"error": "Animal not found"})
                return

            real_id = animal["id"]
            cursor.execute("DELETE FROM animals WHERE id = ?", (real_id,))
            conn.commit()
            STATS_CACHE.clear()
            conn.close()

            # Remove the photo file too; otherwise deleted records leave orphaned uploads.
            photo = animal["photo"] or ""
            if photo.startswith("/uploads/"):
                try:
                    os.remove(os.path.join(UPLOAD_DIR, os.path.basename(photo)))
                except OSError:
                    pass  # already gone or unwritable: the database row is still deleted

            self.send_json(200, {
                "success": True,
                "message": f"Deleted {animal['name']} ({animal['tag']})"
            })
            return

        self.send_json(404, {"error": "Endpoint not found"})

def run_server(port=PORT):
    missing = [name for name, value in (
        ("FARM_ADMIN_TOKEN", ADMIN_TOKEN), ("FARM_VET_TOKEN", VET_TOKEN)
    ) if not value]
    if missing:
        raise SystemExit(
            f"{', '.join(missing)} not set. Set both before starting the server "
            "(see .env.example and README.md)."
        )
    configure_logging()
    init_db()
    # Local development listens only on loopback; BIND_HOST=0.0.0.0 is for a private container network.
    server_address = (BIND_HOST, port)
    httpd = ThreadingHTTPServer(server_address, CattleRequestHandler)
    LOGGER.info("CattleCare backend listening on %s:%s", BIND_HOST, port)
    LOGGER.info("Portal: http://localhost:%s/portal.html", port)
    LOGGER.info("API: http://localhost:%s/api/animals", port)
    LOGGER.info("Database: %s", DB_PATH)
    LOGGER.info("Uploads: %s", UPLOAD_DIR)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Stopping server gracefully")
        httpd.server_close()

if __name__ == "__main__":
    run_server()
