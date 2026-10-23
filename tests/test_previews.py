"""Real database visual data, with the model boundary forbidden."""

from contextlib import closing
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from copilot.db import connect
from copilot.ingest import import_dataset
from copilot.previews import PREVIEW_LABELS, build_preview
from copilot.tools import ToolService


ROOT = Path(__file__).resolve().parents[1]


class PreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = ROOT / "tests" / "data" / ("preview-test-" + uuid4().hex + ".sqlite3")
        import_dataset(ROOT / "challenge-dataset/sample-data", cls.database, "Preview test")
        with closing(connect(cls.database)) as conn, conn:
            cls.dataset_id, cls.user_id = conn.execute("SELECT dataset_id,user_id FROM users LIMIT 1").fetchone()
            conn.execute("DELETE FROM corrections")
            conn.execute("DELETE FROM context_notes WHERE origin='runtime'")
        cls.tools = ToolService(cls.database, cls.dataset_id, cls.user_id)

    @classmethod
    def tearDownClass(cls):
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(cls.database) + suffix).unlink(missing_ok=True)

    def test_all_previews_use_real_rows_without_model_or_chat_writes(self):
        with closing(connect(self.database)) as conn:
            before = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("chat_messages", "context_notes", "corrections")}
        with patch("copilot.model.urlopen", side_effect=AssertionError("No provider calls permitted")) as network, \
                patch("copilot.agent.OpenRouterClient", side_effect=AssertionError("No model clients permitted")) as client:
            for kind in PREVIEW_LABELS:
                with self.subTest(kind=kind):
                    result = build_preview(self.tools, kind, asset_id="WELL-0001")
                    self.assertTrue(result["visuals"][0]["data"])
                    self.assertTrue(result["citations"])
                    self.assertIn("no model API calls", result["tool_calls"][0]["mode"])
            network.assert_not_called()
            client.assert_not_called()
        with closing(connect(self.database)) as conn:
            after = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in before}
        self.assertEqual(before, after)

    def test_status_card_matches_imported_manual_and_inspection(self):
        row = build_preview(self.tools, "status", asset_id="well-1")["visuals"][0]["data"][0]
        self.assertEqual(row["asset_id"], "WELL-0001")
        self.assertEqual(row["inspector_name"], "Ryan Brooks")
        self.assertAlmostEqual(row["temperature_delta_c"], row["wellhead_temp_c"] - row["ambient_temp_c"])
        self.assertEqual(bool(row["is_producing"]), row["temperature_delta_c"] >= row["production_delta_c"])

    def test_downtime_matches_independently_counted_weekly_inspections(self):
        result = build_preview(self.tools, "downtime", start_date="2025-01-01", end_date="2025-12-31")
        with closing(connect(self.database)) as conn:
            readings = conn.execute("SELECT i.asset_id,i.wellhead_temp_c,i.ambient_temp_c,t.production_delta_c "
                                    "FROM inspections i JOIN wells w USING(dataset_id,asset_id) "
                                    "JOIN well_types t USING(dataset_id,well_type) "
                                    "WHERE i.dataset_id=? AND i.inspected_at LIKE '2025-%'", (self.dataset_id,))
            expected = {}
            for asset, wellhead, ambient, threshold in readings:
                if wellhead - ambient < threshold:
                    expected[asset] = expected.get(asset, 0) + 7
        expected = [{"asset_id": asset, "downtime_days": days} for asset, days in
                    sorted(expected.items(), key=lambda item: (-item[1], item[0]))[:10]]
        self.assertEqual(result["visuals"][0]["data"], expected)

    def test_spill_previews_exclude_documented_false_positive(self):
        table = build_preview(self.tools, "table", asset_id="WELL-0001", start_date="2020-01-01")
        corrected = next(row for row in table["visuals"][0]["data"] if row["inspection_id"] == "INS-0001-0070")
        self.assertEqual(corrected["effective_spill_detected"], 0)
        self.assertTrue(corrected["correction_sources"])
        heatmap = build_preview(self.tools, "spills", start_date="2020-01-01")
        effective_count = sum(row["spill_count"] for row in heatmap["visuals"][0]["data"])
        with closing(connect(self.database)) as conn:
            raw_count = conn.execute("SELECT count(*) FROM inspections WHERE dataset_id=? AND spill_detected=1 AND date(inspected_at)<=?",
                                     (self.dataset_id, self.tools.as_of_date)).fetchone()[0]
        self.assertLess(effective_count, raw_count)

    def test_trend_respects_inclusive_date_window(self):
        result = build_preview(self.tools, "trend", asset_id="WELL-0001", start_date="2026-10-01", end_date="2026-10-15")
        rows = result["visuals"][0]["data"]
        self.assertTrue(rows)
        self.assertTrue(all("2026-10-01" <= row["inspected_at"][:10] <= "2026-10-15" for row in rows))
        self.assertTrue(any(row["inspected_at"].startswith("2026-10-15") for row in rows))

    def test_invalid_scope_or_window_is_rejected(self):
        for arguments in ({"kind": "missing"}, {"kind": "status", "asset_id": "WELL-0001' OR 1=1"},
                          {"kind": "trend", "asset_id": "WELL-0001", "start_date": "2026-10-16", "end_date": "2026-10-15"},
                          {"kind": "spills", "end_date": "2026-10-17"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                build_preview(self.tools, **arguments)


if __name__ == "__main__":
    unittest.main()
