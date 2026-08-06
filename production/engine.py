"""
Julie ChenBot Production Engine
===============================

The Production Engine is the heart of Julie ChenBot.

It coordinates every production monitoring system and serves as the
single orchestrator for Julie's autonomous workflow.

Each production cycle follows the same pipeline:

    check_rss()
    check_image()
    check_house_state()
    process_events()
    announce()
    save_state()

Adding a new monitoring concern should only require implementing a
new method within this class. The overall execution pipeline should
remain unchanged.

The Production Engine does not communicate with Discord directly.
Instead, it discovers production events and queues them for later
processing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from config import (
    BOT_NAME,
    BUILD,
    PHASE,
    VERSION,
)

from database.storage import Storage
from production.rss import FeedUpdate, JokersRSS
from services.logger import ProductionLogger


class ProductionEngine:
    """
    Coordinates Julie ChenBot's production systems.
    """

    def __init__(
        self,
        storage: Optional[Storage] = None,
    ) -> None:

        self.logger = ProductionLogger.get("Engine")

        #
        # Persistent storage
        #

        self.storage = storage or Storage()

        #
        # Monitoring engines
        #

        self.rss = JokersRSS(storage=self.storage)

        #
        # Runtime
        #

        self.started_at = datetime.utcnow()

        self.tick_count = 0
        self.error_count = 0

        self.last_error: Optional[str] = None

        #
        # Monitoring timestamps
        #

        self.last_tick_at: Optional[datetime] = None
        self.last_rss_check_at: Optional[datetime] = None

        #
        # Cached state
        #

        self.last_rss_update: Optional[FeedUpdate] = None

        #
        # Event queue
        #

        self.pending_events: list[FeedUpdate] = []

        self.logger.info(
            "Production Engine initialized."
        )

    # ======================================================
    # Runtime
    # ======================================================

    @property
    def uptime(self) -> timedelta:
        """
        Returns engine uptime.
        """

        return datetime.utcnow() - self.started_at

    # ======================================================
    # Tick
    # ======================================================

    async def tick(self) -> None:
        """
        Executes one production monitoring cycle.
        """

        self.tick_count += 1
        self.last_tick_at = datetime.utcnow()

        try:

            await self.check_rss()
            await self.check_image()
            await self.check_house_state()
            await self.process_events()
            await self.announce()
            await self.save_state()

            self.last_error = None

        except Exception as exc:

            self.error_count += 1
            self.last_error = str(exc)

            self.logger.exception(
                "Production tick failed."
            )

    # ======================================================
    # RSS
    # ======================================================

    async def check_rss(
        self,
    ) -> Optional[FeedUpdate]:
        """
        Checks the Jokers RSS feed.
        """

        self.last_rss_check_at = datetime.utcnow()

        update = self.rss.check()

        if update is None:

            self.logger.info(
                "No new live feed updates."
            )

            return None

        self.last_rss_update = update

        self.pending_events.append(update)

        self.logger.info(
            "NEW LIVE FEED UPDATE"
        )

        self.logger.info(update.title)
        self.logger.info(update.link)

        return update

    # ======================================================
    # Image
    # ======================================================

    async def check_image(self) -> None:
        """
        Checks the House Status image.

        Implemented during Phase 4.
        """

        return

    # ======================================================
    # House State
    # ======================================================

    async def check_house_state(self) -> None:
        """
        Checks the live house state.

        Implemented during Phase 4.
        """

        return

    # ======================================================
    # Events
    # ======================================================

    async def process_events(self) -> None:
        """
        Processes queued production events.
        """

        while self.pending_events:

            event = self.pending_events.pop(0)

            self.logger.info(
                "Queued event processed: %s",
                event.title,
            )

    # ======================================================
    # Announcements
    # ======================================================

    async def announce(self) -> None:
        """
        Publishes announcements.

        Discord integration arrives in a later phase.
        """

        return

    # ======================================================
    # Persistence
    # ======================================================

    async def save_state(self) -> None:
        """
        Persists runtime state.

        RSS persistence already occurs inside rss.py.
        """

        return

    # ======================================================
    # Health
    # ======================================================

    def health(self) -> dict:
        """
        Returns runtime health.
        """

        return {
            "status": (
                "healthy"
                if self.last_error is None
                else "degraded"
            ),
            "started_at": self._iso(
                self.started_at
            ),
            "uptime_seconds": round(
                self.uptime.total_seconds(),
                1,
            ),
            "tick_count": self.tick_count,
            "last_tick_at": self._iso(
                self.last_tick_at
            ),
            "last_rss_check_at": self._iso(
                self.last_rss_check_at
            ),
            "last_known_update": {
                "guid": self.storage.last_guid,
                "title": self.storage.last_title,
                "published": self.storage.last_published,
            },
            "error_count": self.error_count,
            "last_error": self.last_error,
        }

    # ======================================================
    # About
    # ======================================================

    def info(self) -> dict:
        """
        Returns descriptive information about Julie.
        """

        return {
            "name": BOT_NAME,
            "version": VERSION,
            "phase": PHASE,
            "build": BUILD,
            "uptime": self._format_uptime(
                self.uptime
            ),
        }

    # ======================================================
    # Helpers
    # ======================================================

    @staticmethod
    def _iso(
        value: Optional[datetime],
    ) -> Optional[str]:

        if value is None:
            return None

        return value.isoformat()

    @staticmethod
    def _format_uptime(
        uptime: timedelta,
    ) -> str:

        return str(
            timedelta(
                seconds=int(
                    uptime.total_seconds()
                )
            )
        )