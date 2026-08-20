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

import re
from dataclasses import dataclass
from typing import Optional
from urllib.request import Request, urlopen

import feedparser

from config import RSS_FEED, RSS_MAX_BACKFILL
from database.storage import Storage
from services.logger import ProductionLogger

logger = ProductionLogger.get("RSS")

_USER_AGENT = "JulieChenBot/1.0"

# feedparser.parse() has no timeout parameter in the installed version
# (verified via inspect.signature on feedparser 6.0.14) and performs
# its own network fetch internally when given a URL, unbounded -- a
# stalled connection can hang indefinitely. This is the same bound
# used by every other Julie monitor's urlopen() call.
_FETCH_TIMEOUT = 20

# Matches a literal <img src="..."> in an item's own description/
# content HTML -- see _extract_image_url() below for why this is
# deliberately as far as detection goes.
_IMG_SRC_PATTERN = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)


def _extract_image_url(entry, description: str) -> str:
    """Finds a direct, hotlinkable image URL for one RSS entry, if the
    feed actually provides one. Returns "" when it doesn't -- this is
    the normal case today (see below), not a bug.

    Checked in order, all of it read from data feedparser/the feed
    already handed us -- no additional network request is made here:

        1. A standard media-RSS/Media RSS media:content or media:
           thumbnail element, or a plain RSS <enclosure>, if
           feedparser populated one on this entry.
        2. A literal <img src="..."> embedded directly in the item's
           own description/content HTML.

    Verified directly against the live Joker's Updates RSS feed and
    the individual post pages it links to: neither currently exposes
    either of these for an (IMG)-tagged item. Joker's Updates embeds
    each image via Imgur's client-side embed widget -- a `<blockquote
    class="imgur-embed-pub" data-id="...">` that a browser's
    JavaScript resolves into a real picture at view time -- which
    never appears as an <img> tag, enclosure, or media element
    anywhere in the feed or the linked page's server-rendered HTML.
    That opaque Imgur ID is deliberately NOT resolved into a real
    file URL here: doing so would mean guessing a URL shape (e.g.
    assuming a specific file extension) or making an additional
    request to Imgur itself just because the text says "(IMG)" --
    neither of which this is allowed to do. An item with no
    extractable image URL is handled exactly like an item with no
    image at all by the rest of the pipeline (see production/
    engine.py _rss_event(), services/discord_output.py) -- the text
    update still posts normally.
    """

    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key) if hasattr(entry, "get") else None
        if media:
            url = media[0].get("url", "")
            if url:
                return url

    enclosures = entry.get("enclosures") if hasattr(entry, "get") else None
    if enclosures:
        for enclosure in enclosures:
            enclosure_type = enclosure.get("type", "")
            url = enclosure.get("href", "") or enclosure.get("url", "")
            if url and (not enclosure_type or enclosure_type.startswith("image/")):
                return url

    match = _IMG_SRC_PATTERN.search(description or "")
    if match:
        return match.group(1)

    return ""


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
    # "" when the feed provides no direct image URL for this item --
    # see _extract_image_url() above for exactly what this does and
    # does not attempt.
    image_url: str = ""


# ======================================================
# RSS Engine
# ======================================================


class JokersRSS:
    """
    Downloads and monitors the JokersUpdates RSS feed.

    State is persisted using Storage so Julie remembers the
    latest feed item even after restarting.
    """

    SEEN_KEY = "seen_guids"
    SEEN_LIMIT = 300

    def __init__(
        self,
        storage: Optional[Storage] = None,
        max_backfill: Optional[int] = None,
    ) -> None:

        self.feed_url = RSS_FEED
        self.storage = storage or Storage()
        self.max_backfill = max_backfill or RSS_MAX_BACKFILL

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

        # Fetch the bytes ourselves with a bounded timeout, then hand
        # feedparser only the already-fetched bytes to parse -- this
        # removes feedparser's own unbounded network fetch entirely
        # (it does parsing only here, no I/O). A fetch failure or
        # timeout degrades to an empty feed rather than raising,
        # matching the "no entries" outcome entries()/latest() already
        # handle for any other empty or malformed feed response.
        try:
            request = Request(
                self.feed_url,
                headers={"User-Agent": _USER_AGENT},
            )
            with urlopen(request, timeout=_FETCH_TIMEOUT) as response:
                raw = response.read()
        except Exception as exc:

            logger.warning(
                "RSS feed fetch failed: %s", exc
            )

            return feedparser.parse(b"")

        feed = feedparser.parse(
            raw
        )

        if feed.bozo:

            logger.warning(
                "RSS parser reported a problem."
            )

        return feed

    # ======================================================
    # Latest Feed Entry
    # ======================================================

    def entries(self) -> list[FeedUpdate]:
        """
        Returns every item currently in the feed, newest first.
        """

        feed = self.download()

        if not feed.entries:

            logger.warning(
                "RSS feed returned no entries."
            )

            return []

        return [
            FeedUpdate(
                guid=getattr(entry, "id", ""),
                title=getattr(entry, "title", ""),
                description=getattr(entry, "description", ""),
                link=getattr(entry, "link", ""),
                published=getattr(entry, "published", ""),
                image_url=_extract_image_url(
                    entry, getattr(entry, "description", "")
                ),
            )
            for entry in feed.entries
        ]

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
            image_url=_extract_image_url(entry, getattr(entry, "description", "")),
        )

    # ======================================================
    # New Update?
    # ======================================================

    def check_all(
        self,
        limit: Optional[int] = None,
    ) -> list[FeedUpdate]:
        """
        Returns every feed item Julie has not announced yet,
        oldest first, so nothing is missed while she is offline.

        Deduplication uses a bounded ledger of recently seen GUIDs
        rather than only the newest GUID. That way an item is never
        announced twice even if last_guid is lost, rewound, or the
        feed briefly reorders.

        First run:
            Records the whole current feed and returns [] so Julie
            does not dump the entire backlog into Discord on a
            fresh install.
        """

        limit = limit or self.max_backfill

        updates = self.entries()

        if not updates:
            return []

        last_guid = self.storage.last_guid
        seen = set(self.storage.get(self.SEEN_KEY, []))

        # First launch: snapshot everything, announce nothing.
        if not last_guid and not seen:

            logger.info(
                "Creating first RSS snapshot (%s item(s) recorded).",
                len(updates),
            )

            self._remember(updates[0])
            self._record_seen(updates)

            return []

        fresh = [
            update
            for update in updates
            if update.guid and update.guid != last_guid
            and update.guid not in seen
        ]

        if not fresh:

            logger.info(
                "No new Jokers updates."
            )

            return []

        # updates arrive newest first; announce oldest first so the
        # channel reads in chronological order.
        fresh.reverse()

        skipped: list[FeedUpdate] = []

        if len(fresh) > limit:

            skipped = fresh[:-limit]
            fresh = fresh[-limit:]

            logger.warning(
                "Catch-up capped at %s item(s); %s older item(s) "
                "marked as seen without announcing.",
                limit,
                len(skipped),
            )

        logger.info(
            "NEW Jokers updates detected: %s item(s).",
            len(fresh),
        )

        # Everything we looked at is now seen, including anything
        # skipped by the cap, so it cannot resurface later.
        self._record_seen(skipped + fresh)
        self._remember(fresh[-1])

        return fresh

    def _record_seen(self, updates: list[FeedUpdate]) -> None:
        """
        Adds GUIDs to the bounded seen ledger.
        """

        seen = list(self.storage.get(self.SEEN_KEY, []))

        for update in updates:
            if update.guid and update.guid not in seen:
                seen.append(update.guid)

        if len(seen) > self.SEEN_LIMIT:
            seen = seen[-self.SEEN_LIMIT:]

        self.storage.set(self.SEEN_KEY, seen)

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