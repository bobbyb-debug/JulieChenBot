"""
Julie ChenBot Production Tracker
================================

Tracks which production events have already been processed.

Responsible for:
    • Preventing duplicate RSS announcements
    • Remembering image changes
    • Remembering the last production event
"""

from __future__ import annotations

from production.events import ProductionEvent
from database.storage import Storage
from services.logger import ProductionLogger


class ProductionTracker:
    """
    Tracks processed production events.
    """

    def __init__(self) -> None:

        self.logger = ProductionLogger.get("Tracker")

        self.storage = Storage()

    # ==========================================================
    # RSS Tracking
    # ==========================================================

    def has_seen_guid(
        self,
        guid: str,
    ) -> bool:

        if not guid:
            return False

        return self.storage.last_guid == guid

    def remember_rss(
        self,
        event: ProductionEvent,
    ) -> None:

        self.storage.last_guid = event.guid
        self.storage.last_title = event.title

        published = event.metadata.get(
            "published",
            "",
        )

        self.storage.last_published = published

        self.logger.info(
            "Stored RSS GUID: %s",
            event.guid,
        )

    # ==========================================================
    # Image Tracking
    # ==========================================================

    @property
    def last_image_hash(self) -> str:

        return self.storage.get(
            "last_image_hash",
            "",
        )

    @last_image_hash.setter
    def last_image_hash(
        self,
        value: str,
    ) -> None:

        self.storage.set(
            "last_image_hash",
            value,
        )

    # ==========================================================
    # Feed State
    # ==========================================================

    @property
    def feed_state(self) -> str:

        return self.storage.get(
            "feed_state",
            "UNKNOWN",
        )

    @feed_state.setter
    def feed_state(
        self,
        value: str,
    ) -> None:

        self.storage.set(
            "feed_state",
            value,
        )

    # ==========================================================
    # Last Event
    # ==========================================================

    def remember_event(
        self,
        event: ProductionEvent,
    ) -> None:

        self.storage.set(
            "last_event",
            event.to_dict(),
        )

        self.logger.info(
            "Stored production event: %s",
            event.title,
        )

    def last_event(self) -> ProductionEvent | None:

        data = self.storage.get(
            "last_event",
        )

        if not data:
            return None

        try:

            return ProductionEvent.from_dict(
                data
            )

        except Exception:

            self.logger.exception(
                "Failed loading last production event."
            )

            return None

    # ==========================================================
    # Duplicate Detection
    # ==========================================================

    def is_duplicate(
        self,
        event: ProductionEvent,
    ) -> bool:

        if event.guid:

            return self.has_seen_guid(
                event.guid
            )

        last = self.last_event()

        if last is None:
            return False

        return (

            last.event_type == event.event_type

            and

            last.title == event.title

            and

            last.message == event.message

        )

    # ==========================================================
    # Record Event
    # ==========================================================

    def record(
        self,
        event: ProductionEvent,
    ) -> None:

        if event.guid:

            self.remember_rss(
                event
            )

        self.remember_event(
            event
        )

    # ==========================================================
    # Reset
    # ==========================================================

    def reset(self) -> None:

        self.storage.set(
            "last_guid",
            "",
        )

        self.storage.set(
            "last_title",
            "",
        )

        self.storage.set(
            "last_published",
            "",
        )

        self.storage.set(
            "last_image_hash",
            "",
        )

        self.storage.set(
            "feed_state",
            "UNKNOWN",
        )

        self.storage.set(
            "last_event",
            {},
        )

        self.logger.info(
            "Production tracker reset."
        )