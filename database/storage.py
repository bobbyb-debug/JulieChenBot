"""
Julie ChenBot Storage
=====================

Persistent JSON storage for Julie ChenBot.

Stores production state between restarts.

Examples
--------
storage = Storage()

storage.set("last_guid", guid)

guid = storage.get("last_guid")

storage.last_guid = guid
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class Storage:

    FILE = Path("data/storage.json")

    DEFAULT_DATA = {

        # RSS
        "last_guid": "",
        "last_title": "",
        "last_published": "",

        # Feed status
        "feed_state": "UNKNOWN",

        # Image watcher
        "last_image_hash": "",

        # Last production event
        "last_event": {},

        # Statistics
        "statistics": {

            "rss_updates": 0,

            "announcements": 0,

            "feed_interruptions": 0,

            "bot_starts": 0,

        },
    }

    def __init__(self) -> None:

        self._lock = threading.Lock()

        self.FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._data: dict[str, Any] = {}

        self.load()

    # ======================================================
    # Load
    # ======================================================

    def load(self) -> None:

        if not self.FILE.exists():

            self._data = dict(
                self.DEFAULT_DATA
            )

            self.save()

            return

        try:

            with self.FILE.open(
                "r",
                encoding="utf-8",
            ) as f:

                self._data = json.load(
                    f
                )

        except Exception:

            self._data = dict(
                self.DEFAULT_DATA
            )

            self.save()

        # Ensure any new keys exist
        self._merge_defaults()

    # ======================================================
    # Save
    # ======================================================

    def save(self) -> None:

        with self._lock:

            with self.FILE.open(
                "w",
                encoding="utf-8",
            ) as f:

                json.dump(

                    self._data,

                    f,

                    indent=4,

                    ensure_ascii=False,

                    sort_keys=True,

                )

    # ======================================================
    # Defaults
    # ======================================================

    def _merge_defaults(self) -> None:

        changed = False

        for key, value in self.DEFAULT_DATA.items():

            if key not in self._data:

                self._data[key] = value

                changed = True

        if changed:

            self.save()

    # ======================================================
    # Generic
    # ======================================================

    def get(
        self,
        key: str,
        default=None,
    ):

        return self._data.get(
            key,
            default,
        )

    def set(
        self,
        key: str,
        value,
    ) -> None:

        self._data[key] = value

        self.save()

    # ======================================================
    # Convenience Properties
    # ======================================================

    @property
    def last_guid(self) -> str:

        return self.get(
            "last_guid",
            "",
        )

    @last_guid.setter
    def last_guid(
        self,
        value: str,
    ) -> None:

        self.set(
            "last_guid",
            value,
        )

    @property
    def last_title(self) -> str:

        return self.get(
            "last_title",
            "",
        )

    @last_title.setter
    def last_title(
        self,
        value: str,
    ) -> None:

        self.set(
            "last_title",
            value,
        )

    @property
    def last_published(self) -> str:

        return self.get(
            "last_published",
            "",
        )

    @last_published.setter
    def last_published(
        self,
        value: str,
    ) -> None:

        self.set(
            "last_published",
            value,
        )

    @property
    def last_image_hash(self) -> str:

        return self.get(
            "last_image_hash",
            "",
        )

    @last_image_hash.setter
    def last_image_hash(
        self,
        value: str,
    ) -> None:

        self.set(
            "last_image_hash",
            value,
        )

    @property
    def feed_state(self) -> str:

        return self.get(
            "feed_state",
            "UNKNOWN",
        )

    @feed_state.setter
    def feed_state(
        self,
        value: str,
    ) -> None:

        self.set(
            "feed_state",
            value,
        )

    # ======================================================
    # Statistics
    # ======================================================

    def increment(
        self,
        key: str,
    ) -> int:

        stats = self.get(
            "statistics",
            {},
        )

        stats[key] = stats.get(
            key,
            0,
        ) + 1

        self.set(
            "statistics",
            stats,
        )

        return stats[key]

    def statistic(
        self,
        key: str,
    ) -> int:

        return self.get(
            "statistics",
            {},
        ).get(
            key,
            0,
        )

    # ======================================================
    # Reset
    # ======================================================

    def reset(self) -> None:

        self._data = dict(
            self.DEFAULT_DATA
        )

        self.save()