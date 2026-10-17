"""Offline rolling-window tests. Fake time avoids provider calls or real waits."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from copilot.rate_limits import RateLimitError, TokenBudget


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class TokenBudgetTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ("rate-test-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.db = self.root / "app.sqlite3"
        self.time = FakeTime()

    def cleanup(self):
        if self.root.resolve().parent == Path(__file__).resolve().parent and self.root.name.startswith("rate-test-"):
            shutil.rmtree(self.root)

    def ledger(self, **kwargs):
        return TokenBudget(self.db, provider="google", model="gemma-test",
                           clock=self.time.clock, sleep=self.time.sleep, **kwargs)

    def test_six_k_then_eleven_k_waits_for_previous_request_to_expire(self):
        first = self.ledger()
        first.reserve(6000)
        # A new instance uses the same ledger, regardless of chat/user/dataset.
        second = self.ledger()
        updates = []
        reservation = second.reserve(11000, updates.append)
        self.assertEqual(reservation.waited_seconds, 60)
        self.assertTrue(any(item["rate_limit_status"] == "queued" for item in updates))
        self.assertEqual(updates[-1]["rate_limit_status"], "reserved")
        self.assertLessEqual(max(self.time.sleeps), 1)
        self.assertEqual(updates[-1]["rate_limit_budget_tokens"], 14500)
        self.assertGreater(reservation.tokens, 11000)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM model_token_reservations").fetchone()[0], 1)

    def test_over_budget_and_unknown_count_fail_without_waiting_or_reserving(self):
        ledger = self.ledger()
        for count, message in [(None, "unknown"), (True, "unknown"), (-1, "unknown"), (15000, "above")]:
            with self.subTest(count=count), self.assertRaisesRegex(RateLimitError, message):
                ledger.reserve(count)
        self.assertEqual(self.time.sleeps, [])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM model_token_reservations").fetchone()[0], 0)

    def test_scope_is_explicit_project_and_model_not_credentials_or_identity(self):
        self.ledger().reserve(11000)
        another = self.ledger(quota_group="another-project")
        self.assertEqual(another.reserve(11000).waited_seconds, 0)
        with closing(sqlite3.connect(self.db)) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(model_token_reservations)")]
        self.assertEqual(columns, ["reservation_id", "provider", "model", "quota_group", "reserved_at", "tokens"])

    def test_usage_upgrade_is_shared_and_failed_reservations_remain(self):
        ledger = self.ledger()
        first = ledger.reserve(6000)
        ledger.reconcile(first, 12000)
        ledger.reconcile(first, 1)  # A later small value cannot free budget.
        self.assertEqual(self.ledger().reserve(3000).waited_seconds, 60)

    def test_cooldown_is_persistent_and_queue_wait_is_bounded(self):
        self.ledger().cooldown(90)
        limited = self.ledger(max_wait=5)
        with self.assertRaisesRegex(RateLimitError, "queue is still busy"):
            limited.reserve(1)
        self.assertEqual(sum(self.time.sleeps), 5)
        self.time.now += 85
        self.assertEqual(self.ledger().reserve(1).waited_seconds, 0)

    def test_atomic_reservations_never_overbook_simultaneous_clients(self):
        # Every thread gets its own SQLite connection/client. Queued clients
        # stop at the fake sleep boundary, so the test never waits in real time.
        self.ledger()
        def no_wait(_):
            raise RateLimitError("queued")
        def reserve(_):
            try:
                ledger = TokenBudget(self.db, provider="google", model="gemma-test",
                                     clock=self.time.clock, sleep=no_wait)
                ledger.reserve(3000)
                return True
            except RateLimitError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(pool.map(reserve, range(8)))
        self.assertEqual(sum(accepted), 4)
        with closing(sqlite3.connect(self.db)) as conn:
            used, count = conn.execute("SELECT SUM(tokens),COUNT(*) FROM model_token_reservations").fetchone()
        self.assertEqual(count, 4)
        self.assertLessEqual(used, 14500)


if __name__ == "__main__":
    unittest.main()
