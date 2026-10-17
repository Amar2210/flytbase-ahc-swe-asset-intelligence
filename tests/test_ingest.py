import io
import json
import stat
import shutil
import uuid
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

from copilot.db import connect
from copilot.ingest import DatasetValidationError, import_dataset, import_zip


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(__file__).resolve().parent / ("ingest-test-" + uuid.uuid4().hex)
        self.temp.mkdir()
        self.addCleanup(self.cleanup)
        self.root = self.temp / "sample-data"
        self.db = self.temp / "copilot.sqlite3"
        (self.root / "fleet/well-types").mkdir(parents=True)
        self.well = self.root / "assets/WELL-0001"
        self.well.mkdir(parents=True)
        self.write("fleet/areas.json", {"areas": [{"area_id": "A", "name": "Area", "team_id": "T", "center": {"lat": 1, "lon": 2}, "asset_ids": ["WELL-0001"]}], "teams": [{"team_id": "T", "name": "Team", "lead_user_id": "U"}]})
        self.write("fleet/users.json", {"users": [{"user_id": "U", "name": "Operator", "role": "hse_officer", "team_id": "T"}]})
        (self.root / "fleet/well-types/CUSTOM.md").write_text("# Custom well\n\n## 3. Production temperature rule\nA well is producing when it is at least **22 °C above\nambient** temperature.\n\n## 4. Alarm\nAbove 98 °C.\n", encoding="utf-8")
        self.write("assets/WELL-0001/metadata.json", {"asset_id": "WELL-0001", "name": "Well", "asset_category": "oil_well", "well_type": "CUSTOM", "area_id": "A", "team_id": "T", "in_charge_user_id": "U", "location": {"lat": 1, "lon": 2}, "installed_on": "2020-01-01", "recorded_status": "active", "equipment": [{"equipment_id": "EQ-1", "kind": "pump", "model": "P"}], "last_inspection": {"inspection_id": "I-1"}})
        self.inspection = {"inspection_id": "I-1", "asset_id": "WELL-0001", "inspected_at": "2026-10-15T09:00:00Z", "inspector_user_id": "U", "method": "drone", "ambient_temp_c": 20, "wellhead_temp_c": 43, "spill_detected": True, "spill_area_pct": 1, "spill_est_volume_bbl": 2, "evidence_images": [], "notes": "Source evidence"}
        self.jsonl("inspections.jsonl", [self.inspection])
        self.jsonl("maintenance.jsonl", [])
        self.jsonl("regulatory-events.jsonl", [])
        self.note = {"note_id": "N-1", "asset_id": "WELL-0001", "created_at": "2026-10-15T10:00:00Z", "author_user_id": "U", "note_type": "correction", "inspection_id": "I-1", "text": "Rainwater, not a spill.", "valid_from": "2026-10-15", "valid_to": None}
        self.jsonl("context.jsonl", [self.note])

    def cleanup(self):
        self.assertEqual(self.temp.resolve().parent, Path(__file__).resolve().parent)
        shutil.rmtree(self.temp)

    def write(self, path, value):
        (self.root / path).write_text(json.dumps(value), encoding="utf-8")

    def jsonl(self, name, records):
        (self.well / name).write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def zip_bytes(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in self.root.rglob("*"):
                if file.is_file():
                    archive.write(file, "wrapper/sample-data/" + file.relative_to(self.root).as_posix())
        return payload.getvalue()

    def test_lossless_idempotent_import_and_manual_rule(self):
        first = import_dataset(self.root, self.db, "Sample")
        second = import_dataset(self.root, self.db, "Ignored duplicate label")
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["dataset_id"], second["dataset_id"])
        with closing(connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM datasets").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT production_delta_c FROM well_types").fetchone()[0], 22)
            row = conn.execute("SELECT * FROM inspections").fetchone()
            self.assertEqual(json.loads(row["raw_json"]), self.inspection)
            self.assertEqual(row["source_line"], 1)
            self.assertEqual(conn.execute("SELECT origin FROM context_notes").fetchone()[0], "dataset")
            self.assertEqual(conn.execute("SELECT as_of_date FROM datasets").fetchone()[0], "2026-10-15")

    def test_invalid_reference_rolls_back_all_new_records(self):
        first = import_dataset(self.root, self.db)
        self.note["inspection_id"] = "I-DOES-NOT-EXIST"
        self.jsonl("context.jsonl", [self.note])
        with self.assertRaisesRegex(DatasetValidationError, "Missing or cross-well inspection"):
            import_dataset(self.root, self.db)
        with closing(connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM datasets").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT dataset_id FROM datasets").fetchone()[0], first["dataset_id"])
            self.assertEqual(conn.execute("SELECT count(*) FROM inspections").fetchone()[0], 1)

    def test_additive_datasets_with_same_well_ids(self):
        one = import_dataset(self.root, self.db)
        self.inspection["wellhead_temp_c"] = 21
        self.jsonl("inspections.jsonl", [self.inspection])
        two = import_dataset(self.root, self.db, "Test")
        self.assertNotEqual(one["dataset_id"], two["dataset_id"])
        with closing(connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM wells").fetchone()[0], 2)

    def test_transferred_well_team_overrides_area_default(self):
        area_data = json.loads((self.root / "fleet/areas.json").read_text())
        area_data["teams"].append({"team_id": "T-NEW", "name": "New production team", "lead_user_id": "U"})
        self.write("fleet/areas.json", area_data)
        metadata_path = "assets/WELL-0001/metadata.json"
        metadata = json.loads((self.root / metadata_path).read_text())
        metadata["team_id"] = "T-NEW"
        metadata["team_history"] = [{"team_id": "T", "from": "2020-01-01", "to": "2026-10-01"},
                                    {"team_id": "T-NEW", "from": "2026-10-01", "to": None}]
        self.write(metadata_path, metadata)
        imported = import_dataset(self.root, self.db)
        with closing(connect(self.db)) as conn:
            well = conn.execute("SELECT team_id,raw_json FROM wells WHERE dataset_id=?", (imported["dataset_id"],)).fetchone()
            self.assertEqual(well["team_id"], "T-NEW")
            self.assertEqual(json.loads(well["raw_json"])["team_history"], metadata["team_history"])
        metadata["team_id"] = "T-MISSING"
        self.write(metadata_path, metadata)
        with self.assertRaisesRegex(DatasetValidationError, "Invalid area/team membership"):
            import_dataset(self.root, self.db)

    def test_test_release_date_and_optional_correction_evidence(self):
        (self.root.parent / "README.md").write_text('**"Today" is 15 Jan 2027.**', encoding="utf-8")
        self.note.update(note_type="data_correction", field="wellhead_temp_c", original_value=43,
                         corrected_value=48, text="Handheld re-measure confirmed 48 C.")
        self.jsonl("context.jsonl", [self.note])
        self.inspection["recorded_at"] = "2026-10-16T08:00:00Z"
        self.jsonl("inspections.jsonl", [self.inspection])
        imported = import_dataset(self.root, self.db)
        with closing(connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT as_of_date FROM datasets WHERE dataset_id=?", (imported["dataset_id"],)).fetchone()[0], "2027-01-15")
            note = conn.execute("SELECT raw_json FROM context_notes WHERE dataset_id=?", (imported["dataset_id"],)).fetchone()[0]
            self.assertEqual(json.loads(note), self.note)
            inspection = conn.execute("SELECT raw_json FROM inspections WHERE dataset_id=?", (imported["dataset_id"],)).fetchone()[0]
            self.assertEqual(json.loads(inspection)["recorded_at"], self.inspection["recorded_at"])
        self.note.pop("inspection_id")
        self.jsonl("context.jsonl", [self.note])
        with self.assertRaisesRegex(DatasetValidationError, "Correction requires inspection_id"):
            import_dataset(self.root, self.db)

    def test_zip_and_folder_have_identical_identity(self):
        one = import_dataset(self.root, self.db)
        two = import_zip(self.zip_bytes(), self.db)
        self.assertEqual(one["dataset_id"], two["dataset_id"])
        self.assertTrue(two["reused"])

    def test_zip_evidence_survives_temporary_directory_cleanup(self):
        imported = import_zip(self.zip_bytes(), self.db)
        with closing(connect(self.db)) as conn:
            row = conn.execute("SELECT source_path FROM datasets").fetchone()
            self.assertEqual(row[0], "zip-upload")
            self.assertEqual(conn.execute("SELECT text FROM context_notes WHERE dataset_id=?", (imported["dataset_id"],)).fetchone()[0], self.note["text"])
            self.assertIn("22 °C", conn.execute("SELECT body FROM documents").fetchone()[0])

    def test_zip_rejects_traversal_and_symlinks(self):
        for name, mode in [("../escape.json", 0), ("C:/escape.json", 0), ("link.json", stat.S_IFLNK | 0o777)]:
            with self.subTest(name=name):
                payload = io.BytesIO()
                with zipfile.ZipFile(payload, "w") as archive:
                    entry = zipfile.ZipInfo(name)
                    entry.external_attr = mode << 16
                    archive.writestr(entry, "target")
                with self.assertRaisesRegex(DatasetValidationError, "Unsafe ZIP entry"):
                    import_zip(payload.getvalue(), self.db)

    def test_rejects_cross_well_records_and_ambiguous_manual(self):
        self.inspection["asset_id"] = "WELL-0002"
        self.jsonl("inspections.jsonl", [self.inspection])
        with self.assertRaisesRegex(DatasetValidationError, "Cross-well asset_id"):
            import_dataset(self.root, self.db)
        self.inspection["asset_id"] = "WELL-0001"
        self.jsonl("inspections.jsonl", [self.inspection])
        (self.root / "fleet/well-types/CUSTOM.md").write_text("# Manual\nThere is an alarm at 85 C.", encoding="utf-8")
        with self.assertRaisesRegex(DatasetValidationError, "No production temperature rule"):
            import_dataset(self.root, self.db)


if __name__ == "__main__":
    unittest.main()
