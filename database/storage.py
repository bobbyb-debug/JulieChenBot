"""
Julie ChenBot Storage
=====================

Persistent JSON storage for Julie ChenBot.

Stores production state between restarts.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any

from config import DATA


class Storage:
    FILE = DATA / "storage.json"

    DEFAULT_DATA = {
        "last_guid": "",
        "last_title": "",
        "last_published": "",
        "feed_state": "UNKNOWN",
        "last_image_hash": "",
        "hamsterwatch_last_hash": "",
        "last_event": {},
        # Durable queue of not-yet-fully-delivered ProductionEvents
        # (see production/events.py ProductionEvent.to_dict()), so a
        # process restart can resume delivery instead of losing
        # events that were queued or partially delivered when the
        # process stopped. Written/read as generic JSON via get()/
        # set() -- this key carries no Discord-specific persistence
        # logic of its own (see ProductionEngine._persist_pending_events
        # in production/engine.py).
        "pending_events": [],
        "statistics": {
            "rss_updates": 0,
            "announcements": 0,
            "feed_interruptions": 0,
            "bot_starts": 0,
        },
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.FILE.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if not self.FILE.exists():
            self._data = dict(self.DEFAULT_DATA)
            self.save()
            return
        try:
            with self.FILE.open("r", encoding="utf-8") as f:
                self._data = json.load(f)
        except Exception:
            self._data = dict(self.DEFAULT_DATA)
            self.save()
        self._merge_defaults()

    def save(self) -> None:
        """Writes storage.json atomically.

        Writing directly to self.FILE would leave a corruption window
        if the process is interrupted mid-write (Railway restart, OOM
        kill, crash). Instead: write the complete new contents to a
        temp file in the same directory, flush and fsync it, then
        atomically swap it in with os.replace() -- which is atomic on
        both POSIX and Windows (Windows: MoveFileExW with
        MOVEFILE_REPLACE_EXISTING; os.rename() alone would raise on
        Windows if the destination already exists). If anything fails
        before the replace, the existing storage.json is never opened
        for writing at all, so it is left exactly as it was.
        """

        with self._lock:

            payload = json.dumps(
                self._data,
                indent=4,
                ensure_ascii=False,
                sort_keys=True,
            )

            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.FILE.parent),
                prefix=f".{self.FILE.name}.",
                suffix=".tmp",
            )

            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())

                os.replace(tmp_name, self.FILE)

            except Exception:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
                raise

    def _merge_defaults(self) -> None:
        changed = False
        for key, value in self.DEFAULT_DATA.items():
            if key not in self._data:
                self._data[key] = value
                changed = True
        if changed:
            self.save()

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value
        self.save()

    @property
    def last_guid(self) -> str:
        return self.get("last_guid", "")

    @last_guid.setter
    def last_guid(self, value: str) -> None:
        self.set("last_guid", value)

    @property
    def last_title(self) -> str:
        return self.get("last_title", "")

    @last_title.setter
    def last_title(self, value: str) -> None:
        self.set("last_title", value)

    @property
    def last_published(self) -> str:
        return self.get("last_published", "")

    @last_published.setter
    def last_published(self, value: str) -> None:
        self.set("last_published", value)

    @property
    def last_image_hash(self) -> str:
        return self.get("last_image_hash", "")

    @last_image_hash.setter
    def last_image_hash(self, value: str) -> None:
        self.set("last_image_hash", value)

    @property
    def feed_state(self) -> str:
        return self.get("feed_state", "UNKNOWN")

    @feed_state.setter
    def feed_state(self, value: str) -> None:
        self.set("feed_state", value)

    def increment(self, key: str) -> int:
        stats = self.get("statistics", {})
        stats[key] = stats.get(key, 0) + 1
        self.set("statistics", stats)
        return stats[key]

    def statistic(self, key: str) -> int:
        return self.get("statistics", {}).get(key, 0)

    def reset(self) -> None:
        self._data = dict(self.DEFAULT_DATA)
        self.save()
