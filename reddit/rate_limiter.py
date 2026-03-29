"""Rate limiter for Reddit API calls.

Reddit API: 100 requests per minute for OAuth clients.
We limit to 1 req/sec to stay well within limits.
"""

import logging
import os
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.expanduser("~/.reddit/rate_limit.db")
DEFAULT_MIN_INTERVAL = 1.0  # 1 second between requests


class RateLimiter:
    """In-memory rate limiter."""

    def __init__(self, min_interval: float = DEFAULT_MIN_INTERVAL):
        self.min_interval = min_interval
        self._last_request = 0.0

    def acquire(self):
        now = time.time()
        elapsed = now - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request = time.time()


class SharedRateLimiter:
    """SQLite-backed cross-process rate limiter."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH, min_interval: float = DEFAULT_MIN_INTERVAL):
        self.min_interval = min_interval
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rate_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_request_ts REAL DEFAULT 0
            )
        """)
        conn.execute("INSERT OR IGNORE INTO rate_state (id, last_request_ts) VALUES (1, 0)")
        conn.commit()
        conn.close()

    def acquire(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute("PRAGMA busy_timeout=3000")
        try:
            while True:
                row = conn.execute("SELECT last_request_ts FROM rate_state WHERE id = 1").fetchone()
                last_ts = row[0] if row else 0
                now = time.time()
                elapsed = now - last_ts
                if elapsed >= self.min_interval:
                    conn.execute("UPDATE rate_state SET last_request_ts = ? WHERE id = 1", (now,))
                    conn.commit()
                    return
                time.sleep(self.min_interval - elapsed)
        finally:
            conn.close()


def get_rate_limiter() -> SharedRateLimiter:
    """Get the default shared rate limiter."""
    return SharedRateLimiter()
