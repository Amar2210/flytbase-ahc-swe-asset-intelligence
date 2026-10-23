"""Scheduler retirement uses isolated fixtures, never the application's database."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from copilot import db
from copilot.agent import Copilot
from copilot.config import Settings


OBSOLETE = {"model_token_reservations", "model_rate_limit_cooldowns"}


class SchedulerRetirementTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / "data" / ("migration-test-" + uuid4().hex)
        self.root.mkdir(parents=True)
        self.database = self.root / "isolated.sqlite3"
        db.initialize(self.database)
        with closing(db.connect(self.database)) as conn, conn:
            conn.execute("INSERT INTO datasets VALUES ('D','Public fixture','fixture','2026-10-22','2026-10-16','{}')")
            conn.execute("INSERT INTO users(dataset_id,user_id,name,raw_json,source_path) VALUES ('D','U','Fixture user','{}','fleet/users.json')")
            conn.execute("INSERT INTO wells(dataset_id,asset_id,name,raw_json,source_path) VALUES ('D','WELL-0001','Fixture well','{}','assets/WELL-0001/metadata.json')")
        Copilot(self.database, "D", "U", Settings(db_path=str(self.database)))
        payload = json.dumps({"answer": "Original private answer", "token_usage": [{"provider": "google", "rate_limit_wait_seconds": 12}]})
        with closing(db.connect(self.database)) as conn, conn:
            conn.execute("INSERT INTO chat_messages(dataset_id,user_id,session_id,role,content,payload_json,created_at) VALUES ('D','U','S','user','Original private request',?,'2026-10-22T00:00:00Z')", (payload,))
            conn.execute("INSERT INTO user_preferences VALUES ('D','U',?, '2026-10-22T00:00:00Z',1)", (json.dumps({"cost_display": "thousands_usd"}),))
            conn.execute("INSERT INTO equipment_replacement_reports VALUES ('R','D','WELL-0001','U',NULL,'direct_report','reported_replaced','2026-10-15','Original replacement statement','2026-10-22T00:00:00Z',NULL)")
            conn.execute("INSERT INTO corrections VALUES ('C','D','INS-1','wellhead_temp_c','41.0','U','Original reason','2026-10-22T00:00:00Z','accepted',NULL,'sensor_reading_correction')")
            conn.execute("INSERT INTO pending_updates VALUES ('P','D','U','S','add_shared_note',?,'2026-10-22T00:00:00Z','pending',NULL)", (json.dumps({"asset_id": "WELL-0001", "text": "Original pending argument"}),))
            conn.execute("CREATE TABLE model_audit (id TEXT PRIMARY KEY, content TEXT)")
            conn.execute("CREATE INDEX keep_model_audit ON model_audit(content)")
            conn.execute("INSERT INTO model_audit VALUES ('keep','Unrelated table must remain')")

    def tearDown(self):
        if self.root.name.startswith("migration-test-") and self.root.parent == Path(__file__).resolve().parent / "data":
            shutil.rmtree(self.root)

    def snapshot(self):
        with closing(db.connect(self.database)) as conn:
            names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'") if row[0] not in OBSOLETE]
            return {name: [tuple(row) for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid')] for name in names}

    def create_legacy(self, names):
        with closing(db.connect(self.database)) as conn, conn:
            if "model_token_reservations" in names:
                conn.execute("CREATE TABLE model_token_reservations (reservation_id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL, quota_group TEXT NOT NULL, reserved_at REAL NOT NULL, tokens INTEGER NOT NULL CHECK(tokens >= 0))")
                conn.execute("CREATE INDEX model_token_reservations_scope ON model_token_reservations(provider,model,quota_group,reserved_at)")
                conn.execute("INSERT INTO model_token_reservations VALUES ('old','google','old-model','old-group',1000,100)")
            if "model_rate_limit_cooldowns" in names:
                conn.execute("CREATE TABLE model_rate_limit_cooldowns (provider TEXT NOT NULL, model TEXT NOT NULL, quota_group TEXT NOT NULL, until_at REAL NOT NULL, PRIMARY KEY(provider,model,quota_group))")
                conn.execute("INSERT INTO model_rate_limit_cooldowns VALUES ('google','old-model','old-group',2000)")

    def test_fresh_database_has_no_scheduler_tables_or_indexes(self):
        with closing(db.connect(self.database)) as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        self.assertTrue(OBSOLETE.isdisjoint(names))
        self.assertNotIn("model_token_reservations_scope", names)

    def test_both_partial_and_repeated_upgrades_preserve_every_other_row(self):
        before = self.snapshot()
        for names in (OBSOLETE, {"model_token_reservations"}, {"model_rate_limit_cooldowns"}, set()):
            with self.subTest(names=names):
                self.create_legacy(names)
                db.initialize(self.database)
                db.initialize(self.database)
                self.assertEqual(self.snapshot(), before)
                with closing(db.connect(self.database)) as conn:
                    objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
                    self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertTrue(OBSOLETE.isdisjoint(objects))
                self.assertNotIn("model_token_reservations_scope", objects)
                self.assertIn("keep_model_audit", objects)

    def test_current_application_schema_retains_seventeen_tables_and_constraints(self):
        with closing(db.connect(self.database)) as conn:
            names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'model_audit'")]
            columns = sum(len(conn.execute(f'PRAGMA table_info("{name}")').fetchall()) for name in names)
            foreign_keys = sum(len(conn.execute(f'PRAGMA foreign_key_list("{name}")').fetchall()) for name in names)
        self.assertEqual((len(names), columns, foreign_keys), (17, 176, 15))


if __name__ == "__main__":
    unittest.main()
