"""
Julie ChenBot Production Parser
===============================

Converts raw Jokers RSS entries into ProductionEvents.

Input:
    feedparser entry

Output:
    ProductionEvent
"""

from __future__ import annotations

import html
import re

from production.events import ProductionEvent, rss_event
from services.logger import ProductionLogger


class RSSParser:
    """
    Converts RSS entries into ProductionEvents.
    """

    def __init__(self) -> None:

        self.logger = ProductionLogger.get("Parser")

    # ==========================================================
    # HTML Cleanup
    # ==========================================================

    @staticmethod
    def clean(text: str) -> str:
        """
        Removes HTML and cleans whitespace.
        """

        if not text:
            return ""

        # HTML entities
        text = html.unescape(text)

        # Remove HTML tags
        text = re.sub(
            r"<[^>]+>",
            "",
            text,
        )

        # Collapse whitespace
        text = re.sub(
            r"\s+",
            " ",
            text,
        )

        return text.strip()

    # ==========================================================
    # Parse One Entry
    # ==========================================================

    def parse(self, entry) -> ProductionEvent:
        """
        Convert a feedparser entry into a ProductionEvent.
        """

        guid = getattr(entry, "id", "")

        if not guid:
            guid = getattr(entry, "guid", "")

        title = self.clean(
            getattr(entry, "title", "")
        )

        description = self.clean(
            getattr(entry, "description", "")
        )

        if not description:
            description = self.clean(
                getattr(entry, "summary", "")
            )

        link = getattr(entry, "link", "")

        published = getattr(
            entry,
            "published",
            "",
        )

        self.logger.info(
            "Parsed RSS: %s",
            title,
        )

        event = rss_event(
            title=title,
            message=description,
            guid=guid,
            url=link,
        )

        event.metadata["published"] = published

        return event

    # ==========================================================
    # Parse Feed
    # ==========================================================

    def parse_feed(
        self,
        entries,
    ) -> list[ProductionEvent]:
        """
        Convert every RSS entry into ProductionEvents.
        """

        events = []

        for entry in entries:

            try:

                events.append(
                    self.parse(entry)
                )

            except Exception:

                self.logger.exception(
                    "Failed parsing RSS entry."
                )

        self.logger.info(
            "Parsed %s production event(s).",
            len(events),
        )

        return events

    # ==========================================================
    # Latest Event
    # ==========================================================

    def latest(
        self,
        entries,
    ) -> ProductionEvent | None:
        """
        Parse only the newest RSS entry.
        """

        if not entries:
            return None

        return self.parse(
            entries[0]
        )