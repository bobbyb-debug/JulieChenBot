"""
Julie ChenBot - Jokers RSS Engine
=================================

Monitors the JokersUpdates RSS feed and returns only
the newest feed entry.

This module DOES NOT post to Discord.
It simply downloads and parses the feed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import feedparser

from config import RSS_FEED
from services.logger import ProductionLogger

logger = ProductionLogger.get("Jokers")


@dataclass(slots=True)
class FeedUpdate:
    """
    Represents a single Jokers RSS update.
    """

    guid: str
    title: str
    description: str
    link: str
    published: str


class JokersRSS:
    """
    Jokers RSS monitoring engine.
    """

    def __init__(self) -> None:

        self.feed_url = RSS_FEED

        self.last_guid: str | None = None

        logger.info("Jokers RSS engine initialized.")

    # ======================================================
    # Download RSS
    # ======================================================

    def download(self):

        logger.info("Checking Jokers RSS feed...")

        feed = feedparser.parse(self.feed_url)

        if feed.bozo:
            logger.warning(
                "RSS parser reported a problem."
            )

        return feed

    # ======================================================
    # Latest Entry
    # ======================================================

    def latest(self) -> Optional[FeedUpdate]:

        feed = self.download()

        if not feed.entries:

            logger.warning(
                "RSS feed returned no entries."
            )

            return None

        entry = feed.entries[0]

        guid = getattr(entry, "id", "")
        title = getattr(entry, "title", "")
        description = getattr(entry, "description", "")
        link = getattr(entry, "link", "")
        published = getattr(entry, "published", "")

        return FeedUpdate(
            guid=guid,
            title=title,
            description=description,
            link=link,
            published=published,
        )

    # ======================================================
    # New Update?
    # ======================================================

    def check(self) -> Optional[FeedUpdate]:
        """
        Returns ONLY newly discovered updates.

        First run:
            Saves current GUID.
            Returns None.

        Future runs:
            Returns FeedUpdate only if new.
        """

        latest = self.latest()

        if latest is None:
            return None

        # First launch

        if self.last_guid is None:

            self.last_guid = latest.guid

            logger.info(
                "Initial RSS snapshot stored."
            )

            return None

        # Already seen

        if latest.guid == self.last_guid:

            logger.info(
                "No new Jokers updates."
            )

            return None

        # New update

        self.last_guid = latest.guid

        logger.info(
            "NEW Jokers update detected!"
        )

        return latest

    # ======================================================
    # Force Current
    # ======================================================

    def current(self) -> Optional[FeedUpdate]:
        """
        Returns latest item without comparing GUIDs.
        Useful for testing.
        """

        return self.latest()