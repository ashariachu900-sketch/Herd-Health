"""
Automated Test Suite for CattleCare Backend
Uses Python's standard unittest library - no external packages required.
Executes in-process for 100% reliability across all CI/CD and sandbox environments.

Execute via:
    python -m unittest test_server.py -v
"""

import os
import io
import json
import shutil
import tempfile
import unittest
import datetime

# Import server module under test
import server

class InProcessTestClient:
    """Simulates HTTP requests into CattleRequestHandler without requiring OS TCP sockets."""
    def request(self, method, path, data=None, headers=None):
        req = server.CattleRequestHandler.__new__(server.CattleRequestHandler)
        body_bytes = json.dumps(data).encode("utf-8") if data is not None else b""
        req.rfile = io.BytesIO(body_bytes)
        req.wfile = io.BytesIO()
        req.path = path
        req.command = method
        req.headers = {"X-Admin-Token": "REDACTED-OLD-SECRET"}
        if headers:
            req.headers.update(headers)
        req.headers["Content-Length"] = str(len(body_bytes))

        status_code = [200]
        def mock_send_response(code, msg=None):
            status_code[0] = code

        req.send_response = mock_send_response
        req.send_header = lambda *a: None
        req.end_headers = lambda: None
        req.send_cors_headers = lambda: None

        if method == "GET":
            req.do_GET()
        elif method == "POST":
            req.do_POST()
        elif method == "PUT":
            req.do_PUT()
        elif method == "DELETE":
            req.do_DELETE()

        raw_output = req.wfile.getvalue().decode("utf-8")
        try:
            return status_code[0], json.loads(raw_output) if raw_output else {}
        except Exception:
            return status_code[0], {"raw": raw_output}

class CattleCareBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Create an isolated temporary directory for test database
        cls.test_dir = tempfile.mkdtemp(prefix="cattlecare_test_")
        cls.test_db = os.path.join(cls.test_dir, "test_cattlecare.db")

        # Point server to isolated test database
        cls.original_db_path = server.DB_PATH
        server.DB_PATH = cls.test_db

        # Initialize the test database
        server.init_db()
        cls.client = InProcessTestClient()

    @classmethod
    def tearDownClass(cls):
        # Clean up temporary test database and directory
        server.DB_PATH = cls.original_db_path
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    # --------------------------------------------------------------------------
    # 1. Logic & Calculation Tests
    # --------------------------------------------------------------------------

    def test_01_vaccine_status_calculations(self):
        """Test vaccine date difference and status categorization logic."""
        today = datetime.date.today()
        today_str = today.isoformat()
        yesterday_str = (today - datetime.timedelta(days=1)).isoformat()
        future_str = (today + datetime.timedelta(days=10)).isoformat()

        self.assertEqual(server.vaccine_status(today_str), "today")
        self.assertEqual(server.vaccine_status(yesterday_str), "overdue")
        self.assertEqual(server.vaccine_status(future_str), "upcoming")
        self.assertEqual(server.vaccine_status(""), "none")
        self.assertEqual(server.vaccine_status(None), "none")

    # --------------------------------------------------------------------------
    # 2. Database Schema & Indexing Tests
    # --------------------------------------------------------------------------

    def test_02_database_schema_and_indexes(self):
        """Verify database table creation and performance indexes."""
        conn = server.get_db()
        cursor = conn.cursor()

        # Check table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='animals';")
        self.assertIsNotNone(cursor.fetchone(), "Table 'animals' must exist.")

        # Check performance indexes exist
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index';")
        index_names = [row["name"] for row in cursor.fetchall()]
        self.assertIn("idx_animals_created", index_names, "Index 'idx_animals_created' must exist.")
        self.assertIn("idx_animals_health", index_names, "Index 'idx_animals_health' must exist.")

        # Check initial seed records
        cursor.execute("SELECT COUNT(*) as cnt FROM animals;")
        self.assertGreaterEqual(cursor.fetchone()["cnt"], 3, "Database should have initial seed records.")
        conn.close()

    # --------------------------------------------------------------------------
    # 3. Health & Stats Endpoints
    # --------------------------------------------------------------------------

    def test_03_api_health_endpoint(self):
        """Verify GET /api/health returns 200 OK with status and service info."""
        status, data = self.client.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("status"), "ok")
        self.assertIn("service", data)

    def test_04_api_stats_endpoint(self):
        """Verify GET /api/stats returns aggregated metrics."""
        status, data = self.client.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertIn("totalCattle", data)
        self.assertIn("healthyCattle", data)
        self.assertIn("healthPercent", data)
        self.assertIn("upcoming", data)
        self.assertIsInstance(data["upcoming"], list)

    def test_05_api_animals_list_and_filters(self):
        """Verify GET /api/animals supports search and health filters."""
        # Unfiltered list
        status, animals = self.client.request("GET", "/api/animals")
        self.assertEqual(status, 200)
        self.assertIsInstance(animals, list)
        self.assertGreaterEqual(len(animals), 3)

        # Health filter: Healthy
        status, healthy_animals = self.client.request("GET", "/api/animals?health=Healthy")
        self.assertEqual(status, 200)
        for a in healthy_animals:
            self.assertEqual(a["health"], "Healthy")

        # Search filter
        status, searched = self.client.request("GET", "/api/animals?search=Bella")
        self.assertEqual(status, 200)
        self.assertTrue(any(a["name"] == "Bella" for a in searched))

    # --------------------------------------------------------------------------
    # 4. CRUD Lifecycle & Business Rule Tests
    # --------------------------------------------------------------------------

    def test_06_crud_lifecycle(self):
        """Verify full Create -> Read -> Update -> Delete lifecycle with duplicate protection."""
        new_animal = {
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

        # 1. Create animal (POST)
        status, created = self.client.request("POST", "/api/animals", data=new_animal)
        self.assertEqual(status, 201)
        created_id = created["id"]
        self.assertEqual(created["name"], "Moona")
        self.assertEqual(created["tag"], "TEST-COW-999")
        self.assertEqual(created["photo"], "data:image/jpeg;base64,samplephoto123")

        # 2. Reject duplicate tag ID (POST duplicate)
        dup_status, dup_err = self.client.request("POST", "/api/animals", data=new_animal)
        self.assertEqual(dup_status, 409, "Must reject duplicate animal tag ID.")

        # 3. Read single animal by ID (GET)
        status, fetched = self.client.request("GET", f"/api/animals/{created_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["name"], "Moona")

        # 4. Read single animal by Tag (GET)
        status, fetched_by_tag = self.client.request("GET", f"/api/animals/TEST-COW-999")
        self.assertEqual(status, 200)
        self.assertEqual(fetched_by_tag["id"], created_id)

        # 5. Update animal details while preserving photo (PUT)
        update_payload = {
            "name": "Moona Champion",
            "tag": "TEST-COW-999",
            "breed": "Simmental Purebred",
            "health": "Needs attention",
            "photo": ""  # Omitted/empty new photo: MUST preserve existing photo
        }
        status, updated = self.client.request("PUT", f"/api/animals/{created_id}", data=update_payload)
        self.assertEqual(status, 200)
        self.assertEqual(updated["name"], "Moona Champion")
        self.assertEqual(updated["health"], "Needs attention")
        self.assertEqual(updated["photo"], "data:image/jpeg;base64,samplephoto123", "Photo must NOT be erased on update.")

        # 6. Delete animal (DELETE)
        status, deleted = self.client.request("DELETE", f"/api/animals/{created_id}")
        self.assertEqual(status, 200)
        self.assertTrue(deleted.get("success"))

        # 7. Confirm deletion (GET -> 404)
        status, missing = self.client.request("GET", f"/api/animals/{created_id}")
        self.assertEqual(status, 404)

    # --------------------------------------------------------------------------
    # 5. Backup & Security Boundary Tests
    # --------------------------------------------------------------------------

    def test_07_api_export_endpoint(self):
        """Verify GET /api/export generates complete database backup."""
        status, backup = self.client.request("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertIn("exportedAt", backup)
        self.assertIn("totalRecords", backup)
        self.assertIn("animals", backup)
        self.assertIsInstance(backup["animals"], list)

    def test_08_payload_size_limit(self):
        """Verify server rejects oversized payloads > 5MB with 413 Payload Too Large."""
        oversized_data = {"junk": "x" * (6 * 1024 * 1024)}  # 6 MB payload
        status, resp = self.client.request("POST", "/api/animals", data=oversized_data)
        self.assertEqual(status, 413, "Must return HTTP 413 for payloads > 5MB.")

    def test_09_blocked_sensitive_files(self):
        """Verify server blocks direct downloads of database, source code, and configs (HTTP 403)."""
        blocked_paths = ["/cattlecare.db", "/server.py", "/test_server.py", "/run_server.bat", "/.gitignore"]
        for p in blocked_paths:
            status, resp = self.client.request("GET", p)
            self.assertEqual(status, 403, f"Path '{p}' must be blocked with HTTP 403 Forbidden.")

    def test_10_unauthorized_mutation(self):
        """Verify server rejects mutations with invalid auth token with HTTP 401 Unauthorized."""
        bad_headers = {"X-Admin-Token": "invalid-hacker-token"}
        dummy_data = {"name": "Hacked", "tag": "HACK-01"}
        status, resp = self.client.request("POST", "/api/animals", data=dummy_data, headers=bad_headers)
        self.assertEqual(status, 401, "Invalid token must return HTTP 401 Unauthorized.")

if __name__ == "__main__":
    unittest.main(verbosity=2)
