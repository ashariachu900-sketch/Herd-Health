"""
CattleCare Backend Server
A hardened, multi-threaded REST API server and static file server
powered by Python's standard library (ThreadingHTTPServer + sqlite3 in WAL mode).

Endpoints:
  GET    /api/health              - Liveness check
  POST   /api/session             - Exchange admin/vet token for an HttpOnly cookie + CSRF token
  GET    /api/session             - Restore the current browser session (no credentials in JS)
  POST   /api/logout              - Clear the session cookie
  GET    /api/animals             - List; safe by default. Authenticated admins get notes/photos;
                                   ?public=true or ?private=true (401 when unauthenticated) override.
  GET    /api/animals/<id>        - Full record for admins; public-safe passport for everyone else
  POST   /api/animals             - Create (admin session/token; server-generated id/createdAt)
  PUT    /api/animals/<id>        - Update (admin session/token; verification preserved)
  DELETE /api/animals/<id>        - Delete (admin session/token)
  POST   /api/animals/<id>/verify - Vet verification (vet session/token; writes audit log)
  GET    /api/stats               - Dashboard aggregates
  GET    /api/analytics           - Safe herd analytics for insights
  GET    /api/audit               - Append-only action log (admin)
  GET    /api/passport/<id-or-tag>- Public-safe passport (no notes or photos)
  GET    /api/animals/<id>/events - Care timeline (admin)
  POST   /api/animals/<id>/events - Add care event (admin)
  GET    /api/export              - Complete JSON backup incl. child tables and photo bytes
  GET    /uploads/<file>          - Uploaded animal photos
  Static files                   - index.html, portal.html, CSS, JS

FARM_ADMIN_TOKEN and FARM_VET_TOKEN are both required at startup, must differ, and
have no defaults. Browsers exchange a token once for a short-lived HttpOnly cookie;
writes from a cookie session require an X-CSRF-Token header. Header tokens remain
supported for CLI/backup clients. Failed guesses are throttled per client IP,
requests are bounded by a semaphore and a 10-second socket timeout, and JSON logs
rotate when CATTLECARE_LOG_DIR is set.

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
import secrets
import base64
import binascii
import logging
import logging.handlers
import threading
import sqlite3
import datetime
import urllib.parse
from http.cookies import SimpleCookie
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

# Browser sessions: the token is exchanged once for an HttpOnly cookie. The CSRF
# value is returned to JavaScript so writes cannot be triggered from another site.
SESSION_COOKIE_NAME = "cattlecare_session"
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "28800"))  # 8 hours
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1" or \
    os.environ.get("FORCE_SECURE_COOKIES", "0") == "1"
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()

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

def prune_failed_attempts_locked(now):
    """Bound the per-IP table; otherwise every guessed IP lives forever."""
    expired = [ip for ip, (_, last) in FAILED_ATTEMPTS.items()
               if now - last > ATTEMPT_WINDOW_SECONDS]
    for ip in expired:
        FAILED_ATTEMPTS.pop(ip, None)

def is_rate_limited(handler):
    ip = client_ip(handler)
    now = time.time()
    with FAILED_ATTEMPTS_LOCK:
        prune_failed_attempts_locked(now)
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
        prune_failed_attempts_locked(now)
        count, last = FAILED_ATTEMPTS.get(ip, (0, 0))
        if now - last > ATTEMPT_WINDOW_SECONDS:
            count = 0
        FAILED_ATTEMPTS[ip] = (count + 1, now)

def clear_auth_failures(handler):
    with FAILED_ATTEMPTS_LOCK:
        FAILED_ATTEMPTS.pop(client_ip(handler), None)

def request_is_secure(handler):
    """Caddy terminates TLS; trust its forwarded-proto header only on loopback/proxy."""
    if COOKIE_SECURE:
        return True
    forwarded = handler.headers.get("X-Forwarded-Proto", "")
    return forwarded.split(",")[0].strip().lower() == "https"

def prune_sessions_locked(now):
    for sid, session in list(SESSIONS.items()):
        if session["expires"] <= now:
            SESSIONS.pop(sid, None)

def get_session(handler):
    """Return a live session dict from the HttpOnly cookie, or None."""
    raw_cookie = handler.headers.get("Cookie", "")
    if not raw_cookie:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(raw_cookie)
    except Exception:
        return None
    morsel = cookie.get(SESSION_COOKIE_NAME)
    if not morsel:
        return None
    now = time.time()
    with SESSIONS_LOCK:
        prune_sessions_locked(now)
        session = SESSIONS.get(morsel.value)
        return dict(session) if session else None

def create_session(role, actor, secure=False):
    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    session = {
        "role": role,
        "actor": actor[:50] or role,
        "csrf": csrf,
        "expires": time.time() + SESSION_TTL_SECONDS,
        "secure": bool(secure),
    }
    with SESSIONS_LOCK:
        prune_sessions_locked(time.time())
        SESSIONS[sid] = session
    return sid, session

def session_cookie_header(sid, secure, max_age=SESSION_TTL_SECONDS):
    parts = [f"{SESSION_COOKIE_NAME}={sid}", "Path=/", "HttpOnly", "SameSite=Strict",
             f"Max-Age={max_age}"]
    if secure:
        parts.append("Secure")
    return "Set-Cookie", "; ".join(parts)

def extract_token(handler, role=None):
    """Read the header for the requested role before any bearer fallback.

    A client may legitimately send both headers (for example a vet script run by
    an admin); preferring the admin header for every role used to block that.
    """
    if role == "admin":
        candidates = [handler.headers.get("X-Admin-Token")]
    elif role == "vet":
        candidates = [handler.headers.get("X-Vet-Token")]
    else:
        candidates = [handler.headers.get("X-Admin-Token"), handler.headers.get("X-Vet-Token")]
    authorization = handler.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        candidates.append(authorization[7:].strip())
    return next((value for value in candidates if value), "")

def auth_role(handler, role=None):
    """Return (role, session_or_None). Does not emit challenges or throttling."""
    session = get_session(handler)
    if session and (role is None or session["role"] == role):
        return session["role"], session
    token = extract_token(handler, role)
    if token and ADMIN_TOKEN and hmac.compare_digest(token, ADMIN_TOKEN):
        return "admin", None
    if token and VET_TOKEN and hmac.compare_digest(token, VET_TOKEN):
        return "vet", None
    return None, None

def actor_for(handler, fallback):
    return getattr(handler, "audit_actor", None) or fallback

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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

def ensure_sequence_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_sequences (
            name TEXT PRIMARY KEY,
            nextValue INTEGER NOT NULL
        )
    """)
    row = conn.execute(
        "SELECT nextValue FROM app_sequences WHERE name = 'animal_tag'"
    ).fetchone()
    if row:
        return
    max_number = 0
    for animal in conn.execute("SELECT tag FROM animals").fetchall():
        match = re.fullmatch(r"COW-(\d+)", str(animal["tag"] or ""), re.IGNORECASE)
        if match:
            max_number = max(max_number, int(match.group(1)))
    conn.execute(
        "INSERT INTO app_sequences (name, nextValue) VALUES ('animal_tag', ?)",
        (max_number + 1,)
    )

def generate_animal_tag(cursor):
    row = cursor.execute(
        "SELECT nextValue FROM app_sequences WHERE name = 'animal_tag'"
    ).fetchone()
    number = int(row["nextValue"]) if row else 1
    while True:
        tag = f"COW-{number:04d}"
        collision = cursor.execute(
            "SELECT 1 FROM animals WHERE LOWER(tag) = LOWER(?)", (tag,)
        ).fetchone()
        if not collision:
            break
        number += 1
    cursor.execute(
        "UPDATE app_sequences SET nextValue = ? WHERE name = 'animal_tag'",
        (number + 1,)
    )
    return tag

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
    ensure_sequence_table(conn)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS verification_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            animalId TEXT NOT NULL,
            verifiedBy TEXT NOT NULL,
            verifiedAt TEXT NOT NULL,
            actor TEXT NOT NULL,
            FOREIGN KEY(animalId) REFERENCES animals(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_verification_animal ON verification_log (animalId);")

    # Schema version 2 adds a care timeline and one append-only action log.
    # PRAGMA user_version survives restarts and lets future upgrades migrate safely.
    if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS care_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                animalId TEXT NOT NULL,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                details TEXT,
                occurredAt TEXT NOT NULL,
                createdAt TEXT NOT NULL,
                actor TEXT NOT NULL,
                FOREIGN KEY(animalId) REFERENCES animals(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_animal ON care_events (animalId, occurredAt DESC);")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                entityId TEXT NOT NULL,
                summary TEXT NOT NULL,
                actor TEXT NOT NULL,
                createdAt TEXT NOT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log (createdAt DESC);")
        conn.execute("PRAGMA user_version = 2")

    care_event_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(care_events)")
    }
    if "vaccineName" not in care_event_columns:
        conn.execute("ALTER TABLE care_events ADD COLUMN vaccineName TEXT")

    # The audit trail is append-only at the storage layer, not just by convention.
    # Created on every startup so databases migrated by an earlier version also
    # get the protection.
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log (createdAt DESC);")
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS audit_log_no_update
        BEFORE UPDATE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
    """)
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
        BEFORE DELETE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
    """)

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

EVENT_TYPES = ("vaccination", "treatment", "health_check", "observation", "movement")

def safe_animal_payload(conn, row):
    """The only fields an unauthenticated reader may receive. No notes, photos or actor names."""
    safe_events = conn.execute("""
        SELECT type, title, occurredAt FROM care_events
        WHERE animalId = ? AND type IN ('vaccination', 'health_check')
        ORDER BY occurredAt DESC LIMIT 5
    """, (row["id"],)).fetchall()
    return {
        "id": row["id"],
        "name": row["name"],
        "tag": row["tag"],
        "breed": row["breed"] or "",
        "dob": row["dob"] or "",
        "createdAt": row["createdAt"] or "",
        "health": row["health"] or "Healthy",
        "lastVaccineDate": row["lastVaccineDate"] or "",
        "vaccineName": row["vaccineName"] or "",
        "nextVaccineDate": row["nextVaccineDate"] or "",
        "verified": bool(row["verified"]) if "verified" in row.keys() else False,
        "verifiedBy": (row["verifiedBy"] or "") if "verifiedBy" in row.keys() else "",
        "verifiedAt": (row["verifiedAt"] or "") if "verifiedAt" in row.keys() else "",
        "careHighlights": [dict(e) for e in safe_events],
    }

def write_audit(conn, action, entity_id, summary, actor="farmer"):
    """Append a mutation to the audit log in the caller's transaction."""
    conn.execute("""
        INSERT INTO audit_log (action, entityId, summary, actor, createdAt)
        VALUES (?, ?, ?, ?, ?)
    """, (action, str(entity_id), str(summary)[:200], str(actor), datetime.datetime.now().isoformat()))

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

    if not fields["name"]:
        return None, "Animal name is required."

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
    encoded = match.group(2)
    encoded += "=" * (-len(encoded) % 4)  # tolerate data URIs without padding
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid base64 image") from exc
    if len(data) > MAX_BODY_SIZE:
        raise ValueError("Photo exceeds 5MB limit")
    signatures = {
        "png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpeg": data.startswith(b"\xff\xd8\xff"),
        "gif": data.startswith((b"GIF87a", b"GIF89a")),
        "webp": len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    if not signatures[match.group(1)]:
        raise ValueError("Base64 content does not match its declared image format")
    file_name = f"{uuid.uuid4()}.{extension}"
    with open(os.path.join(UPLOAD_DIR, file_name), "wb") as handle:
        handle.write(data)
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

    SECURITY_HEADERS = (
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Content-Security-Policy",
         "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
         "style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; "
         "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"),
    )

    def end_headers(self):
        # Applies to API and static responses. The flag prevents duplicates when
        # send_json() and SimpleHTTPRequestHandler both call end_headers().
        if not getattr(self, "_security_headers_sent", False):
            self._security_headers_sent = True
            for key, value in self.SECURITY_HEADERS:
                self.send_header(key, value)
        super().end_headers()

    def send_json(self, status_code, data, extra_headers=None):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
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

    def require_role(self, role, write=False):
        """Authorize a request. Cookie sessions must also send the matching CSRF header on writes."""
        actual, session = auth_role(self, role)
        if actual == role:
            if write and session:
                csrf = self.headers.get("X-CSRF-Token", "")
                if not csrf or not hmac.compare_digest(csrf, session["csrf"]):
                    self.send_json(403, {"error": "Missing or invalid X-CSRF-Token."})
                    return False
            clear_auth_failures(self)
            self.audit_actor = session["actor"] if session else ("farmer" if role == "admin" else "vet-token")
            return True
        if not is_rate_limited(self):
            record_auth_failure(self)
            LOGGER.warning("Rejected %s credential from %s", role, client_ip(self))
            header = "X-Admin-Token" if role == "admin" else "X-Vet-Token"
            self.send_json(401, {"error": f"Unauthorized: Valid {header} (or {role} session) required."})
        return False

    def check_auth(self, write=False):
        return self.require_role("admin", write=write)

    def check_vet_auth(self, write=False):
        return self.require_role("vet", write=write)

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

        if path == "/api/session":
            session = get_session(self)
            if not session:
                # Not an error: read-only visitors are expected on every page load.
                self.send_json(200, {"authenticated": False, "role": None})
                return
            self.send_json(200, {
                "authenticated": True,
                "role": session["role"],
                "actor": session["actor"],
                "csrfToken": session["csrf"],
                "expiresAt": datetime.datetime.fromtimestamp(session["expires"]).isoformat()
            })
            return

        if path == "/api/export":
            if not self.check_auth():
                return
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM animals ORDER BY createdAt DESC")
            animals = [row_to_dict(r) for r in cursor.fetchall()]
            tables = {}
            for name in ("care_events", "verification_log", "audit_log"):
                cursor.execute(f"SELECT * FROM {name} ORDER BY id")
                tables[name] = [dict(r) for r in cursor.fetchall()]
            conn.close()

            photos = []
            photo_exts = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                          ".gif": "image/gif", ".webp": "image/webp"}
            for animal in animals:
                relative = animal.get("photo") or ""
                if not relative.startswith("/uploads/"):
                    continue
                file_name = os.path.basename(relative)
                file_path = os.path.join(UPLOAD_DIR, file_name)
                if not os.path.isfile(file_path):
                    continue
                mime = photo_exts.get(os.path.splitext(file_name)[1].lower(), "application/octet-stream")
                with open(file_path, "rb") as handle:
                    photos.append({
                        "path": relative,
                        "mimeType": mime,
                        "data": base64.b64encode(handle.read()).decode("ascii")
                    })

            self.send_json(200, {
                "schemaVersion": 3,
                "exportedAt": datetime.datetime.now().isoformat(),
                "totalRecords": len(animals),
                "animals": animals,
                "careEvents": tables["care_events"],
                "verificationLog": tables["verification_log"],
                "auditLog": tables["audit_log"],
                "photos": photos
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

        if path == "/api/analytics":
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT health, COUNT(*) AS n FROM animals GROUP BY health")
            health = {(r["health"] or "Unknown"): r["n"] for r in cursor.fetchall()}
            cursor.execute("""
                SELECT CASE
                         WHEN nextVaccineDate IS NULL OR nextVaccineDate = '' THEN 'none'
                         WHEN date(nextVaccineDate) <= date('now') THEN 'due'
                         ELSE 'upcoming'
                       END AS bucket,
                       COUNT(*) AS n
                FROM animals GROUP BY bucket
            """)
            vaccines = {r["bucket"]: r["n"] for r in cursor.fetchall()}
            cursor.execute("""
                SELECT breed, COUNT(*) AS n FROM animals
                WHERE breed IS NOT NULL AND breed != ''
                GROUP BY breed ORDER BY n DESC, breed ASC LIMIT 5
            """)
            breeds = [{"breed": r["breed"], "count": r["n"]} for r in cursor.fetchall()]
            cursor.execute("""
                SELECT SUM(CASE WHEN verified = 1 THEN 1 ELSE 0 END) AS verified, COUNT(*) AS total
                FROM animals
            """)
            verify = cursor.fetchone()
            cursor.execute("SELECT type, COUNT(*) AS n FROM care_events GROUP BY type")
            events = {r["type"]: r["n"] for r in cursor.fetchall()}
            conn.close()
            self.send_json(200, {
                "health": health,
                "vaccines": vaccines,
                "topBreeds": breeds,
                "verifiedCount": verify["verified"] or 0,
                "total": verify["total"] or 0,
                "eventTypes": events
            })
            return

        if path == "/api/audit":
            if not self.check_auth():
                return
            conn = get_db()
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY createdAt DESC, id DESC LIMIT 100"
            ).fetchall()
            conn.close()
            self.send_json(200, [dict(r) for r in rows])
            return

        if path.startswith("/api/passport/"):
            animal_id = urllib.parse.unquote(path[len("/api/passport/"):].strip())
            conn = get_db()
            row = conn.execute(
                "SELECT * FROM animals WHERE id = ? OR LOWER(tag) = LOWER(?)", (animal_id, animal_id)
            ).fetchone()
            if not row:
                conn.close()
                self.send_json(404, {"error": "Animal not found"})
                return
            payload = safe_animal_payload(conn, row)
            conn.close()
            self.send_json(200, payload)
            return

        if path.startswith("/api/animals/") and path.endswith("/events"):
            if not self.check_auth():
                return
            animal_id = urllib.parse.unquote(path[len("/api/animals/"):-len("/events")].strip("/"))
            conn = get_db()
            record = conn.execute(
                "SELECT id FROM animals WHERE id = ? OR LOWER(tag) = LOWER(?)", (animal_id, animal_id)
            ).fetchone()
            if not record:
                conn.close()
                self.send_json(404, {"error": "Animal not found"})
                return
            rows = conn.execute("""
                SELECT e.*, a.name AS animalName, a.tag AS animalTag,
                       COALESCE(e.vaccineName, a.vaccineName) AS vaccineName
                FROM care_events e
                JOIN animals a ON a.id = e.animalId
                WHERE e.animalId = ?
                ORDER BY e.occurredAt DESC, e.id DESC LIMIT 100
            """, (record["id"],)).fetchall()
            conn.close()
            self.send_json(200, [dict(r) for r in rows])
            return

        if path == "/api/animals":
            search = query.get("search", [""])[0].strip().lower()
            tag_filter = query.get("tag", [""])[0].strip().lower()
            health_filter = query.get("health", ["all"])[0].strip().lower()
            # ?summary=true — omit large base64 photo blobs from list responses.
            # Callers that need the full photo should fetch /api/animals/<id>.
            summary_mode = query.get("summary", ["false"])[0].strip().lower() == "true"
            # Safe by default: only a live admin session/token unlocks notes and photos.
            authenticated, _ = auth_role(self)
            private_requested = query.get("private", ["false"])[0].strip().lower() == "true"
            if private_requested and authenticated != "admin":
                if not self.check_auth():
                    return
            public_mode = authenticated != "admin" or \
                query.get("public", ["false"])[0].strip().lower() == "true"
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
                search_columns = ["LOWER(name) LIKE ? ESCAPE '\\'",
                                  "LOWER(tag) LIKE ? ESCAPE '\\'",
                                  "LOWER(breed) LIKE ? ESCAPE '\\'"]
                params.extend([like] * len(search_columns))
                # Searching notes would leak private text through a public endpoint.
                if public_mode:
                    clauses.append("(" + " OR ".join(search_columns) + ")")
                else:
                    search_columns.append("LOWER(COALESCE(notes, '')) LIKE ? ESCAPE '\\'")
                    params.append(like)
                    clauses.append("(" + " OR ".join(search_columns) + ")")
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
                if public_mode:
                    animal.pop("notes", None)
                    animal.pop("photo", None)
                    animal["hasPhoto"] = False
                results.append(animal)

            self.send_json(200, results, {"X-Total-Count": str(total_count)})
            return

        if path.startswith("/api/animals/"):
            animal_id = urllib.parse.unquote(path.replace("/api/animals/", "").strip())
            authenticated, _ = auth_role(self)
            conn = get_db()
            row = conn.execute(
                "SELECT * FROM animals WHERE id = ? OR LOWER(tag) = LOWER(?)", (animal_id, animal_id)
            ).fetchone()
            if not row:
                conn.close()
                self.send_json(404, {"error": "Animal not found"})
                return
            # A scanned QR or guessed tag never receives private notes or a photo path.
            payload = row_to_dict(row) if authenticated == "admin" else safe_animal_payload(conn, row)
            conn.close()
            self.send_json(200, payload)
            return

        # Protected static files check (prevent downloading raw database, source code, or configs)
        BLOCKED_FILES = {
            "cattlecare.db", "cattlecare.db-wal", "cattlecare.db-shm",
            "server.py", "test_server.py", "backup.py",
            "restore.py", "run_server.bat",
            "caddyfile", "docker-compose.yml", ".gitignore", "readme.md"
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

        if path == "/api/session":
            data = self.parse_json_body()
            if data is None or data == "ERROR":
                return
            token = str(data.get("token") or "")
            role = None
            if token and ADMIN_TOKEN and hmac.compare_digest(token, ADMIN_TOKEN):
                role = "admin"
            elif token and VET_TOKEN and hmac.compare_digest(token, VET_TOKEN):
                role = "vet"
            if not role:
                if not is_rate_limited(self):
                    record_auth_failure(self)
                    self.send_json(401, {"error": "Unauthorized: invalid session token."})
                return
            clear_auth_failures(self)
            operator = str(data.get("operator") or "").strip()[:50]
            actor = operator or ("Farmer" if role == "admin" else "Veterinarian")
            sid, session = create_session(role, actor, request_is_secure(self))
            self.send_json(201, {
                "role": role,
                "actor": session["actor"],
                "csrfToken": session["csrf"],
                "expiresAt": datetime.datetime.fromtimestamp(session["expires"]).isoformat()
            }, {"Set-Cookie": session_cookie_header(sid, request_is_secure(self))[1]})
            return

        if path == "/api/logout":
            session = get_session(self)
            if session:
                with SESSIONS_LOCK:
                    # Cookie value is needed to remove the exact session.
                    raw = SimpleCookie()
                    raw.load(self.headers.get("Cookie", ""))
                    morsel = raw.get(SESSION_COOKIE_NAME)
                    if morsel:
                        SESSIONS.pop(morsel.value, None)
            secure = session["secure"] if session else request_is_secure(self)
            self.send_json(200, {"success": True}, {
                "Set-Cookie": session_cookie_header("", secure, max_age=0)[1]
            })
            return

        # Care timeline events: structured history instead of one free-text notes blob.
        if path.startswith("/api/animals/") and path.endswith("/events"):
            if not self.check_auth(write=True):
                return
            animal_id = urllib.parse.unquote(path[len("/api/animals/"):-len("/events")].strip("/"))
            data = self.parse_json_body()
            if data is None or data == "ERROR":
                return
            event_type = str(data.get("type") or "").strip()
            title = str(data.get("title") or "").strip()[:80]
            details = str(data.get("details") or "").strip()[:500]
            occurred = str(data.get("occurredAt") or "").strip()
            vaccine_name = str(data.get("vaccineName") or "").strip()[:70]
            next_vaccine = str(data.get("nextVaccineDate") or "").strip()
            if event_type not in EVENT_TYPES:
                self.send_json(400, {"error": "Invalid event type."})
                return
            if not title or not valid_iso_date(occurred):
                self.send_json(400, {"error": "title and a valid occurredAt date are required."})
                return
            if next_vaccine and not valid_iso_date(next_vaccine):
                self.send_json(400, {"error": "nextVaccineDate must be a valid YYYY-MM-DD date or empty."})
                return
            conn = get_db()
            cursor = conn.cursor()
            record = cursor.execute(
                "SELECT id, name, tag, vaccineName FROM animals WHERE id = ? OR LOWER(tag) = LOWER(?)",
                (animal_id, animal_id)
            ).fetchone()
            if not record:
                conn.close()
                self.send_json(404, {"error": "Animal not found"})
                return
            actor = actor_for(self, "farmer")
            created_at = datetime.datetime.now().isoformat()
            linked_vaccine_name = vaccine_name or (title if event_type == "vaccination" else "")
            cursor.execute("""
                INSERT INTO care_events (
                    animalId, type, title, details, occurredAt, createdAt, actor, vaccineName
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record["id"], event_type, title, details, occurred, created_at, actor,
                linked_vaccine_name
            ))
            if event_type == "vaccination":
                cursor.execute("""
                    UPDATE animals
                    SET vaccineName = ?,
                        lastVaccineDate = ?,
                        nextVaccineDate = CASE WHEN ? <> '' THEN ? ELSE nextVaccineDate END
                    WHERE id = ?
                """, (
                    linked_vaccine_name,
                    occurred,
                    next_vaccine,
                    next_vaccine,
                    record["id"]
                ))
                STATS_CACHE.clear()
            write_audit(conn, "event", record["id"], f"{event_type}: {title}", actor)
            conn.commit()
            event_id = cursor.lastrowid
            conn.close()
            self.send_json(201, {
                "id": event_id, "animalId": record["id"], "type": event_type,
                "title": title, "details": details, "occurredAt": occurred,
                "createdAt": created_at, "actor": actor,
                "animalName": record["name"], "animalTag": record["tag"],
                "vaccineName": linked_vaccine_name
            })
            return

        # Vet verification requires a separate vet token and writes an audit row.
        if path.startswith("/api/animals/") and path.endswith("/verify"):
            if not self.check_vet_auth(write=True):
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
            actor = actor_for(self, "vet-token")
            conn = get_db()
            cursor = conn.cursor()
            record = cursor.execute(
                "SELECT id FROM animals WHERE id = ? OR LOWER(tag) = LOWER(?)", (animal_id, animal_id)
            ).fetchone()
            if not record:
                conn.close()
                self.send_json(404, {"error": "Animal not found"})
                return
            real_id = record["id"]
            cursor.execute("""
                UPDATE animals SET verified = 1, verifiedBy = ?, verifiedAt = ? WHERE id = ?
            """, (verified_by, verified_at, real_id))
            cursor.execute("""
                INSERT INTO verification_log (animalId, verifiedBy, verifiedAt, actor)
                VALUES (?, ?, ?, ?)
            """, (real_id, verified_by, verified_at, actor))
            write_audit(conn, "verify", real_id, f"Vet verification by {verified_by}", actor)
            conn.commit()
            STATS_CACHE.clear()
            updated = row_to_dict(cursor.execute(
                "SELECT * FROM animals WHERE id = ?", (real_id,)
            ).fetchone())
            conn.close()
            self.send_json(200, updated)
            return

        if path == "/api/animals":
            if not self.check_auth(write=True):
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

            conn = get_db()
            cursor = conn.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                supplied_tag = str(fields["tag"] or "").strip()
                if supplied_tag:
                    collision = cursor.execute(
                        "SELECT 1 FROM animals WHERE LOWER(tag) = LOWER(?)", (supplied_tag,)
                    ).fetchone()
                    if collision:
                        conn.rollback()
                        conn.close()
                        self.send_json(409, {"error": f"Animal ID '{supplied_tag}' is already registered."})
                        return
                    animal_tag = supplied_tag
                else:
                    animal_tag = generate_animal_tag(cursor)

                try:
                    photo_path = persist_photo(fields["photo"])
                except (ValueError, OSError) as exc:
                    conn.rollback()
                    conn.close()
                    self.send_json(400, {"error": f"Could not store photo: {exc}"})
                    return

                cursor.execute("""
                    INSERT INTO animals (
                        id, name, tag, breed, dob, health,
                        lastVaccineDate, vaccineName, nextVaccineDate,
                        notes, photo, createdAt
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    animal_id,
                    fields["name"], animal_tag, fields["breed"], fields["dob"],
                    fields["health"], fields["lastVaccineDate"], fields["vaccineName"],
                    fields["nextVaccineDate"], fields["notes"], photo_path, created_at
                ))
                write_audit(conn, "create", animal_id,
                            f"Registered {fields['name']} ({animal_tag})",
                            actor_for(self, "farmer"))
                conn.commit()
                STATS_CACHE.clear()
            except sqlite3.IntegrityError:
                if photo_path.startswith("/uploads/"):
                    try:
                        os.remove(os.path.join(UPLOAD_DIR, os.path.basename(photo_path)))
                    except OSError:
                        pass
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
            if not self.check_auth(write=True):
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
            # Tags are permanent identifiers: edits never change or regenerate them.
            animal_tag = current["tag"]

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
                fields["name"], animal_tag, fields["breed"], fields["dob"],
                fields["health"], fields["lastVaccineDate"], fields["vaccineName"],
                fields["nextVaccineDate"], fields["notes"], photo_to_save, real_id
            ))
            write_audit(conn, "update", real_id,
                        f"Updated {fields['name']} ({fields['tag']})",
                        actor_for(self, "farmer"))
            conn.commit()
            STATS_CACHE.clear()
            old_photo = current["photo"] or ""
            if old_photo.startswith("/uploads/") and old_photo != photo_to_save:
                try:
                    os.remove(os.path.join(UPLOAD_DIR, os.path.basename(old_photo)))
                except OSError:
                    pass

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
            if not self.check_auth(write=True):
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
            # Child rows have no independent meaning once the animal is gone. The
            # audit_log row is intentionally kept as the tamper-evident history.
            cursor.execute("DELETE FROM care_events WHERE animalId = ?", (real_id,))
            cursor.execute("DELETE FROM verification_log WHERE animalId = ?", (real_id,))
            cursor.execute("DELETE FROM animals WHERE id = ?", (real_id,))
            write_audit(conn, "delete", real_id,
                        f"Deleted {animal['name']} ({animal['tag']})",
                        actor_for(self, "farmer"))
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
    if hmac.compare_digest(ADMIN_TOKEN, VET_TOKEN):
        raise SystemExit("FARM_ADMIN_TOKEN and FARM_VET_TOKEN must be different.")
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
