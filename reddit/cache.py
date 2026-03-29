"""File-based response cache for Reddit API calls."""

import hashlib
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = os.path.expanduser("~/.reddit_cache")
DEFAULT_TTL = 1800  # 30 minutes (Reddit content moves fast)


class RedditCache:
    """Simple file-based cache for API responses."""

    def __init__(self, cache_dir: str = DEFAULT_CACHE_DIR, ttl: int = DEFAULT_TTL):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl

    def _key(self, url: str, params: dict = None) -> str:
        raw = url + (json.dumps(params, sort_keys=True) if params else "")
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, url: str, params: dict = None):
        path = self.cache_dir / f"{self._key(url, params)}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            if time.time() - data.get("_ts", 0) > self.ttl:
                path.unlink(missing_ok=True)
                return None
            return data.get("payload")
        except (json.JSONDecodeError, KeyError):
            path.unlink(missing_ok=True)
            return None

    def set(self, url: str, params: dict, payload):
        path = self.cache_dir / f"{self._key(url, params)}.json"
        path.write_text(json.dumps({"_ts": time.time(), "payload": payload}))

    def clear(self) -> int:
        count = 0
        for f in self.cache_dir.glob("*.json"):
            f.unlink()
            count += 1
        return count
