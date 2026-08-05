"""
Julie ChenBot - Jokers RSS Engine
=================================

Downloads the JokersUpdates RSS feed and returns only
updates Julie has never announced before.

Julie remembers the last RSS item even after restarting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import feedparser

from config import RSS_FEED
from database.storage import Storage
from services.logger import ProductionLogger

logger = ProductionLogger.get("Jokers")


@dataclass(slots=True)
class FeedUpdate:
    """
    Represents one Jokers RSS update.
    """

    guid: str
    title: str
    description: str
    link: str
    published: str


class JokersRSS:
    """
    Jokers RSS Engine
    """

    def __init__(self) -> None:

        self.feed_url = RSS_FEED

        self.db = Storage()

        logger.info("Jokers RSS engine initialized.")

    # ==========================================================
    # Download Feed
    # ==========================================================

    def download(self):

        logger.info("Checking Jokers RSS...")

        feed = feedparser.parse(self.feed_url)

        if feed.bozo:
            logger.warning(
                "RSS parser reported warnings."
            )

        return feed

    # ==========================================================
    # Current Feed Entry
    # ==========================================================

    def latest(self) -> Optional[FeedUpdate]:

        feed = self.download()

        if not feed.entries:

            logger.warning(
                "RSS returned zero entries."
            )

            return None

        entry = feed.entries[0]

        return FeedUpdate(
            guid=getattr(entry, "id", ""),
            title=getattr(entry, "title", ""),
            description=getattr(entry, "description", ""),
            link=getattr(entry, "link", ""),
            published=getattr(entry, "published", ""),
        )

    # ==========================================================
    # Has Anything Changed?
    # ==========================================================

    def check(self) -> Optional[FeedUpdate]:

        latest = self.latest()

        if latest is None:
            return None

        # First launch

        if not self.db.last_guid:

            logger.info(
                "Creating first RSS snapshot."
            )

            self.db.last_guid = latest.guid
            self.db.last_title = latest.title
            self.db.last_published = latest.published

            return None

        # No change

        if latest.guid == self.db.last_guid:

            logger.info(
                "No new Jokers updates."
            )

            return None

        # New update

        logger.info(
            "New Jokers update detected."
        )

        self.db.last_guid = latest.guid
        self.db.last_title = latest.title
        self.db.last_published = latest.published

        return latest

    # ==========================================================
    # Force Latest
    # ==========================================================

    def current(self) -> Optional[FeedUpdate]:
        """
        Always return the newest RSS item,
        regardless of whether Julie has seen it.
        """

        return self.latest()