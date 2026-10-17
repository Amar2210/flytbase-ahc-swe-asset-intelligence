"""Real benchmark data validates business rules, isolation and durable updates."""
from contextlib import closing
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from copilot.db import connect
from copilot.ingest import import_dataset
from copilot.tools import ToolService, normalize_asset_id, _false_spill, _containment_evidence


class ToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = Path(__file__).resolve().parent / ("tool-test-" + uuid4().hex)
        cls.temp.mkdir()
        cls.db = cls.temp / "test.sqlite3"
        sample = Path(__file__).resolve().parents[1] / "challenge-dataset/sample-data"
        cls.dataset_id = import_dataset(sample, cls.db, "Sample")["dataset_id"]

    @classmethod
    def tearDownClass(cls):
        # Verify the absolute target before recursive cleanup on Windows.
        if cls.temp.resolve().parent == Path(__file__).resolve().parent and cls.temp.name.startswith("tool-test-"):
            shutil.rmtree(cls.temp)

    def setUp(self):
        self.service = ToolService(self.db, self.dataset_id, "U-201")
        with closing(connect(self.db)) as conn:
            conn.execute("DELETE FROM corrections")
            conn.execute("DELETE FROM context_notes WHERE origin='runtime'")
            conn.execute("DELETE FROM chat_messages")
            conn.commit()

    def query(self, sql):
        result = self.service.execute("query_records", {"sql": sql})
        self.assertNotIn("error", result, result)
        return result

    def test_reference_status_card_and_source_provenance(self):
        result = self.service.execute("get_well", {"asset_id": "well-001"})
        self.assertNotIn("error", result, result)
        latest = result["latest_inspection"]
        self.assertEqual(latest["inspection_id"], "INS-0001-0203")
        self.assertEqual(latest["is_producing"], 0)
        self.assertAlmostEqual(latest["temperature_delta_c"], 3.2)
        self.assertEqual(latest["inspector_name"], "Ryan Brooks")
        self.assertTrue(any(note["note_id"] == "CTX-0001-003" for note in result["shared_notes"]))
        self.assertTrue(any(c.get("source_line") for c in result["citations"]))

    def test_corrected_spill_statistics(self):
        result = self.query("SELECT count(*) spill_count,round(sum(effective_spill_est_volume_bbl),1) barrels FROM effective_inspections WHERE asset_id='WELL-0001' AND effective_spill_detected=1")
        self.assertEqual(result["rows"], [{"spill_count": 6, "barrels": 15.1}])
        false_positive = self.query("SELECT spill_detected,effective_spill_detected,correction_sources FROM effective_inspections WHERE inspection_id='INS-0001-0070'")["rows"][0]
        self.assertEqual(false_positive["spill_detected"], 1)
        self.assertEqual(false_positive["effective_spill_detected"], 0)
        self.assertIn("CTX-0001-001", false_positive["correction_sources"])
        cited = self.query("SELECT inspection_id,correction_sources FROM effective_inspections WHERE inspection_id='INS-0001-0070'")
        self.assertTrue(any(c.get("record_id") == "CTX-0001-001" for c in cited["citations"]))
        self.assertTrue(any(c.get("rule_quote") and c.get("line_start") for c in cited["citations"]))
        with closing(connect(self.db)) as conn:
            corrections = conn.execute("SELECT text FROM context_notes WHERE note_type='correction'").fetchall()
        self.assertEqual(len(corrections), 56)
        self.assertTrue(all(_false_spill(row["text"]) for row in corrections))
        self.assertFalse(_false_spill("Corrected estimate to 5.4 bbl of crude oil."))

    def test_downtime_uses_weekly_inspections(self):
        result = self.query("SELECT 7*count(*) downtime_days FROM effective_inspections WHERE asset_id='WELL-0001' AND is_producing=0 AND strftime('%Y',inspected_at)='2025'")
        self.assertEqual(result["rows"], [{"downtime_days": 21}])
        rankings = self.query("SELECT i.asset_id,7*count(*) downtime_days FROM effective_inspections i JOIN wells w USING(asset_id) JOIN areas a USING(area_id) WHERE a.name='Northeast Block 7' AND date(i.inspected_at) BETWEEN '2026-06-16' AND '2026-10-16' AND i.is_producing=0 GROUP BY i.asset_id ORDER BY downtime_days DESC,i.asset_id LIMIT 3")["rows"]
        self.assertEqual({r["asset_id"]: r["downtime_days"] for r in rankings}, {"WELL-0121": 49, "WELL-0138": 42, "WELL-0107": 42})

    def test_runtime_correction_propagates_without_mutating_raw(self):
        correction = self.service.execute("correct_inspection", {"inspection_id": "INS-0164-0205", "field": "wellhead_temp_c", "value": 41.0, "reason": "Faulty thermal gun; field crew remeasured."})
        self.assertNotIn("error", correction, correction)
        self.assertEqual(correction["original_value"], 24.0)
        self.assertEqual(correction["correction"]["feedback_kind"], "sensor_reading_correction")
        other_user = ToolService(self.db, self.dataset_id, "U-203")
        result = other_user.execute("get_well", {"asset_id": "WELL-0164"})
        self.assertNotIn("error", result, result)
        self.assertEqual(result["latest_inspection"]["effective_wellhead_temp_c"], 41)
        self.assertEqual(result["latest_inspection"]["wellhead_temp_c"], 24)
        self.assertEqual(result["latest_inspection"]["is_producing"], 1)
        self.assertIn("Pump jack not moving", result["latest_inspection"]["notes"])
        self.assertTrue(result["corrections"])
        backlink = correction["correction"]["source_note_id"]
        note = next(n for n in result["shared_notes"] if n["note_id"] == backlink)
        self.assertEqual(note["note_type"], "correction")
        self.assertEqual(note["correction_status"], "accepted")

    def test_competing_corrections_are_disputed(self):
        args = {"inspection_id": "INS-0164-0205", "field": "wellhead_temp_c", "value": 41, "reason": "Confirmed by remeasurement"}
        self.assertTrue(self.service.execute("correct_inspection", args)["applied"])
        args["value"] = 25
        disputed = self.service.execute("correct_inspection", args)
        self.assertFalse(disputed["applied"])
        result = self.query("SELECT effective_wellhead_temp_c,has_disputed_corrections FROM effective_inspections WHERE inspection_id='INS-0164-0205'")["rows"][0]
        self.assertEqual(result["effective_wellhead_temp_c"], 24)
        self.assertEqual(result["has_disputed_corrections"], 1)
        well = self.service.execute("get_well", {"asset_id": "WELL-0164"})
        correction_notes = [n for n in well["shared_notes"] if n["origin"] == "runtime" and n["note_type"] == "correction"]
        self.assertEqual(len(correction_notes), 2)
        self.assertTrue(all(n["correction_status"] == "disputed" for n in correction_notes))

    def test_runtime_conflict_preserves_imported_false_positive_correction(self):
        disputed = self.service.execute("correct_inspection", {"inspection_id": "INS-0001-0070", "field": "spill_detected", "value": True, "reason": "A new operator claims it was oil"})
        self.assertNotIn("error", disputed, disputed)
        self.assertFalse(disputed["applied"])
        self.assertEqual(disputed["conflicting_imported_note_ids"], ["CTX-0001-001"])
        row = self.query("SELECT effective_spill_detected,has_disputed_corrections FROM effective_inspections WHERE inspection_id='INS-0001-0070'")["rows"][0]
        self.assertEqual(row["effective_spill_detected"], 0)
        self.assertEqual(row["has_disputed_corrections"], 1)
        self.assertTrue(any(c.get("record_id") == "CTX-0001-001" for c in disputed["citations"]))

    def test_notes_do_not_override_detection_and_expiry_does_not_prove_restart(self):
        claims = self.query("SELECT effective_spill_detected,correction_sources FROM effective_inspections WHERE inspection_id='INS-0101-0197'")["rows"][0]
        self.assertEqual(claims["effective_spill_detected"], 1)
        self.assertFalse(claims["correction_sources"])
        saved = self.service.execute("add_shared_note", {"asset_id": "WELL-0007", "text": "Planned workover", "valid_from": "2026-10-17", "valid_to": "2026-10-26"})
        self.assertTrue(saved["saved"])
        future_service = ToolService(self.db, self.dataset_id, "U-203", "2026-10-27")
        result = future_service.execute("get_well", {"asset_id": "WELL-0007"})
        runtime = [n for n in result["shared_notes"] if n["origin"] == "runtime"]
        self.assertEqual(runtime[0]["applicability"], "expired")
        self.assertTrue(any("does not confirm" in warning for warning in result["warnings"]))

    def test_private_history_is_user_scoped_and_sql_inaccessible(self):
        with closing(connect(self.db)) as conn:
            for uid, content in [("U-201", "We discussed WELL-0121"), ("U-203", "Private second user")]:
                conn.execute("INSERT INTO chat_messages(dataset_id,user_id,session_id,role,content,created_at) VALUES (?,?,?,'user',?,'2026-10-16T12:00:00Z')", (self.dataset_id, uid, uid, content))
            conn.commit()
        memory = self.service.execute("recent_my_conversations", {})
        self.assertEqual([m["content"] for m in memory["messages"]], ["We discussed WELL-0121"])
        for sql in ("SELECT * FROM chat_messages", "SELECT * FROM main.chat_messages", "WITH wells AS (SELECT * FROM chat_messages) SELECT * FROM wells"):
            self.assertIn("error", self.service.execute("query_records", {"sql": sql}))

    def test_ai_estimate_feedback_tag_is_durable_without_type_wide_learning(self):
        correction = self.service.execute("correct_inspection", {"inspection_id": "INS-0151-0201",
            "field": "spill_est_volume_bbl", "value": 5.4, "reason": "Verified field estimate replaces the AI image estimate."})
        self.assertTrue(correction["saved"], correction)
        self.assertEqual(correction["original_value"], 4.5)
        self.assertEqual(correction["correction"]["feedback_kind"], "ai_estimate_feedback")
        original = self.query("SELECT spill_est_volume_bbl,effective_spill_est_volume_bbl FROM effective_inspections WHERE inspection_id='INS-0151-0201'")["rows"][0]
        self.assertEqual(original, {"spill_est_volume_bbl": 4.5, "effective_spill_est_volume_bbl": 5.4})
        well = self.service.execute("get_well", {"asset_id": "WELL-0151"})
        runtime = next(row for row in well["shared_notes"] if row.get("correction_id") == correction["correction"]["correction_id"])
        self.assertEqual(runtime["feedback_kind"], "ai_estimate_feedback")
        self.assertEqual(runtime["correction_status"], "accepted")
        source = next(row for row in well["citations"] if row["table"] == "corrections")
        self.assertEqual(source["feedback_kind"], "ai_estimate_feedback")
        ordinary = self.service.execute("get_well", {"asset_id": "WELL-0101"})
        opinions = [row for row in ordinary["shared_notes"] if row["note_type"] == "operator_feedback"]
        self.assertTrue(opinions)
        self.assertTrue(all(row["feedback_kind"] is None and row["correction_status"] is None for row in opinions))
        disputed = self.service.execute("correct_inspection", {"inspection_id": "INS-0151-0201",
            "field": "spill_est_volume_bbl", "value": 5.6, "reason": "Conflicting estimate."})
        self.assertEqual(disputed["correction"]["feedback_kind"], "ai_estimate_feedback")
        self.assertEqual(disputed["correction"]["status"], "disputed")
        self.assertFalse(disputed["applied"])

    def test_readonly_and_dataset_isolation(self):
        with closing(connect(self.db)) as conn:
            conn.execute("INSERT OR IGNORE INTO datasets VALUES ('other','Other',NULL,'2026-10-16',NULL,'{}')")
            conn.execute("INSERT OR IGNORE INTO users(dataset_id,user_id,name,raw_json,source_path) VALUES ('other','SECRET','Other-only user','{}','secret.json')")
            conn.commit()
        self.assertEqual(self.query("SELECT * FROM users WHERE user_id='SECRET'")["rows"], [])
        for sql in ("SELECT * FROM datasets", "DELETE FROM inspections", "PRAGMA database_list", "SELECT load_extension('evil')", "SELECT * FROM wells; DELETE FROM wells", "WITH x AS (SELECT 1) DELETE FROM wells", "SELECT * FROM sqlite_master"):
            self.assertIn("error", self.service.execute("query_records", {"sql": sql}), sql)
        self.assertEqual(self.query("SELECT count(*) n FROM inspections")["rows"][0]["n"], 39567)
        self.assertTrue(self.query("SELECT inspection_id FROM inspections")["truncated"])

    def test_document_search_and_path_isolation(self):
        result = self.service.execute("search_documents", {"query": "false positives qualified", "limit": 3})
        self.assertNotIn("error", result, result)
        self.assertTrue(any("spill-response-sop" in d["path"] for d in result["documents"]))
        path = next(d["path"] for d in result["documents"] if "spill-response-sop" in d["path"])
        full = self.service.execute("get_document", {"path": path})
        self.assertIn("Corrected detections do not", full["body"])
        self.assertIn("error", self.service.execute("get_document", {"path": "../../.env"}))

    def test_spill_compliance_joins_effective_detections_sop_and_report_lines(self):
        result = self.service.execute("get_spill_compliance", {"area": "southwest block 24"})
        self.assertNotIn("error", result, result)
        self.assertEqual((result["start_date"], result["end_date"]), ("2026-07-19", "2026-10-16"))
        self.assertEqual(result["rule"]["containment_limit_hours"], 4)
        self.assertEqual(result["total_spills"], 10)
        self.assertEqual(result["counts"], {"breach": 2, "within_limit": 8, "unknown": 0})
        breaches = {row["asset_id"]: row for row in result["rows"] if row["status"] == "breach"}
        self.assertEqual({well: row["containment_hours"] for well, row in breaches.items()},
                         {"WELL-0082": 7.1, "WELL-0100": 8.1})
        self.assertEqual(breaches["WELL-0082"]["hours_over_limit"], 3.1)
        self.assertTrue(any(c.get("path", "").endswith("spill-response-sop.md")
                            and c["line_start"] == 51 for c in result["citations"]))
        report_sources = [c for c in result["citations"] if c.get("path", "").endswith("MNT-0082-028.md")]
        self.assertEqual({c["line_start"] for c in report_sources}, {12, 66})
        self.assertTrue(all("contained" in c["quote"].lower() for c in report_sources))
        capped = self.service.execute("get_spill_compliance", {"area": result["area"]["area_id"], "limit": 1})
        self.assertEqual(capped["counts"], result["counts"])
        self.assertEqual(capped["total_spills"], 10)
        self.assertTrue(capped["truncated"])
        self.assertEqual(capped["omitted_rows"], 9)
        self.assertEqual(capped["rows"][0]["status"], "breach")

    def test_spill_compliance_respects_runtime_false_positive_correction(self):
        corrected = self.service.execute("correct_inspection", {
            "inspection_id": "INS-0082-0198", "field": "spill_detected", "value": False,
            "reason": "Qualified inspector confirmed water rather than crude oil."})
        self.assertTrue(corrected["applied"])
        result = self.service.execute("get_spill_compliance", {"area": "Southwest Block 24"})
        self.assertEqual(result["counts"]["breach"], 1)
        self.assertEqual(result["total_spills"], 9)
        self.assertGreaterEqual(result["excluded_corrected_detections"], 1)
        self.assertFalse(any(row["inspection_id"] == "INS-0082-0198" for row in result["rows"]))

    def test_spill_compliance_missing_or_conflicting_report_times_are_unknown(self):
        path = "assets/WELL-0082/maintenance-reports/MNT-0082-028.md"
        with closing(connect(self.db)) as conn:
            original = conn.execute("SELECT body FROM documents WHERE dataset_id=? AND path=?", (self.dataset_id, path)).fetchone()[0]
        for body in (
            "Repair took 10 hours. Written notice sent 7.1 hours after discovery.\n| SR-02 | Not met | No elapsed time recorded |",
            "Contained about 7.1 hours after discovery.\nContained about 2.0 hours after discovery.",
        ):
            try:
                with closing(connect(self.db)) as conn, conn:
                    conn.execute("UPDATE documents SET body=? WHERE dataset_id=? AND path=?", (body, self.dataset_id, path))
                result = self.service.execute("get_spill_compliance", {"area": "Southwest Block 24"})
                row = next(row for row in result["rows"] if row["inspection_id"] == "INS-0082-0198")
                self.assertEqual(row["status"], "unknown")
                self.assertIsNone(row["containment_hours"])
                self.assertEqual(result["counts"]["unknown"], 1)
                self.assertEqual(result["counts"]["breach"], 1)
            finally:
                with closing(connect(self.db)) as conn, conn:
                    conn.execute("UPDATE documents SET body=? WHERE dataset_id=? AND path=?", (original, self.dataset_id, path))

    def test_spill_compliance_uses_imported_rule_and_handles_missing_rule(self):
        path = "fleet/sops/spill-response-sop.md"
        with closing(connect(self.db)) as conn:
            original = conn.execute("SELECT body FROM documents WHERE dataset_id=? AND path=?", (self.dataset_id, path)).fetchone()[0]
        for body, expected_limit, expected_unknown in (
            (original.replace("contained within **4 hours**", "contained within **8 hours**"), 8, 0),
            ("No explicit containment deadline imported.", None, 10),
        ):
            try:
                with closing(connect(self.db)) as conn, conn:
                    conn.execute("UPDATE documents SET body=? WHERE dataset_id=? AND path=?", (body, self.dataset_id, path))
                result = self.service.execute("get_spill_compliance", {"area": "Southwest Block 24"})
                self.assertEqual(result["rule"]["containment_limit_hours"], expected_limit)
                self.assertEqual(result["counts"]["unknown"], expected_unknown)
                if expected_limit == 8:
                    self.assertEqual(result["counts"]["breach"], 1)
                    row = next(row for row in result["rows"] if row["asset_id"] == "WELL-0082")
                    self.assertEqual(row["status"], "within_limit")
            finally:
                with closing(connect(self.db)) as conn, conn:
                    conn.execute("UPDATE documents SET body=? WHERE dataset_id=? AND path=?", (original, self.dataset_id, path))

    def test_containment_parser_accepts_only_explicit_elapsed_time(self):
        self.assertEqual([row["hours"] for row in _containment_evidence(
            "The release was **contained about 7 hours after the inspection flight**.\n"
            "Contained 180 minutes after discovery.")], [7, 3])
        self.assertEqual(_containment_evidence(
            "Contain within 4 hours. Well stopped for 10 hours.\n"
            "Written notice 7 hours after discovery.\nContained within 4 hours of discovery."), [])
        self.assertEqual(_containment_evidence(
            "It was not contained 3 hours after discovery.\n"
            "It should be contained 3 hours after discovery."), [])

    def test_spill_compliance_validates_area_dates_and_bounds(self):
        for arguments in (
            {"area": "%"}, {"area": "Southwest", "days": 90},
            {"area": "Southwest Block 24", "days": 0},
            {"area": "Southwest Block 24", "limit": 301},
            {"area": "Southwest Block 24", "limit": True},
            {"area": "Southwest Block 24", "start_date": "2026-10-20", "end_date": "2026-10-16"},
        ):
            self.assertIn("error", self.service.execute("get_spill_compliance", arguments), arguments)
        result = self.service.execute("get_spill_compliance", {
            "area": "Southwest Block 24", "start_date": "2026-09-10", "end_date": "2026-09-10"})
        self.assertEqual(result["start_date"], "2026-09-10")
        self.assertEqual(result["end_date"], "2026-09-10")
        self.assertEqual(result["total_spills"], 3)
        self.assertTrue(all(row["inspected_at"].startswith("2026-09-10") for row in result["rows"]))
        self.assertIn("INS-0082-0198", [row["inspection_id"] for row in result["rows"]])

    def test_validation_prevents_invalid_updates(self):
        for field, value in (("wellhead_temp_c", float("nan")), ("spill_detected", 1), ("spill_area_pct", 110), ("spill_est_volume_bbl", -1), ("notes", "oops")):
            self.assertIn("error", self.service.execute("correct_inspection", {"inspection_id": "INS-0001-0203", "field": field, "value": value, "reason": "test"}))
        self.assertIn("error", self.service.execute("add_shared_note", {"asset_id": "WELL-0001", "text": "Question", "valid_from": "2026-10-20", "valid_to": "2026-10-10"}))
        self.assertEqual(normalize_asset_id("well 7"), "WELL-0007")


if __name__ == "__main__":
    unittest.main()
