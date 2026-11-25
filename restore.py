"""Restore a complete CattleCare JSON export into a fresh database.

Usage:
    python restore.py backups/cattlecare_backup_20261124_020000.json

Use --db and --uploads to override CATTLECARE_DB / CATTLECARE_UPLOADS.
--force deletes existing application rows first; take a separate file backup
before using it. The audit_log triggers are temporarily removed for that reset
and recreated immediately afterwards.
"""

import argparse
import base64
import json
import os
import re
import sqlite3

import server

PHOTO_PATH_RE = re.compile(r"^/uploads/[0-9a-f-]+\.(png|jpg|jpeg|gif|webp)$")
TABLE_COLUMNS = {
    "animals": (
        "id", "name", "tag", "breed", "dob", "health", "lastVaccineDate",
        "vaccineName", "nextVaccineDate", "notes", "photo", "createdAt",
        "verified", "verifiedBy", "verifiedAt",
    ),
    "care_events": (
        "id", "animalId", "type", "title", "details", "occurredAt", "createdAt", "actor",
        "vaccineName",
    ),
    "verification_log": ("id", "animalId", "verifiedBy", "verifiedAt", "actor"),
    "audit_log": ("id", "action", "entityId", "summary", "actor", "createdAt"),
    "app_sequences": ("name", "nextValue"),
}
EXPORT_KEYS = {
    "animals": "animals",
    "careEvents": "care_events",
    "verificationLog": "verification_log",
    "auditLog": "audit_log",
    "appSequences": "app_sequences",
}


def _rows(payload, export_key):
    rows = payload.get(export_key, [])
    if not isinstance(rows, list):
        raise ValueError(f"'{export_key}' must be a list.")
    return rows


def _create_audit_triggers(conn):
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS audit_log_no_update
        BEFORE UPDATE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
        BEFORE DELETE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
    """)


def restore_backup(payload, db_path, upload_dir, force=False):
    """Restore one export. Returns a dict of inserted row counts."""
    if not isinstance(payload, dict):
        raise ValueError("Backup must be a JSON object.")
    version = payload.get("schemaVersion", 1)
    if not isinstance(version, int) or version < 2:
        raise ValueError("Backup schemaVersion 2 or newer is required.")
    if not _rows(payload, "animals"):
        raise ValueError("Backup contains no animals; refusing to wipe a database for nothing.")

    db_path = os.path.abspath(db_path)
    upload_dir = os.path.abspath(upload_dir)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    os.makedirs(upload_dir, exist_ok=True)

    # init_db() creates the schema and triggers without seeding demo data.
    previous_seed = os.environ.get("SEED_DEMO_DATA")
    os.environ["SEED_DEMO_DATA"] = "0"
    previous_db, previous_uploads = server.DB_PATH, server.UPLOAD_DIR
    server.DB_PATH, server.UPLOAD_DIR = db_path, upload_dir
    try:
        server.init_db()
    finally:
        server.DB_PATH, server.UPLOAD_DIR = previous_db, previous_uploads
        if previous_seed is None:
            os.environ.pop("SEED_DEMO_DATA", None)
        else:
            os.environ["SEED_DEMO_DATA"] = previous_seed

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute("SELECT COUNT(*) FROM animals").fetchone()[0]
        if existing and not force:
            raise ValueError(
                f"Target database already has {existing} animals. Use --force only after "
                "backing up the current database."
            )
        if existing:
            for trigger in ("audit_log_no_update", "audit_log_no_delete"):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for table in ("care_events", "verification_log", "animals", "audit_log", "app_sequences"):
                conn.execute(f"DELETE FROM {table}")
            _create_audit_triggers(conn)
            conn.commit()

        counts = {}
        for export_key, table in EXPORT_KEYS.items():
            columns = TABLE_COLUMNS[table]
            records = _rows(payload, export_key)
            placeholders = ", ".join("?" for _ in columns)
            column_sql = ", ".join(columns)
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError(f"Rows in '{export_key}' must be objects.")
                values = [record.get(column) for column in columns]
                if table == "app_sequences":
                    conn.execute("""
                        INSERT INTO app_sequences (name, nextValue)
                        VALUES (?, ?)
                        ON CONFLICT(name) DO UPDATE SET nextValue = excluded.nextValue
                    """, values)
                elif table == "animals":
                    defaults = {
                        "breed": "",
                        "dob": "",
                        "health": "Healthy",
                        "lastVaccineDate": "",
                        "vaccineName": "",
                        "nextVaccineDate": "",
                        "notes": "",
                        "photo": "",
                        "createdAt": "",
                        "verified": 0,
                        "verifiedBy": "",
                        "verifiedAt": "",
                    }
                    values = [
                        defaults.get(column, "") if record.get(column) is None else record.get(column)
                        for column in columns
                    ]
                    conn.execute(
                        f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})", values
                    )
                else:
                    conn.execute(
                        f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})", values
                    )
            counts[table] = len(records)

        max_cow_number = 0
        for record in conn.execute("SELECT tag FROM animals").fetchall():
            match = re.fullmatch(r"COW-(\d+)", str(record["tag"] or ""), re.IGNORECASE)
            if match:
                max_cow_number = max(max_cow_number, int(match.group(1)))
        sequence_records = _rows(payload, "appSequences")
        animal_sequence = max_cow_number + 1
        for sequence in sequence_records:
            if sequence.get("name") != "animal_tag":
                continue
            try:
                animal_sequence = max(animal_sequence, int(sequence.get("nextValue") or 1))
            except (TypeError, ValueError):
                pass
        conn.execute("""
            INSERT INTO app_sequences (name, nextValue)
            VALUES ('animal_tag', ?)
            ON CONFLICT(name) DO UPDATE SET nextValue = excluded.nextValue
        """, (animal_sequence,))

        photos = payload.get("photos", [])
        if not isinstance(photos, list):
            raise ValueError("'photos' must be a list.")
        for item in photos:
            if not isinstance(item, dict):
                raise ValueError("Photo entries must be objects.")
            relative = item.get("path", "")
            if not PHOTO_PATH_RE.fullmatch(relative):
                raise ValueError(f"Unsafe photo path in backup: {relative!r}")
            encoded = str(item.get("data") or "")
            encoded += "=" * (-len(encoded) % 4)
            data = base64.b64decode(encoded, validate=True)
            if len(data) > server.MAX_BODY_SIZE:
                raise ValueError(f"Photo exceeds size limit: {relative}")
            with open(os.path.join(upload_dir, os.path.basename(relative)), "wb") as handle:
                handle.write(data)
        counts["photos"] = len(photos)

        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Restore a complete CattleCare JSON backup.")
    parser.add_argument("backup", help="Path to the JSON file written by backup.py")
    parser.add_argument("--db", default=os.environ.get("CATTLECARE_DB", "./cattlecare.db"))
    parser.add_argument("--uploads", default=os.environ.get("CATTLECARE_UPLOADS", "./uploads"))
    parser.add_argument("--force", action="store_true", help="Replace existing application rows")
    args = parser.parse_args()

    with open(args.backup, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    counts = restore_backup(payload, args.db, args.uploads, force=args.force)
    summary = ", ".join(f"{key}={value}" for key, value in counts.items())
    print(f"Restored into {args.db}: {summary}")


if __name__ == "__main__":
    main()
