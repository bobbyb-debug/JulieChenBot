"""
Julie ChenBot Production RSS
============================

Downloads and validates the JokersUpdates RSS feed.

This module ONLY downloads RSS.

It does not:
    - Parse Big Brother events
    - Announce updates
    - Track duplicates

Those responsibilities belong elsewhere.
"""

from __future__ import annotations

import feedparser

from config import RSS_FEED
from services.logger import ProductionLogger


class RSSService:
    """
    Handles downloading the JokersUpdates RSS feed.
    """

    def __init__(self) -> None:

        self.logger = ProductionLogger.get("RSS")

        self.feed_url = RSS_FEED

    # ==========================================================
    # Download
    # ==========================================================

    def download(self):

        self.logger.info(
            "Downloading Jokers RSS..."
        )

        feed = feedparser.parse(
            self.feed_url
        )

        if feed.bozo:

            self.logger.warning(
                "RSS parser returned warnings."
            )

        return feed

    # ==========================================================
    # Entries
    # ==========================================================

    def entries(self):

        feed = self.download()

        if not hasattr(feed, "entries"):

            self.logger.warning(
                "RSS feed contains no entries."
            )

            return []

        self.logger.info(
            "Downloaded %s RSS entries.",
            len(feed.entries),
        )

        return feed.entries

    # ==========================================================
    # Latest
    # ==========================================================

    def latest(self):

        entries = self.entries()

        if not entries:

            return None

        return entries[0]

    # ==========================================================
    # Health
    # ==========================================================

    def healthy(self) -> bool:

        try:

            feed = self.download()

            return bool(feed.entries)

        except Exception:

            self.logger.exception(
                "RSS health check failed."
            )

            return False