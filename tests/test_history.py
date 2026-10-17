"""Exact private-history calendar boundaries, paging and tenant isolation."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from copilot.db import connect, initialize
from copilot.history import resolve_history_window, timestamp_us
from copilot.tools import ToolService


class HistoryWindowTests(unittest.TestCase):
    def window(self, period=None, now="2026-10-17T10:00:00+00:00", zone="UTC", **bounds):
        return resolve_history_window(period=period, now=datetime.fromisoformat(now), timezone_name=zone, **bounds)

    def test_yesterday_is_full_local_day_and_uses_local_clock(self):
        result = self.window("yesterday", now="2026-01-01T00:15:00+00:00", zone="America/Chicago")
        self.assertEqual(result["reference_today"], "2025-12-31")
        self.assertEqual(result["local_start_date"], "2025-12-30")
        self.assertEqual(result["start_at_utc"], "2025-12-30T06:00:00Z")
        self.assertEqual(result["end_at_utc_exclusive"], "2025-12-31T06:00:00Z")

    def test_india_date_conversion_crosses_utc_day(self):
        result = self.window("yesterday", now="2026-10-16T20:00:00+00:00", zone="Asia/Kolkata")
        self.assertEqual(result["reference_today"], "2026-10-17")
        self.assertEqual(result["start_at_utc"], "2026-10-15T18:30:00Z")
        self.assertEqual(result["end_at_utc_exclusive"], "2026-10-16T18:30:00Z")

    def test_calendar_weeks_start_monday_across_year_boundary(self):
        this_week = self.window("this week", now="2026-01-01T12:00:00+00:00")
        last_week = self.window("last_week", now="2026-01-01T12:00:00+00:00")
        self.assertEqual((this_week["local_start_date"], this_week["local_end_date_exclusive"]), ("2025-12-29", "2026-01-05"))
        self.assertEqual((last_week["local_start_date"], last_week["local_end_date_exclusive"]), ("2025-12-22", "2025-12-29"))
        sunday = self.window("this week", now="2027-01-03T12:00:00+00:00")
        monday = self.window("last week", now="2027-01-04T12:00:00+00:00")
        self.assertEqual(sunday["start_at_utc"], monday["start_at_utc"])
        self.assertEqual(sunday["end_at_utc_exclusive"], monday["end_at_utc_exclusive"])

    def test_dst_days_are_23_or_25_hours_with_exact_boundaries(self):
        for start, end, hours in (("2026-03-08T05:00:00Z", "2026-03-09T04:00:00Z", 23),
                                  ("2026-11-01T04:00:00Z", "2026-11-02T05:00:00Z", 25)):
            day = start[:10]
            result = self.window(zone="America/New_York", start_date=day, end_date=day)
            self.assertEqual((result["start_at_utc"], result["end_at_utc_exclusive"]), (start, end))
            self.assertEqual(timestamp_us(end) - timestamp_us(start), hours * 3600 * 1_000_000)

    def test_explicit_inclusive_dates_open_bounds_and_validation(self):
        result = self.window(start_date="2024-02-28", end_date="2024-02-29")
        self.assertEqual(result["end_at_utc_exclusive"], "2024-03-01T00:00:00Z")
        self.assertIsNone(self.window(end_date="2026-01-01")["start_at_utc"])
        self.assertIsNone(self.window(start_date="2026-01-01")["end_at_utc_exclusive"])
        for bounds in ({"period": "last month"}, {"period": "yesterday", "start_date": "2026-10-01"},
                       {"start_date": "2026-10-02", "end_date": "2026-10-01"},
                       {"start_date": "2026-02-30"}, {"start_date": "20261001"}, {"zone": "Not/AZone"}):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                self.window(**bounds)
        with self.assertRaises(ValueError):
            resolve_history_window(now=datetime(2026, 1, 1))

    def test_timestamp_comparison_preserves_offsets_and_microseconds(self):
        self.assertEqual(timestamp_us("2026-10-16T00:00:00.000001Z"), timestamp_us("2026-10-16T05:30:00.000001+05:30"))
        self.assertEqual(timestamp_us("2026-10-16T00:00:00.000001Z") - timestamp_us("2026-10-16T00:00:00Z"), 1)
        self.assertIsNone(timestamp_us("2026-10-16T00:00:00"))
        self.assertIsNone(timestamp_us("bad-date"))


class PrivateHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parent = Path(__file__).resolve().parent / "data"
        cls.temp = cls.parent / ("history-test-" + uuid4().hex)
        cls.temp.mkdir(parents=True)
        cls.db = cls.temp / "history.sqlite3"
        initialize(cls.db)
        with closing(connect(cls.db)) as conn, conn:
            for dataset in ("field", "other"):
                conn.execute("INSERT INTO datasets VALUES (?,?,NULL,'2026-10-17',NULL,'{}')", (dataset, dataset))
                for user in ("alice", "bob"):
                    conn.execute("INSERT INTO users(dataset_id,user_id,name,raw_json,source_path) VALUES (?,?,?,'{}','users.json')", (dataset, user, user))

    @classmethod
    def tearDownClass(cls):
        if cls.temp.resolve().parent == cls.parent.resolve() and cls.temp.name.startswith("history-test-"):
            shutil.rmtree(cls.temp)

    def setUp(self):
        with closing(connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM chat_messages")
            conn.execute("DELETE FROM corrections")
        # Deliberately unrelated to the chat clock.
        self.service = ToolService(self.db, "field", "alice", "2001-01-01", field_timezone="Asia/Kolkata")
        self.service.history_reference_now = datetime(2026, 10, 17, 10, tzinfo=timezone.utc)

    def save(self, content, at="2026-10-16T10:00:00Z", dataset="field", user="alice", session="old-session", role="user"):
        with closing(connect(self.db)) as conn, conn:
            return conn.execute("INSERT INTO chat_messages(dataset_id,user_id,session_id,role,content,created_at) VALUES (?,?,?,?,?,?)",
                                (dataset, user, session, role, content, at)).lastrowid

    def search(self, **arguments):
        result = self.service.execute("search_my_conversations", arguments)
        self.assertNotIn("error", result, result)
        return result

    def test_half_open_bounds_include_start_and_final_microsecond(self):
        self.save("Before", "2026-10-15T18:29:59.999999Z")
        first = self.save("First WELL-1", "2026-10-16T00:00:00+05:30")
        last = self.save("Last WELL-2", "2026-10-16T18:29:59.999999Z", session="another-session")
        self.save("After", "2026-10-16T18:30:00Z")
        result = self.search(period="yesterday")
        self.assertEqual([row["message_id"] for row in result["messages"]], [last, first])
        self.assertEqual(result["window"]["reference_today"], "2026-10-17")
        self.assertEqual({row["session_id"] for row in result["messages"]}, {"old-session", "another-session"})
        self.assertEqual([source["record_id"] for source in result["citations"]], [str(last), str(first)])
        self.assertTrue(all(source["scope"] == "private" and source["created_at"] for source in result["citations"]))

    def test_pages_reach_older_than_thirty_without_duplicates_and_freeze_snapshot(self):
        original = [self.save(f"Earlier WELL-{index + 1}", at=f"2026-10-16T10:{index:02d}:00Z") for index in range(45)]
        result = self.search(period="yesterday", limit=7)
        retrieved = [row["message_id"] for row in result["messages"]]
        self.save("Arrived during paging", "2026-10-16T11:00:00Z")
        self.service.history_reference_now = datetime(2026, 11, 20, tzinfo=timezone.utc)
        while result["has_more"]:
            result = self.search(cursor=result["next_cursor"], limit=7)
            self.assertEqual(result["window"]["reference_today"], "2026-10-17")
            retrieved.extend(row["message_id"] for row in result["messages"])
        self.assertEqual(retrieved, list(reversed(original)))
        self.assertIsNone(result["next_cursor"])

    def test_equal_timestamp_tiebreak_and_older_range(self):
        ids = [self.save("Old wells", at="2021-01-01T12:00:00Z") for _ in range(4)]
        for index in range(40):
            self.save("Newer unrelated", session=f"new-{index}")
        first = self.search(start_date="2021-01-01", end_date="2021-01-01", limit=2)
        second = self.search(cursor=first["next_cursor"], limit=2)
        self.assertEqual([row["message_id"] for row in first["messages"] + second["messages"]], list(reversed(ids)))
        self.assertFalse(second["has_more"])

    def test_literal_keywords_entity_and_role_filters(self):
        wanted = self.save("WELL-001 needs PUMP review 100%_", role="assistant")
        self.save("WELL-0001 needs review", role="assistant")
        self.save("WELL-0010 pump review 100%_", role="assistant")
        self.save("well 1 pump review 100%_", role="user")
        result = self.search(query="PUMP 100%_", asset_id="well 1", role="assistant")
        self.assertEqual([row["message_id"] for row in result["messages"]], [wanted])
        self.assertEqual(self.search(query="%") ["messages"][0]["role"], "user")
        self.assertEqual(self.search(query="' OR 1=1 --")["messages"], [])

    def test_current_user_dataset_scope_cannot_be_overridden_or_bypassed(self):
        own = self.save("My WELL-0001")
        self.save("Another user's secret", user="bob")
        self.save("Another dataset's secret", dataset="other")
        self.assertEqual([row["message_id"] for row in self.search()["messages"]], [own])
        self.assertEqual(self.search(query="secret")["messages"], [])
        for parameter in ("user_id", "dataset_id", "session_id"):
            self.assertIn("error", self.service.execute("search_my_conversations", {parameter: "bob"}))
        for sql in ("SELECT * FROM chat_messages", "SELECT * FROM main.chat_messages"):
            self.assertIn("error", self.service.execute("query_records", {"sql": sql}))

    def test_cursor_is_scope_bound_and_rejects_malformed_or_changed_filters(self):
        self.save("one")
        self.save("two")
        cursor = self.search(limit=1)["next_cursor"]
        other_user = ToolService(self.db, "field", "bob")
        other_dataset = ToolService(self.db, "other", "alice")
        for service in (other_user, other_dataset):
            self.assertIn("error", service.execute("search_my_conversations", {"cursor": cursor}))
        for arguments in ({"cursor": "bad!"}, {"cursor": cursor, "query": "changed"}, {"limit": 0},
                          {"limit": True}, {"role": "system"}, {"asset_id": "not-a-well"}):
            self.assertIn("error", self.service.execute("search_my_conversations", arguments))

    def test_empty_search_and_invalid_timestamps_do_not_invent_history(self):
        self.save("Naive clock", at="2026-10-16T10:00:00")
        self.save("Invalid clock", at="invalid")
        result = self.search(period="yesterday")
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["citations"], [])
        self.assertFalse(result["has_more"])

    def test_maximum_unicode_query_can_continue(self):
        query = "😀" * 500
        self.save(query)
        self.save(query)
        first = self.search(query=query, limit=1)
        second = self.search(cursor=first["next_cursor"], limit=1)
        self.assertEqual(len(second["messages"]), 1)
        self.assertFalse(second["has_more"])

    def test_existing_correction_schema_migrates_tag_without_accepting_dispute(self):
        with closing(connect(self.db)) as conn, conn:
            conn.execute("ALTER TABLE corrections DROP COLUMN feedback_kind")
            conn.execute("INSERT INTO corrections(correction_id,dataset_id,inspection_id,field,value_json,author_user_id,reason,created_at,status) VALUES ('old-correction','field','INS-1','spill_detected','false','alice','Verified rainwater','2026-10-16T10:00:00Z','disputed')")
        initialize(self.db)
        with closing(connect(self.db)) as conn:
            row = conn.execute("SELECT feedback_kind,status FROM corrections WHERE correction_id='old-correction'").fetchone()
        self.assertEqual(tuple(row), ("ai_detection_feedback", "disputed"))


if __name__ == "__main__":
    unittest.main()
