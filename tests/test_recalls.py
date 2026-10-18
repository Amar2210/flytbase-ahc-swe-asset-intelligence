"""Recall semantics: actual installations, shared reports and private preferences."""
from contextlib import closing
from pathlib import Path
import unittest
from uuid import uuid4

from copilot.db import connect, initialize
from copilot.recalls import parse_parts_installed, parse_recall_notice
from copilot.tools import ToolService


NOTICE = """# Supplier Recall Notice
**Date:** 1 Dec 2026
**Reference:** A-2026-01
Acme ZP-42 downhole rod pumps from these manufacturing lots:

- LOT-2601-A
- LOT-2601-C

All other lots, including LOT-2601-B, are not affected.
"""


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).resolve().parents[1] / "data" / ("recall-test-" + uuid4().hex + ".sqlite3")
        initialize(self.path)
        self.ds = "recall-test"
        with closing(connect(self.path)) as conn, conn:
            # These are small independent records in the public dataset's format.
            self.insert(conn, "datasets", dataset_id=self.ds, label="Recall test", imported_at="2027-01-15T00:00:00Z", as_of_date="2027-01-15", counts_json="{}")
            for uid, name, team in (("author", "First Reporter", "A"), ("reader", "Other Reader", "M"), ("indirect", "Indirect Reporter", "B")):
                self.insert(conn, "users", dataset_id=self.ds, user_id=uid, name=name, role="production_supervisor", team_id=team, raw_json="{}", source_path="fleet/users.json")
            for area in ("A", "B"):
                self.insert(conn, "areas", dataset_id=self.ds, area_id=area, name="Area " + area, team_id=area, raw_json="{}", source_path="fleet/areas.json")
            for number, area, team in ((9001, "B", "A"), (9002, "A", "A"), (9003, "B", "B"), (9004, "A", "A")):
                self.insert(conn, "wells", dataset_id=self.ds, asset_id=f"WELL-{number}", name=f"Test {number}", area_id=area, team_id=team, raw_json="{}", source_path=f"assets/WELL-{number}/metadata.json")
            self.insert(conn, "documents", dataset_id=self.ds, document_id="notice", path="fleet/sops/recall.md", kind="sop", title="Recall", body=NOTICE)
            for number in (9001, 9002, 9003, 9004):
                self.install(conn, number, "old", "2026-12-02", "LOT-2601-A", 12500 + number)
            # A later downhole check must not overwrite the installed pump.
            self.insert(conn, "maintenance", dataset_id=self.ds, maintenance_id="check", asset_id="WELL-9001", started_at="2027-01-10T08:00:00Z", completed_at="2027-01-10T10:00:00Z", equipment_kind="downhole_pump", maintenance_type="scheduled", issue="Follow-up check", action_taken="Checked fluid level", raw_json="{}", source_path="assets/WELL-9001/maintenance.jsonl")
            # A replacement with an unaffected lot removes the original recall.
            self.install(conn, 9004, "new", "2026-12-10", "LOT-2601-B", 31000)
        self.author = ToolService(self.path, self.ds, "author", "2027-01-15")

    @staticmethod
    def insert(conn, table, **record):
        conn.execute(f"INSERT INTO {table} ({','.join(record)}) VALUES ({','.join('?' for _ in record)})", tuple(record.values()))

    def install(self, conn, number, suffix, day, lot, cost, finish=None):
        mid = f"INSTALL-{number}-{suffix}"
        path = f"assets/WELL-{number}/maintenance-reports/{mid}.md"
        self.insert(conn, "documents", dataset_id=self.ds, document_id=mid, path=path, asset_id=f"WELL-{number}", kind="report", title=mid,
                    body=f"# Repair\n\n**Parts installed:** Acme ZP-42 pump, serial SN-{number}-{suffix}, lot {lot}\n")
        self.insert(conn, "maintenance", dataset_id=self.ds, maintenance_id=mid, asset_id=f"WELL-{number}", started_at=day + "T08:00:00Z", completed_at=(finish or day) + "T12:00:00Z", equipment_kind="downhole_pump", maintenance_type="corrective", report_file=f"maintenance-reports/{mid}.md", cost_total=cost, raw_json="{}", source_path=f"assets/WELL-{number}/maintenance.jsonl", source_line=7)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)

    def test_latest_installation_not_later_check_or_safe_replacement(self):
        result = self.author.get_recalled_pumps()
        self.assertEqual({row["asset_id"] for row in result["rows"]}, {"WELL-9001", "WELL-9002", "WELL-9003"})
        row = next(row for row in result["rows"] if row["asset_id"] == "WELL-9001")
        self.assertEqual(row["installation_job"], "INSTALL-9001-old")
        self.assertTrue(row["source"].endswith(":3"))
        self.assertTrue(any(c.get("source_line") == 7 for c in result["citations"]))

    def test_preview_is_read_only_then_confirmed_report_propagates(self):
        args = {"asset_id": "WELL-9001", "effective_on": "2027-01-14", "statement": "My crew replaced the pump yesterday; work order pending.", "evidence_basis": "direct_report"}
        preview = self.author.preview_equipment_replacement(args)
        self.assertEqual(preview["reported_by"], "First Reporter")
        self.assertIn("WELL-9001", {row["asset_id"] for row in self.author.get_recalled_pumps()["rows"]})
        saved = self.author.record_equipment_replacement(**preview["arguments"])
        self.assertEqual(saved["report"]["reported_by_user_id"], "author")
        reader = ToolService(self.path, self.ds, "reader", "2027-01-15")
        result = reader.get_recalled_pumps()
        self.assertNotIn("WELL-9001", {row["asset_id"] for row in result["rows"]})
        self.assertEqual(result["excluded_reported_replacements"][0]["reported_by"], "First Reporter")
        with closing(connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM maintenance WHERE asset_id='WELL-9001'").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT author_user_id FROM context_notes WHERE note_id=?", (saved["report"]["source_note_id"],)).fetchone()[0], "author")

    def test_indirect_report_retained_and_actual_author_preserved(self):
        other = ToolService(self.path, self.ds, "indirect", "2027-01-15")
        saved = other.record_equipment_replacement(asset_id="WELL-9003", effective_on="2027-01-15", statement="First Reporter told me it was replaced.", evidence_basis="direct_report", claimed_source_name="First Reporter")
        self.assertEqual(saved["report"]["evidence_basis"], "hearsay")
        self.assertEqual(saved["report"]["status"], "unconfirmed")
        self.assertEqual(saved["report"]["reported_by_user_id"], "indirect")
        self.assertEqual(saved["report"]["claimed_source_user_id"], "author")
        result = self.author.get_recalled_pumps()
        row = next(row for row in result["rows"] if row["asset_id"] == "WELL-9003")
        self.assertEqual(row["status"], "Replacement unconfirmed")
        self.assertEqual(result["unconfirmed_reports"][0]["reported_by"], "Indirect Reporter")
        self.assertEqual(result["unconfirmed_reports"][0]["claimed_source"], "First Reporter")

    def test_private_format_and_own_team_override_area_team(self):
        self.author.save_my_preferences(well_sort="area_then_well_id", cost_display="thousands_usd")
        result = self.author.get_recalled_pumps(my_area=True, include_install_cost=True)
        self.assertEqual([row["asset_id"] for row in result["rows"]], ["WELL-9002", "WELL-9001"])
        self.assertEqual(result["rows"][0]["installation_cost_usd_thousands"], 21.502)
        reader = ToolService(self.path, self.ds, "reader", "2027-01-15")
        self.assertEqual(reader.get_my_preferences()["preferences"], {})
        self.assertIn("installation_cost_usd", reader.get_recalled_pumps(include_install_cost=True)["rows"][0])

    def test_old_report_does_not_override_a_newer_installation(self):
        self.author.record_equipment_replacement(asset_id="WELL-9001", effective_on="2026-11-01", statement="An earlier pump was replaced.", evidence_basis="direct_report")
        self.assertIn("WELL-9001", {row["asset_id"] for row in self.author.get_recalled_pumps()["rows"]})

    def test_other_dataset_replacement_cannot_remove_same_well_id(self):
        with closing(connect(self.path)) as conn, conn:
            self.insert(conn, "datasets", dataset_id="other", label="Other field", imported_at="2027-01-15", counts_json="{}")
            self.insert(conn, "users", dataset_id="other", user_id="author", name="Different Person", team_id="A", raw_json="{}", source_path="fleet/users.json")
            self.insert(conn, "equipment_replacement_reports", report_id="other-report", dataset_id="other", asset_id="WELL-9001", reported_by_user_id="author", evidence_basis="direct_report", status="reported_replaced", effective_on="2027-01-14", statement="Replacement on other field", created_at="2027-01-15")
        result = self.author.get_recalled_pumps()
        self.assertIn("WELL-9001", {row["asset_id"] for row in result["rows"]})
        self.assertFalse(result["excluded_reported_replacements"])
        self.assertFalse(any(c.get("record_id") == "other-report" for c in result["citations"]))

    def test_in_progress_installation_not_treated_as_completed(self):
        with closing(connect(self.path)) as conn, conn:
            self.install(conn, 9002, "planned", "2027-01-14", "LOT-2601-B", 45000, finish="2027-01-20")
        result = self.author.get_recalled_pumps()
        self.assertEqual(result["coverage"]["in_progress_installation_reports"], 1)
        self.assertIn("WELL-9002", {row["asset_id"] for row in result["rows"]})
        row = next(row for row in result["rows"] if row["asset_id"] == "WELL-9002")
        self.assertEqual(row["status"], "Installation in progress; current pump unconfirmed")

    def test_current_esp_well_does_not_inherit_old_rod_pump_recall(self):
        with closing(connect(self.path)) as conn, conn:
            self.insert(conn, "equipment", dataset_id=self.ds, equipment_id="ESP-9002", asset_id="WELL-9002", kind="esp_pump", model="ESP-X", raw_json="{}", source_path="assets/WELL-9002/metadata.json")
        self.assertNotIn("WELL-9002", {row["asset_id"] for row in self.author.get_recalled_pumps()["rows"]})

    def test_invalid_actor_target_date_and_unknown_source_fail_without_write(self):
        base = {"asset_id": "WELL-9001", "effective_on": "2027-01-14", "statement": "Replaced.", "evidence_basis": "direct_report"}
        for change in ({"asset_id": "WELL-9999"}, {"effective_on": "2027-01-16"}, {"claimed_source_name": "Missing Person"}, {"evidence_basis": "verified"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.author.preview_equipment_replacement({**base, **change})
        with closing(connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM equipment_replacement_reports").fetchone()[0], 0)

    def test_notice_only_positive_lots_and_report_line_provenance(self):
        notice = parse_recall_notice({"body": NOTICE, "path": "notice.md"})
        self.assertEqual(notice["affected_lots"], ["LOT-2601-A", "LOT-2601-C"])
        self.assertEqual(notice["model"], "ZP-42")
        self.assertEqual(notice["notice_date"], "2026-12-01")
        parts = parse_parts_installed("# Report\n**Parts installed:** Acme ZP-42 pump, serial S-42, lot LOT-2601-A\n")
        self.assertEqual(parts[0]["line"], 2)


if __name__ == "__main__":
    unittest.main()
