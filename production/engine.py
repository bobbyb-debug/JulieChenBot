"""
Julie ChenBot Production Engine
===============================

The Production Engine is Julie ChenBot's central orchestrator.

It does not perform production monitoring itself.

Instead, it coordinates every monitoring subsystem,
collects ProductionEvents, and forwards meaningful
events to the announcement pipeline.

The Engine intentionally knows nothing about RSS,
images, competitions, or house state. Those concerns
belong to registered monitors executed by the
ProductionWatcher.

ProductionEngine Responsibilities
---------------------------------
• Run one production cycle
• Collect monitor results
• Queue production events
• Coordinate announcements
• Persist runtime state
• Report health information
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Optional

from config import (
    BOT_NAME,
    BUILD,
    PHASE,
    VERSION,
)

from database.storage import Storage

from production.announcer import ProductionAnnouncer
from production.events import ProductionEvent
from production.monitors import MonitorResult
from production.watcher import ProductionWatcher

from services.logger import ProductionLogger


class ProductionEngine:
    """
    Julie ChenBot's production orchestrator.

    The engine owns runtime state while delegating
    all production monitoring to ProductionWatcher.

    Monitors report ProductionEvents which are queued,
    processed, announced, and eventually persisted.
    """

    def __init__(
        self,
        storage: Optional[Storage] = None,
    ) -> None:

        self.logger = ProductionLogger.get("Engine")

        #
        # Persistence
        #

        self.storage = storage or Storage()

        #
        # Core services
        #

        self.watcher = ProductionWatcher(
            storage=self.storage,
        )

        self.announcer = ProductionAnnouncer()

        #
        # Runtime
        #

        self.started_at = datetime.utcnow()

        self.running = False

        self.tick_count = 0

        self.error_count = 0

        self.last_error: Optional[str] = None

        self.last_tick_at: Optional[datetime] = None

        #
        # Monitor results
        #

        self.last_results: list[MonitorResult] = []

        #
        # Production event queue
        #

        self.pending_events: deque[
            ProductionEvent
        ] = deque()

        self.logger.info(
            "Production Engine initialized."
        )

    # =====================================================
    # Runtime Properties
    # =====================================================

    @property
    def uptime(self) -> timedelta:
        """
        Returns the amount of time the engine
        has been running.
        """

        return (
            datetime.utcnow()
            - self.started_at
        )

    @property
    def pending_event_count(self) -> int:
        """
        Number of queued production events.
        """

        return len(
            self.pending_events
        )

    @property
    def monitor_count(self) -> int:
        """
        Number of registered monitors.
        """

        return len(
            self.watcher.monitors
        )

    @property
    def healthy_monitor_count(self) -> int:
        """
        Number of healthy monitors from the
        previous production cycle.
        """

        return sum(

            1

            for result in self.last_results

            if result.status.value == "healthy"

        )

    # =====================================================
    # Helpers
    # =====================================================

    @staticmethod
    def _iso(
        value: Optional[datetime],
    ) -> Optional[str]:

        return (
            value.isoformat()
            if value
            else None
        )

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
        # =====================================================
    # Production Cycle
    # =====================================================

    async def tick(self) -> None:
        """
        Executes one complete production cycle.

        The Engine delegates monitoring to the
        ProductionWatcher, queues any resulting
        ProductionEvents, processes them,
        announces them, and persists runtime state.
        """

        self.tick_count += 1
        self.last_tick_at = datetime.utcnow()

        try:

            #
            # Execute every registered monitor
            #

            results = await self.watcher.run()

            self.last_results = results

            #
            # Collect newly generated events
            #

            for result in results:

                if result.events:

                    self.pending_events.extend(
                        result.events
                    )

            #
            # Pipeline
            #

            await self.process_events()

            await self.announce()

            await self.save_state()

            self.last_error = None

        except Exception as exc:

            self.error_count += 1

            self.last_error = str(exc)

            self.logger.exception(
                "Production cycle failed."
            )

    # =====================================================
    # Event Processing
    # =====================================================

    async def process_events(self) -> None:
        """
        Processes queued production events.

        At present this stage performs logging only.

        Future phases will enrich events with
        timeline information, AI summaries,
        statistics, and persistence.
        """

        if not self.pending_events:

            return

        self.logger.info(

            "Processing %d production event(s).",

            self.pending_event_count,

        )

        for event in self.pending_events:

            self.logger.info(

                "[%s] %s",

                event.source,

                event.title,

            )

    # =====================================================
    # Announcement Pipeline
    # =====================================================

    async def announce(self) -> None:
        """
        Announces every queued production event.

        Events remain queued until successfully
        announced.
        """

        while self.pending_events:

            event = self.pending_events.popleft()

            try:

                await self.announcer.announce(
                    event
                )

                event.mark_announced()

            except Exception:

                #
                # Put the event back.
                #

                self.pending_events.appendleft(
                    event
                )

                self.logger.exception(

                    "Announcement failed."

                )

                break

    # =====================================================
    # Persistence
    # =====================================================

    async def save_state(self) -> None:
        """
        Persists runtime state.

        Storage already owns persistence.
        Future phases may extend this to save
        timelines, monitor history, AI summaries,
        and queued events.
        """

        #
        # Storage setters automatically persist.
        #

        return
        # =====================================================
    # Health Reporting
    # =====================================================

    def health(self) -> dict:
        """
        Returns a snapshot of the Production Engine's
        current runtime health.
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

            "uptime": self._format_uptime(
                self.uptime
            ),

            "tick_count": self.tick_count,

            "last_tick_at": self._iso(
                self.last_tick_at
            ),

            "monitor_count": self.monitor_count,

            "healthy_monitors": (
                self.healthy_monitor_count
            ),

            "pending_events": (
                self.pending_event_count
            ),

            "error_count": self.error_count,

            "last_error": self.last_error,

            "monitors": [

                {

                    "name": result.monitor,

                    "status": result.status.value,

                    "changed": result.changed,

                    "detail": result.detail,

                    "duration_ms": round(
                        result.duration_ms,
                        2,
                    ),

                }

                for result in self.last_results

            ],

        }

    # =====================================================
    # About Julie
    # =====================================================

    def info(self) -> dict:
        """
        Returns descriptive information about
        Julie ChenBot.
        """

        return {

            "name": BOT_NAME,

            "version": VERSION,

            "phase": PHASE,

            "build": BUILD,

            "uptime": self._format_uptime(
                self.uptime
            ),

            "started_at": self._iso(
                self.started_at
            ),

        }

    # =====================================================
    # Shutdown
    # =====================================================

    async def shutdown(self) -> None:
        """
        Gracefully shuts down the Production Engine.
        """

        self.running = False

        self.logger.info(
            "Production Engine shutting down."
        )

        await self.save_state()

    # =====================================================
    # Debug
    # =====================================================

    def __repr__(self) -> str:

        return (

            f"{self.__class__.__name__}("

            f"ticks={self.tick_count}, "

            f"monitors={self.monitor_count}, "

            f"queued_events={self.pending_event_count})"

        )