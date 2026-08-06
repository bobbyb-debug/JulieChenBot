"""
Julie ChenBot RSS Engine
========================

Responsible for downloading and parsing the JokersUpdates RSS feed.

This module owns:

• FeedUpdate
• JokersRSS

It does NOT communicate with Discord or the Production Engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import feedparser

from config import RSS_FEED
from database.storage import Storage
from services.logger import ProductionLogger

logger = ProductionLogger.get("RSS")


# ======================================================
# Feed Update
# ======================================================


@dataclass(slots=True)
class FeedUpdate:
    """
    Represents a single JokersUpdates RSS item.
    """

    guid: str
    title: str
    description: str
    link: str
    published: str


# ======================================================
# RSS Engine
# ======================================================


class JokersRSS:
    """
    Downloads and monitors the JokersUpdates RSS feed.

    State is persisted using Storage so Julie remembers the
    latest feed item even after restarting.
    """

    def __init__(
        self,
        storage: Optional[Storage] = None,
    ) -> None:

        self.feed_url = RSS_FEED
        self.storage = storage or Storage()

        logger.info(
            "Jokers RSS engine initialized."
        )

    # ======================================================
    # Download RSS
    # ======================================================

    def download(self):

        logger.info(
            "Checking Jokers RSS feed..."
        )

        feed = feedparser.parse(
            self.feed_url
        )

        if feed.bozo:

            logger.warning(
                "RSS parser reported a problem."
            )

        return feed

    # ======================================================
    # Latest Feed Entry
    # ======================================================

    def latest(self) -> Optional[FeedUpdate]:

        feed = self.download()

        if not feed.entries:

            logger.warning(
                "RSS feed returned no entries."
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

    # ======================================================
    # New Update?
    # ======================================================

    def check(self) -> Optional[FeedUpdate]:
        """
        Returns ONLY newly discovered feed updates.

        First run:
            Stores the current RSS item and returns None.

        Future runs:
            Returns a FeedUpdate only if the GUID has changed.
        """

        latest = self.latest()

        if latest is None:
            return None

        last_guid = self.storage.last_guid

        #
        # First launch
        #

        if not last_guid:

            logger.info(
                "Creating first RSS snapshot."
            )

            self._remember(
                latest
            )

            return None

        #
        # Already seen
        #

        if latest.guid == last_guid:

            logger.info(
                "No new Jokers updates."
            )

            return None

        #
        # New feed item
        #

        logger.info(
            "NEW Jokers update detected!"
        )

        self._remember(
            latest
        )

        return latest

    # ======================================================
    # Current Feed Item
    # ======================================================

    def current(self) -> Optional[FeedUpdate]:
        """
        Returns the current RSS item without comparing
        against stored state.
        """

        return self.latest()

    # ======================================================
    # Persistence
    # ======================================================

    def _remember(
        self,
        update: FeedUpdate,
    ) -> None:
        """
        Persists the supplied RSS item.
        """

        self.storage.last_guid = update.guid
        self.storage.last_title = update.title
        self.storage.last_published = update.published