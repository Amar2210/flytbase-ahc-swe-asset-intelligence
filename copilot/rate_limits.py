"""Shared local input-token pacing. The ledger contains no prompts or keys."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable
import uuid


class RateLimitError(RuntimeError):
    """A safe local pacing error; no provider request was sent."""


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    tokens: int
    waited_seconds: float


class TokenBudget:
    """Atomic reservations shared by every client using this database and scope.

    This is an app-side rolling window, not a provider quota meter. Calls made
    outside this app are unknown. Failed HTTP attempts keep their reservation.
    A 10% plus 64-token margin covers small tokenizer/usage discrepancies.
    """

    def __init__(self, db_path: str | Path, *, provider: str, model: str,
                 quota_group: str = "default-project", limit: int = 16000,
                 headroom: int = 1500, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 max_wait: float = 180):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", quota_group):
            raise RateLimitError("Use a short non-secret project label for the quota group.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise RateLimitError("The Google input TPM limit must be a positive integer.")
        if isinstance(headroom, bool) or not isinstance(headroom, int) or not 0 <= headroom < limit:
            raise RateLimitError("TPM headroom must be smaller than the input TPM limit.")
        self.db_path = str(Path(db_path).resolve())
        self.scope = (provider, model, quota_group)
        self.limit, self.budget = limit, limit - headroom
        self.clock, self.sleep = clock, sleep
        self.max_wait = max(1.0, float(max_wait))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS model_token_reservations (
                reservation_id TEXT PRIMARY KEY, provider TEXT NOT NULL,
                model TEXT NOT NULL, quota_group TEXT NOT NULL,
                reserved_at REAL NOT NULL, tokens INTEGER NOT NULL CHECK(tokens >= 0)
            )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS model_token_reservations_scope
                ON model_token_reservations(provider, model, quota_group, reserved_at)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS model_rate_limit_cooldowns (
                provider TEXT NOT NULL, model TEXT NOT NULL, quota_group TEXT NOT NULL,
                until_at REAL NOT NULL,
                PRIMARY KEY(provider, model, quota_group)
            )""")

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=5, isolation_level=None)

    def reserve(self, input_tokens: int | None,
                on_progress: Callable[[dict], None] | None = None) -> Reservation:
        if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
            raise RateLimitError(
                "Input token counting is unavailable. No generation request was sent because "
                "the shared TPM budget cannot safely reserve an unknown input. Retry when token counting is available."
            )
        tokens = math.ceil(input_tokens * 1.10) + 64
        if tokens > self.budget:
            raise RateLimitError(
                f"This request needs a conservative reservation of {tokens:,} tokens, above "
                f"the {self.budget:,}-token local TPM budget. No generation request was sent. "
                "Reduce the request context or configure a verified higher provider limit."
            )
        started = self.clock()
        while True:
            now = self.clock()
            waited = max(0.0, now - started)
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM model_token_reservations WHERE reserved_at <= ?", (now - 60.0,))
                conn.execute("DELETE FROM model_rate_limit_cooldowns WHERE until_at <= ?", (now,))
                rows = conn.execute("""SELECT reserved_at, tokens FROM model_token_reservations
                    WHERE provider=? AND model=? AND quota_group=? ORDER BY reserved_at""", self.scope).fetchall()
                cooldown = conn.execute("""SELECT until_at FROM model_rate_limit_cooldowns
                    WHERE provider=? AND model=? AND quota_group=?""", self.scope).fetchone()
                used = sum(row[1] for row in rows)
                if not cooldown and used + tokens <= self.budget:
                    reservation_id = uuid.uuid4().hex
                    conn.execute("INSERT INTO model_token_reservations VALUES (?,?,?,?,?,?)",
                                 (reservation_id, *self.scope, now, tokens))
                    conn.commit()
                    self._progress(on_progress, "reserved", waited, tokens, used + tokens)
                    return Reservation(reservation_id, tokens, waited)
                wait = max(0.01, (cooldown[0] - now) if cooldown else 0.01)
                if used + tokens > self.budget:
                    remaining = used
                    for timestamp, amount in rows:
                        remaining -= amount
                        if remaining + tokens <= self.budget:
                            wait = max(wait, timestamp + 60.0 - now)
                            break
                conn.commit()
            finally:
                conn.close()
            self._progress(on_progress, "queued", waited, tokens, used)
            if waited >= self.max_wait:
                raise RateLimitError(
                    "The shared Google TPM queue is still busy. No generation request was sent "
                    "for this attempt. Retry after the other requests finish."
                )
            self.sleep(min(1.0, wait, self.max_wait - waited))

    def _progress(self, callback, status, waited, reserved, used):
        if callback:
            callback({"rate_limit_status": status,
                      "rate_limit_wait_seconds": round(waited, 2),
                      "rate_limit_reserved_tokens": reserved,
                      "rate_limit_window_reserved_tokens": used,
                      "rate_limit_budget_tokens": self.budget,
                      "rate_limit_limit_tokens": self.limit,
                      "rate_limit_quota_group": self.scope[2]})

    def reconcile(self, reservation: Reservation, actual_input_tokens: int | None) -> None:
        """Increase an underestimated reservation; never release failed attempts."""
        if isinstance(actual_input_tokens, bool) or not isinstance(actual_input_tokens, int) or actual_input_tokens < 0:
            return
        with closing(self._connect()) as conn:
            conn.execute("""UPDATE model_token_reservations SET tokens=MAX(tokens, ?)
                WHERE reservation_id=? AND provider=? AND model=? AND quota_group=?""",
                         (actual_input_tokens, reservation.reservation_id, *self.scope))

    def cooldown(self, seconds: float = 60.0) -> None:
        seconds = max(1.0, min(float(seconds), 3600.0))
        with closing(self._connect()) as conn:
            conn.execute("""INSERT INTO model_rate_limit_cooldowns VALUES (?,?,?,?)
                ON CONFLICT(provider,model,quota_group) DO UPDATE
                SET until_at=MAX(until_at,excluded.until_at)""", (*self.scope, self.clock() + seconds))
