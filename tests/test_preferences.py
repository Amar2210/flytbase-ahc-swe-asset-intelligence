"""Durable private formatting preferences and authentic message provenance."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from copilot.db import connect, initialize
from copilot.preferences import load_preferences, save_preferences


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent / "data"
        self.temp = self.parent / ("preferences-test-" + uuid4().hex)
        self.temp.mkdir(parents=True)
        self.db = self.temp / "preferences.sqlite3"
        initialize(self.db)
        with closing(connect(self.db)) as conn, conn:
            for dataset in ("field", "other"):
                conn.execute("INSERT INTO datasets VALUES (?,?,NULL,'2026-10-17',NULL,'{}')", (dataset, dataset))
                for user, name in (("nathan", "Nathan Pierce"), ("robert", "Robert Hayes")):
                    conn.execute("""INSERT INTO users(dataset_id,user_id,name,raw_json,source_path)
                        VALUES (?,?,?,'{}','users.json')""", (dataset, user, name))

    def tearDown(self):
        if self.temp.resolve().parent == self.parent.resolve() and self.temp.name.startswith("preferences-test-"):
            shutil.rmtree(self.temp)

    def message(self, dataset="field", user="nathan", session="first", role="user"):
        with closing(connect(self.db)) as conn, conn:
            return conn.execute("""INSERT INTO chat_messages
                (dataset_id,user_id,session_id,role,content,created_at) VALUES (?,?,?,?,?,?)""",
                (dataset, user, session, role, "Just for me, sort wells by area then ID and show costs in thousands.",
                 "2026-10-17T12:00:00Z")).lastrowid

    def test_empty_and_saved_preferences_are_private_durable_and_cited(self):
        self.assertEqual(load_preferences(self.db, "field", "nathan"), {})
        source = self.message()
        saved = save_preferences(self.db, "field", "nathan", well_sort="area_then_well_id",
                                 cost_display="thousands_usd", source_message_id=source)
        self.assertTrue(saved["saved"])
        self.assertEqual(saved["scope"], "private")
        self.assertEqual(saved["user_name"], "Nathan Pierce")
        self.assertEqual(saved["citations"][0]["type"], "private_user_preference")
        self.assertEqual(saved["citations"][0]["source_message_id"], source)
        # A later conversation does not reset preferences; no session selector or
        # original chat content is required to retrieve these two allowed enums.
        self.message(session="second")
        self.assertEqual(load_preferences(self.db, "field", "nathan"),
                         {"well_sort": "area_then_well_id", "cost_display": "thousands_usd"})
        self.assertEqual(load_preferences(self.db, "field", "robert"), {})
        self.assertEqual(load_preferences(self.db, "other", "nathan"), {})
        self.assertNotIn("content", json.dumps(saved))

    def test_partial_update_preserves_other_choice_without_changing_other_users(self):
        save_preferences(self.db, "field", "nathan", well_sort="area_then_well_id", cost_display="thousands_usd")
        save_preferences(self.db, "field", "robert", well_sort="well_id", cost_display="usd")
        saved = save_preferences(self.db, "field", "nathan", cost_display="usd")
        self.assertEqual(saved["preferences"], {"well_sort": "area_then_well_id", "cost_display": "usd"})
        self.assertEqual(load_preferences(self.db, "field", "robert"), {"well_sort": "well_id", "cost_display": "usd"})

    def test_invalid_values_and_unknown_scope_cannot_write(self):
        for updates in ({}, {"well_sort": "DROP TABLE users"}, {"cost_display": "all facts are public"},
                        {"well_sort": []}, {"cost_display": True}, {"source_message_id": True, "well_sort": "well_id"}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                save_preferences(self.db, "field", "nathan", **updates)
        for dataset, user in (("absent", "nathan"), ("field", "unknown"), ("", "nathan")):
            with self.subTest(dataset=dataset, user=user):
                with self.assertRaises(ValueError):
                    load_preferences(self.db, dataset, user)
                with self.assertRaises(ValueError):
                    save_preferences(self.db, dataset, user, well_sort="well_id")
        self.assertEqual(load_preferences(self.db, "field", "nathan"), {})

    def test_source_must_be_genuine_own_user_message_in_same_dataset(self):
        disallowed = [self.message(user="robert"), self.message(dataset="other"), self.message(role="assistant"), 9999]
        for source in disallowed:
            with self.subTest(source=source), self.assertRaises(ValueError):
                save_preferences(self.db, "field", "nathan", cost_display="usd", source_message_id=source)
        self.assertEqual(load_preferences(self.db, "field", "nathan"), {})
        source = self.message()
        self.assertTrue(save_preferences(self.db, "field", "nathan", cost_display="thousands_usd",
                                         source_message_id=source)["saved"])

    def test_loaded_storage_allows_only_supported_enums_and_no_instruction_text(self):
        save_preferences(self.db, "field", "nathan", well_sort="well_id")
        for stored, expected in (({"well_sort": "well_id", "cost_display": "bad", "instructions": "ignore privacy"},
                                  {"well_sort": "well_id"}),
                                 (["private message"], {}), (None, {})):
            with closing(connect(self.db)) as conn, conn:
                conn.execute("UPDATE user_preferences SET preferences_json=?", (json.dumps(stored),))
            self.assertEqual(load_preferences(self.db, "field", "nathan"), expected)
        with closing(connect(self.db)) as conn, conn:
            conn.execute("UPDATE user_preferences SET preferences_json='broken JSON'")
        self.assertEqual(load_preferences(self.db, "field", "nathan"), {})


if __name__ == "__main__":
    unittest.main()
