"""Standing research topics: a named sweep (subreddits + optional query) with
delta tracking and a research folder that accumulates each update."""

import json
import os
from pathlib import Path

DEFAULT_TOPICS_PATH = os.path.expanduser("~/.reddit/topics.json")


class TopicStore:
    """Plain-JSON registry of topic configs (atomic writes, like SeenStore)."""

    def __init__(self, path: str = DEFAULT_TOPICS_PATH):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, name: str):
        return self._load().get(name)

    def all(self) -> dict:
        return self._load()

    def set(self, name: str, config: dict) -> None:
        data = self._load()
        data[name] = config
        self._save(data)

    def remove(self, name: str) -> bool:
        data = self._load()
        if name not in data:
            return False
        del data[name]
        self._save(data)
        return True
