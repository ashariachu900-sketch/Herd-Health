"""
Automated Test Suite for CattleCare Backend
Uses Python's standard unittest library - no external packages required.

Execute via:
    python -m unittest test_server.py -v
"""

import os
import io
import sys
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import datetime
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import server

TEST_TOKEN = "unit-test-token"
TEST_VET_TOKEN = "unit-test-vet-token"

class InProcessTestClient:
    """Simulates HTTP requests into CattleRequestHandler without requiring OS TCP sockets."""
    def request(self, method, path, data=None, headers=None):
        req = server.CattleRequestHandler.__new__(server.CattleRequestHandler)
        body_bytes = json.dumps(data).encode("utf-8") if data is not None else b""
        req.rfile = io.BytesIO(body_bytes)
        req.wfile = io.BytesIO()
        req.path = path
        req.command = method
        req.headers = {"X-Admin-Token": TEST_TOKEN}
        if headers:
            req.headers.update(headers)
        req.headers["Content-Length"] = str(len(body_bytes))
        req.headers_out = {}

        status_code = [200]
        def mock_send_response(code, msg=None):
            status_code[0] = code

        req.send_response = mock_send_response
        req.send_header = lambda key, value: req.headers_out.__setitem__(key, value)
        req.end_headers = lambda: None

        if method == "GET":
            req.do_GET()
        elif method == "POST":
            req.do_POST()
        elif method == "PUT":
            req.do_PUT()
        elif method == "DELETE":
            req.do_DELETE()

        raw_output = req.wfile.getvalue().decode("utf-8")
        self.headers_out = req.headers_out
        try:
            return status_code[0], json.loads(raw_output) if raw_output else {}
        except Exception:
            return status_code[0], {"raw": raw_output}

class CattleCareBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp(prefix="cattlecare_test_")
        cls.test_db = os.path.join(cls.test_dir, "test_cattlecare.db")
        cls.original_db_path = server.DB_PATH
        cls.original_token = server.ADMIN_TOKEN
        cls.original_vet_token = server.VET_TOKEN
        cls.original_upload_dir = server.UPLOAD_DIR
        cls.original_seed = os.environ.get("SEED_DEMO_DATA")
        os.environ["SEED_DEMO_DATA"] = "1"  # seed the isolated test database
        server.DB_PATH = cls.test_db
        server.ADMIN_TOKEN = TEST_TOKEN
        server.VET_TOKEN = TEST_VET_TOKEN
        server.UPLOAD_DIR = os.path.join(cls.test_dir, "uploads")
        server.init_db()
        cls.client = InProcessTestClient()

    @classmethod
    def tearDownClass(cls):
        server.DB_PATH = cls.original_db_path
        server.ADMIN_TOKEN = cls.original_token
        server.VET_TOKEN = cls.original_vet_token
        server.UPLOAD_DIR = cls.original_upload_dir
        if cls.original_seed is None:
            os.environ.pop("SEED_DEMO_DATA", None)
        else:
            os.environ["SEED_DEMO_DATA"] = cls.original_seed
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def setUp(self):
        # Throttling and the stats cache are process-wide; isolate them per test.
        server.FAILED_ATTEMPTS.clear()
        server.STATS_CACHE.clear()

    # --------------------------------------------------------------------------
    # 1. Logic & Calculation Tests
    # --------------------------------------------------------------------------

    def test_01_vaccine_status_calculations(self):
        today = datetime.date.today()
        self.assertEqual(server.vaccine_status(today.isoformat()), "today")
        self.assertEqual(server.vaccine_status((today - datetime.timedelta(days=1)).isoformat()), "overdue")
        self.assertEqual(server.vaccine_status((today + datetime.timedelta(days=10)).isoformat()), "upcoming")
        self.assertEqual(server.vaccine_status(""), "none")
        self.assertEqual(server.vaccine_status(None), "none")

    def test_02_database_schema_and_indexes(self):
        conn = server.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='animals';")
        self.assertIsNotNone(cursor.fetchone())
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index';")
        index_names = [row["name"] for row in cursor.fetchall()]
        for expected in ("idx_animals_created", "idx_animals_health", "idx_animals_tag"):
            self.assertIn(expected, index_names)
        cursor.execute("PRAGMA table_info(animals);")
        columns = {row["name"] for row in cursor.fetchall()}
        self.assertTrue({"verified", "verifiedBy", "verifiedAt"}.issubset(columns))
        cursor.execute("SELECT COUNT(*) as cnt FROM animals;")
        self.assertGreaterEqual(cursor.fetchone()["cnt"], 3)
        conn.close()

    # --------------------------------------------------------------------------
    # 2. Read Endpoints
    # --------------------------------------------------------------------------

    def test_03_api_health_endpoint(self):
        status, data = self.client.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("status"), "ok")
        self.assertIn("service", data)

    def test_04_api_stats_endpoint(self):
        status, data = self.client.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        for key in ("totalCattle", "healthyCattle", "healthPercent", "upcoming"):
            self.assertIn(key, data)
        self.assertIsInstance(data["upcoming"], list)

    def test_05_api_animals_list_and_filters(self):
        status, animals = self.client.request("GET", "/api/animals")
        self.assertEqual(status, 200)
        self.assertIsInstance(animals, list)
        self.assertGreaterEqual(len(animals), 3)

        status, healthy = self.client.request("GET", "/api/animals?health=Healthy")
        self.assertEqual(status, 200)
        self.assertTrue(all(a["health"] == "Healthy" for a in healthy))

        status, searched = self.client.request("GET", "/api/animals?search=Bella")
        self.assertEqual(status, 200)
        self.assertTrue(any(a["name"] == "Bella" for a in searched))

        status, tag_match = self.client.request("GET", "/api/animals?tag=COW-101")
        self.assertEqual(status, 200)
        self.assertEqual(len(tag_match), 1)

    # --------------------------------------------------------------------------
    # 3. CRUD Lifecycle
    # --------------------------------------------------------------------------

    def test_06_crud_lifecycle_with_server_generated_id(self):
        new_animal = {
            "id": "client-chosen-id",             # must be ignored
            "createdAt": "2000-01-01T00:00:00",   # must be ignored
            "name": "Moona",
            "tag": "TEST-COW-999",
            "breed": "Simmental",
            "dob": "2023-04-10",
            "health": "Healthy",
            "vaccineName": "Anthrax Spore",
            "nextVaccineDate": "2026-12-20",
            "notes": "Healthy test heifer.",
            "photo": "data:image/jpeg;base64,samplephoto123"
        }
        status, created = self.client.request("POST", "/api/animals", data=new_animal)
        self.assertEqual(status, 201)
        self.assertNotEqual(created["id"], "client-chosen-id")
        self.assertNotEqual(created["createdAt"], "2000-01-01T00:00:00")
        created_id = created["id"]

        dup_status, _ = self.client.request("POST", "/api/animals", data=new_animal)
        self.assertEqual(dup_status, 409)

        status, fetched = self.client.request("GET", f"/api/animals/{created_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["name"], "Moona")

        status, by_tag = self.client.request("GET", "/api/animals/TEST-COW-999")
        self.assertEqual(status, 200)
        self.assertEqual(by_tag["id"], created_id)

        update = {
            "name": "Moona Champion", "tag": "TEST-COW-999",
            "health": "Needs attention", "photo": ""
        }
        status, updated = self.client.request("PUT", f"/api/animals/{created_id}", data=update)
        self.assertEqual(status, 200)
        self.assertEqual(updated["name"], "Moona Champion")
        self.assertTrue(updated["photo"].startswith("/uploads/"))

        status, _ = self.client.request("DELETE", f"/api/animals/{created_id}")
        self.assertEqual(status, 200)
        status, _ = self.client.request("GET", f"/api/animals/{created_id}")
        self.assertEqual(status, 404)

    # --------------------------------------------------------------------------
    # 4. Security & Validation
    # --------------------------------------------------------------------------

    def test_07_export_requires_token(self):
        status, _ = self.client.request("GET", "/api/export")
        self.assertEqual(status, 200, "Default test client supplies a valid token")
        status, body = self.client.request("GET", "/api/export", headers={"X-Admin-Token": ""})
        self.assertEqual(status, 401)
        self.assertIn("error", body)

    def test_08_payload_size_limit(self):
        oversized = {"junk": "x" * (6 * 1024 * 1024)}
        status, _ = self.client.request("POST", "/api/animals", data=oversized)
        self.assertEqual(status, 413)

    def test_09_blocked_sensitive_files(self):
        for path in ("/cattlecare.db", "/server.py", "/test_server.py", "/run_server.bat", "/.gitignore", "/.env.example"):
            status, _ = self.client.request("GET", path)
            self.assertEqual(status, 403, f"{path} must be blocked")

    def test_10_unauthorized_mutations(self):
        for token in ("invalid-hacker-token", "REDACTED-OLD-SECRET", ""):
            status, _ = self.client.request(
                "POST", "/api/animals",
                data={"name": "Hacked", "tag": "HACK-01"},
                headers={"X-Admin-Token": token}
            )
            self.assertEqual(status, 401, f"Token {token!r} must be rejected")

    def test_11_summary_param_excludes_photos(self):
        payload = {
            "name": "SummaryTest", "tag": "SUM-001",
            "photo": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        }
        status, _ = self.client.request("POST", "/api/animals", data=payload)
        self.assertEqual(status, 201)
        status, summary = self.client.request("GET", "/api/animals?summary=true")
        self.assertEqual(status, 200)
        found = next(a for a in summary if a["tag"] == "SUM-001")
        self.assertNotIn("photo", found)
        self.assertTrue(found["hasPhoto"])
        status, full = self.client.request("GET", "/api/animals")
        found_full = next(a for a in full if a["tag"] == "SUM-001")
        self.assertIn("photo", found_full)
        self.client.request("DELETE", f"/api/animals/{found_full['id']}")

    def test_12_rejects_unsafe_photo(self):
        payload = {"name": "BadPhoto", "tag": "BAD-PHOTO", "photo": 'data:image/png" onerror="alert(1)'}
        status, body = self.client.request("POST", "/api/animals", data=payload)
        self.assertEqual(status, 400)
        self.assertIn("Photo", body["error"])

    def test_13_rejects_bad_health_and_dates(self):
        bad_health = {"name": "BadHealth", "tag": "BAD-HEALTH", "health": "Cured"}
        status, _ = self.client.request("POST", "/api/animals", data=bad_health)
        self.assertEqual(status, 400)
        bad_date = {"name": "BadDate", "tag": "BAD-DATE", "dob": "not-a-date"}
        status, _ = self.client.request("POST", "/api/animals", data=bad_date)
        self.assertEqual(status, 400)

    def test_14_verification_is_explicit_and_survives_updates(self):
        status, created = self.client.request(
            "POST", "/api/animals", {"name": "VerifyMe", "tag": "VER-001"}
        )
        self.assertEqual(status, 201)
        aid = created["id"]
        self.assertFalse(created["verified"])

        status, verified = self.client.request(
            "POST", f"/api/animals/{aid}/verify", {"verifiedBy": "Dr Rao"},
            headers={"X-Vet-Token": TEST_VET_TOKEN}
        )
        self.assertEqual(status, 200)
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["verifiedBy"], "Dr Rao")
        self.assertTrue(verified["verifiedAt"])

        conn = server.get_db()
        logs = conn.execute(
            "SELECT verifiedBy, actor FROM verification_log WHERE animalId = ?", (aid,)
        ).fetchall()
        conn.close()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["verifiedBy"], "Dr Rao")
        self.assertEqual(logs[0]["actor"], "vet-token")

        status, updated = self.client.request(
            "PUT", f"/api/animals/{aid}", {"name": "VerifyMe II", "tag": "VER-001"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(updated["verified"], "Editing a record must not clear verification")
        self.client.request("DELETE", f"/api/animals/{aid}")

    def test_15_no_wildcard_cors_headers(self):
        self.client.request("GET", "/api/health")
        self.assertNotIn("Access-Control-Allow-Origin", self.client.headers_out)

    def test_16_real_http_export_auth(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.CattleRequestHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/api/export", timeout=5)
            self.assertEqual(cm.exception.code, 401)
            cm.exception.close()

            request = urllib.request.Request(base + "/api/export", headers={"X-Admin-Token": TEST_TOKEN})
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertNotIn("Access-Control-Allow-Origin", response.headers)
                body = json.load(response)
                self.assertIn("animals", body)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_17_demo_data_is_opt_in(self):
        original_path = server.DB_PATH
        original_seed = os.environ.get("SEED_DEMO_DATA")
        fresh_dir = tempfile.mkdtemp(prefix="cattlecare_empty_")
        try:
            server.DB_PATH = os.path.join(fresh_dir, "empty.db")
            os.environ["SEED_DEMO_DATA"] = "0"
            server.init_db()
            conn = server.get_db()
            count = conn.execute("SELECT COUNT(*) FROM animals").fetchone()[0]
            conn.close()
            self.assertEqual(count, 0)
        finally:
            server.DB_PATH = original_path
            if original_seed is None:
                os.environ.pop("SEED_DEMO_DATA", None)
            else:
                os.environ["SEED_DEMO_DATA"] = original_seed
            shutil.rmtree(fresh_dir, ignore_errors=True)

    def test_18_admin_token_cannot_verify(self):
        status, created = self.client.request(
            "POST", "/api/animals", {"name": "NoVet", "tag": "NOVET-001"}
        )
        self.assertEqual(status, 201)
        aid = created["id"]
        status, body = self.client.request(
            "POST", f"/api/animals/{aid}/verify", {"verifiedBy": "Farmer"},
            headers={"X-Vet-Token": TEST_TOKEN}  # admin token reused as vet token -> reject
        )
        self.assertEqual(status, 401)
        self.assertIn("X-Vet-Token", body["error"])
        self.client.request("DELETE", f"/api/animals/{aid}")

    def test_19_client_backup_sends_admin_token(self):
        src_path = os.path.join(server.STATIC_DIR, "script.js")
        with open(src_path, "r", encoding="utf-8") as handle:
            src = handle.read()
        self.assertIn('headers: { "X-Admin-Token": getAdminToken() }', src)
        self.assertNotIn('fetch(`${API_BASE}/api/export`)', src)

    def test_20_pagination_and_summary(self):
        status, first = self.client.request("GET", "/api/animals?summary=true&limit=1&offset=0")
        self.assertEqual(status, 200)
        self.assertEqual(len(first), 1)
        self.assertNotIn("photo", first[0])

        status, second = self.client.request("GET", "/api/animals?summary=true&limit=1&offset=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0]["id"], second[0]["id"])

    def test_21_failed_tokens_are_throttled_then_cleared(self):
        for _ in range(server.MAX_FAILED_ATTEMPTS):
            status, _ = self.client.request(
                "GET", "/api/export", headers={"X-Admin-Token": "wrong-token"}
            )
            self.assertEqual(status, 401)
        status, body = self.client.request(
            "GET", "/api/export", headers={"X-Admin-Token": "wrong-token"}
        )
        self.assertEqual(status, 429)
        self.assertIn("Too many failed attempts", body["error"])

        status, _ = self.client.request("GET", "/api/export")
        self.assertEqual(status, 200)
        status, _ = self.client.request(
            "GET", "/api/export", headers={"X-Admin-Token": "wrong-token"}
        )
        self.assertEqual(status, 401)  # success reset the counter

    def test_22_stats_use_aggregates_and_invalidate_on_write(self):
        status, before = self.client.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        status, created = self.client.request(
            "POST", "/api/animals", {"name": "StatsProbe", "tag": "STATS-1"}
        )
        self.assertEqual(status, 201)
        status, after = self.client.request("GET", "/api/stats")
        self.assertEqual(after["totalCattle"], before["totalCattle"] + 1)
        self.client.request("DELETE", f"/api/animals/{created['id']}")

    def test_23_delete_removes_uploaded_photo_file(self):
        photo = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        status, created = self.client.request(
            "POST", "/api/animals", {"name": "PhotoProbe", "tag": "PHOTO-1", "photo": photo}
        )
        self.assertEqual(status, 201)
        path = os.path.join(server.UPLOAD_DIR, os.path.basename(created["photo"]))
        self.assertTrue(os.path.isfile(path))
        status, _ = self.client.request("DELETE", f"/api/animals/{created['id']}")
        self.assertEqual(status, 200)
        self.assertFalse(os.path.exists(path))

    def test_24_list_exposes_total_count_and_security_header(self):
        status, body = self.client.request("GET", "/api/animals?summary=true&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 1)
        self.assertGreaterEqual(int(self.client.headers_out["X-Total-Count"]), 3)
        self.assertEqual(self.client.headers_out["X-Content-Type-Options"], "nosniff")

    def test_25_due_filter_is_server_side(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        status, created = self.client.request(
            "POST", "/api/animals",
            {"name": "DueProbe", "tag": "DUE-1", "nextVaccineDate": yesterday}
        )
        self.assertEqual(status, 201)
        status, rows = self.client.request("GET", "/api/animals?health=due")
        self.assertEqual(status, 200)
        self.assertTrue(any(r["id"] == created["id"] for r in rows))
        self.client.request("DELETE", f"/api/animals/{created['id']}")

    def test_26_environment_overrides_are_applied_on_import(self):
        env = os.environ.copy()
        env.update({
            "BIND_HOST": "0.0.0.0",
            "CATTLECARE_DB": os.path.join(self.test_dir, "env", "env.db"),
            "CATTLECARE_UPLOADS": os.path.join(self.test_dir, "env", "photos"),
        })
        probe = (
            "import json, server; print(json.dumps({"
            "'host': server.BIND_HOST, 'db': server.DB_PATH, 'uploads': server.UPLOAD_DIR}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True
        )
        settings = json.loads(result.stdout.strip())
        self.assertEqual(settings["host"], "0.0.0.0")
        self.assertEqual(settings["db"], env["CATTLECARE_DB"])
        self.assertEqual(settings["uploads"], env["CATTLECARE_UPLOADS"])

    def test_27_init_db_creates_missing_parent_directory(self):
        original_path = server.DB_PATH
        nested_dir = os.path.join(self.test_dir, "nested", "deeper")
        try:
            server.DB_PATH = os.path.join(nested_dir, "fresh.db")
            server.init_db()
            self.assertTrue(os.path.isdir(nested_dir))
            self.assertTrue(os.path.isfile(server.DB_PATH))
        finally:
            server.DB_PATH = original_path

    def test_28_search_matches_clinical_notes(self):
        status, created = self.client.request(
            "POST", "/api/animals",
            {"name": "NoteProbe", "tag": "NOTE-1", "notes": "unique-note-probe"}
        )
        self.assertEqual(status, 201)
        status, rows = self.client.request("GET", "/api/animals?search=unique-note-probe")
        self.assertEqual(status, 200)
        self.assertTrue(any(r["id"] == created["id"] for r in rows))
        self.client.request("DELETE", f"/api/animals/{created['id']}")

    def test_29_schema_migration_adds_timeline_and_audit_tables(self):
        conn = server.get_db()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        conn.close()
        self.assertGreaterEqual(version, 2)
        self.assertIn("care_events", tables)
        self.assertIn("audit_log", tables)

    def test_30_care_event_validation_and_auth(self):
        status, created = self.client.request(
            "POST", "/api/animals", {"name": "EventProbe", "tag": "EVENT-1"}
        )
        self.assertEqual(status, 201)
        base = f"/api/animals/{created['id']}/events"

        status, _ = self.client.request(
            "POST", base, {"type": "vaccination", "title": "Bad token", "occurredAt": "2026-01-01"},
            headers={"X-Admin-Token": "wrong-token"}
        )
        self.assertEqual(status, 401)

        status, body = self.client.request(
            "POST", base, {"type": "not-a-type", "title": "Bad type", "occurredAt": "2026-01-01"}
        )
        self.assertEqual(status, 400)
        self.assertIn("Invalid event type", body["error"])

        status, event = self.client.request(
            "POST", base, {
                "type": "vaccination", "title": "Annual booster",
                "occurredAt": "2026-01-01", "details": "No reaction"
            }
        )
        self.assertEqual(status, 201)
        self.assertEqual(event["title"], "Annual booster")
        self.client.request("DELETE", f"/api/animals/{created['id']}")

    def test_31_care_timeline_lists_events_newest_first(self):
        status, created = self.client.request(
            "POST", "/api/animals", {"name": "TimelineProbe", "tag": "TIME-1"}
        )
        self.assertEqual(status, 201)
        base = f"/api/animals/{created['tag']}/events"
        self.client.request("POST", base, {
            "type": "observation", "title": "Older note", "occurredAt": "2025-01-01"
        })
        self.client.request("POST", base, {
            "type": "treatment", "title": "Newer treatment", "occurredAt": "2026-01-01"
        })
        status, events = self.client.request("GET", base)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[0]["title"], "Newer treatment")
        self.client.request("DELETE", f"/api/animals/{created['id']}")

    def test_32_audit_log_records_create_and_delete(self):
        status, before = self.client.request("GET", "/api/audit")
        self.assertEqual(status, 200)
        status, created = self.client.request(
            "POST", "/api/animals", {"name": "AuditProbe", "tag": "AUDIT-1"}
        )
        self.assertEqual(status, 201)
        self.client.request("DELETE", f"/api/animals/{created['id']}")
        status, after = self.client.request("GET", "/api/audit")
        self.assertEqual(status, 200)
        self.assertEqual(len(after), len(before) + 2)
        actions = [entry["action"] for entry in after[:2]]
        self.assertIn("create", actions)
        self.assertIn("delete", actions)

    def test_33_public_passport_omits_notes_and_photo(self):
        status, created = self.client.request(
            "POST", "/api/animals", {"name": "PublicProbe", "tag": "PUBLIC-1", "notes": "secret-note"}
        )
        self.assertEqual(status, 201)
        status, passport = self.client.request("GET", "/api/passport/PUBLIC-1")
        self.assertEqual(status, 200)
        self.assertNotIn("notes", passport)
        self.assertNotIn("photo", passport)
        self.assertEqual(passport["tag"], "PUBLIC-1")
        self.client.request("DELETE", f"/api/animals/{created['id']}")

    def test_34_analytics_returns_only_aggregates(self):
        status, payload = self.client.request("GET", "/api/analytics")
        self.assertEqual(status, 200)
        for key in ("health", "vaccines", "topBreeds", "verifiedCount", "eventTypes"):
            self.assertIn(key, payload)
        self.assertNotIn("animals", payload)
        self.assertNotIn("notes", payload)

    def test_35_public_list_hides_notes_and_does_not_search_them(self):
        status, created = self.client.request(
            "POST", "/api/animals", {
                "name": "PublicListProbe", "tag": "PUBLICLIST-1",
                "notes": "secret-note-for-public-test"
            }
        )
        self.assertEqual(status, 201)

        status, leaks = self.client.request(
            "GET", "/api/animals?public=true&search=secret-note-for-public-test"
        )
        self.assertEqual(status, 200)
        self.assertEqual(leaks, [])

        status, visible = self.client.request(
            "GET", "/api/animals?public=true&search=PublicListProbe"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(visible), 1)
        self.assertNotIn("notes", visible[0])
        self.assertNotIn("photo", visible[0])
        self.assertFalse(visible[0]["hasPhoto"])

        self.client.request("DELETE", f"/api/animals/{created['id']}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
