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


DEFAULT_SEEN_PATH = os.path.expanduser("~/.reddit/seen.json")
SEEN_CAP = 5000  # ids kept per store name (most recent kept)


class SeenStore:
    """Persistent per-name sets of already-emitted item ids (fullnames).

    Powers `--seen NAME`: recurring/scheduled runs only emit items they have
    not reported before. Plain JSON read/write — concurrent cron runs may
    rarely double-report, which is acceptable for a monitoring aid.
    """

    def __init__(self, path: str = DEFAULT_SEEN_PATH):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        # Atomic replace: a crash mid-write must not wipe every store
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)

    def filter_new(self, name: str, items: list) -> list:
        seen = set(self._load().get(name, []))
        return [i for i in items if (i.get("name") or i.get("id")) not in seen]

    def record(self, name: str, items: list) -> None:
        """Record ids; re-recording an id refreshes its recency, so items that
        stay visible across runs don't age past the cap and re-emit as new."""
        ids = [i.get("name") or i.get("id") for i in items]
        ids = [i for i in ids if i]
        if not ids:
            return
        data = self._load()
        merged = data.get(name, []) + ids
        # De-dup keeping the LAST occurrence (freshest position), then cap
        deduped = list(dict.fromkeys(reversed(merged)))
        deduped.reverse()
        data[name] = deduped[-SEEN_CAP:]
        self._save(data)

    def names(self) -> dict:
        return {k: len(v) for k, v in self._load().items()}

    def clear(self, name: str = None) -> int:
        data = self._load()
        if name is None:
            count = len(data)
            self._save({})
            return count
        if name in data:
            del data[name]
            self._save(data)
            return 1
        return 0
