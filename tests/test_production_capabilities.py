"""An unsupported volume question must not become a temperature ranking."""
from contextlib import closing
import json
from pathlib import Path
import unittest
from uuid import uuid4

from copilot.db import connect, initialize
from copilot.production_capabilities import unsupported_production_volume


class ProductionCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).resolve().parents[1] / "data" / ("capability-test-" + uuid4().hex + ".sqlite3")
        initialize(self.path)
        with closing(connect(self.path)) as conn, conn:
            for dataset in ("selected", "other"):
                conn.execute("INSERT INTO datasets(dataset_id,label,imported_at,counts_json) VALUES (?,?,?,?)", (dataset, dataset, "2026-10-17", "{}"))
                conn.execute("INSERT INTO inspections(dataset_id,inspection_id,asset_id,raw_json,source_path) VALUES (?,?,?,?,?)",
                             (dataset, "I1", "WELL-1", json.dumps({"wellhead_temp_c": 40, "spill_est_volume_bbl": 2}), "inspections.jsonl"))

    def tearDown(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)

    def check(self, prompt="Which well produced the most oil in 2026?"):
        return unsupported_production_volume(self.path, "selected", prompt)

    def test_missing_volume_explained_without_returning_a_rank(self):
        result = self.check()
        self.assertIn("no measured oil-production volume", result["answer"])
        self.assertEqual(result["visuals"], [])
        self.assertEqual(result["citations"][0]["dataset_id"], "selected")

    def test_explicit_proxies_and_spill_metrics_are_not_intercepted(self):
        for prompt in ("Which well produced oil on the most inspection days?", "Which oil spill had the largest volume?", "Is WELL-1 producing oil?", "Rank oil wells by downtime"):
            with self.subTest(prompt=prompt):
                self.assertIsNone(self.check(prompt))

    def test_optional_nested_meter_measurements_fall_through(self):
        with closing(connect(self.path)) as conn, conn:
            conn.execute("UPDATE inspections SET raw_json=? WHERE dataset_id='selected'", (json.dumps({"production": {"volume_bbl": 50}}),))
        self.assertIsNone(self.check())

    def test_other_dataset_measurements_do_not_change_selected_capability(self):
        with closing(connect(self.path)) as conn, conn:
            conn.execute("UPDATE inspections SET raw_json=? WHERE dataset_id='other'", (json.dumps({"oil_rate_bpd": 100}),))
        self.assertIsNotNone(self.check())

    def test_relational_meter_field_falls_through(self):
        with closing(connect(self.path)) as conn, conn:
            conn.execute("ALTER TABLE inspections ADD COLUMN oil_volume_bbl REAL")
        self.assertIsNone(self.check())

    def test_metered_production_report_falls_through(self):
        with closing(connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO documents(dataset_id,document_id,path,kind,body) VALUES ('selected','meter','meter.md','report','Metered oil production was 500 bbl.')")
        self.assertIsNone(self.check())


if __name__ == "__main__":
    unittest.main()
