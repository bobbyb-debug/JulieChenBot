"""
Julie ChenBot Production Engine
===============================

Coordinates Julie ChenBot's production monitoring pipeline.

The engine owns runtime state and delegates monitoring to the
ProductionWatcher. Monitor-specific interpretation remains inside the
individual monitor classes.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
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
from production.monitors import (
    MonitorResult,
    MonitorStatus,
)
from production.watcher import ProductionWatcher
from services.logger import ProductionLogger


class ProductionEngine:
    """
    Coordinates Julie ChenBot's production systems.

    The engine runs registered monitors, queues their events, forwards
    events to the announcer, and persists storage changes. It contains
    no monitor-specific business logic.
    """

    def __init__(
        self,
        storage: Optional[Storage] = None,
    ) -> None:

        self.logger = ProductionLogger.get("Engine")

        self.storage = storage or Storage()

        self.watcher = ProductionWatcher(
            storage=self.storage,
        )

        self.announcer = ProductionAnnouncer()

        self.started_at = datetime.now(UTC)
        self.running = False
        self.tick_count = 0
        self.error_count = 0
        self.last_error: Optional[str] = None
        self.last_tick_at: Optional[datetime] = None

        self.last_results: list[MonitorResult] = []

        self.pending_events: deque[ProductionEvent] = deque()

        self.logger.info(
            "Production Engine initialized."
        )

    # =====================================================
    # Runtime
    # =====================================================

    @property
    def uptime(self) -> timedelta:
        """Returns how long the engine has been running."""

        return datetime.now(UTC) - self.started_at

    @property
    def monitor_count(self) -> int:
        """Returns the number of registered monitors."""

        return self.watcher.total_monitors

    @property
    def healthy_monitor_count(self) -> int:
        """Returns the number of healthy monitors in the last cycle."""

        return sum(
            result.status == MonitorStatus.HEALTHY
            for result in self.last_results
        )

    @property
    def pending_event_count(self) -> int:
        """Returns the number of queued production events."""

        return len(self.pending_events)

    # =====================================================
    # Helpers
    # =====================================================

    @staticmethod
    def _iso(
        value: Optional[datetime],
    ) -> Optional[str]:
        """Returns an ISO timestamp when a value is available."""

        if value is None:
            return None

        return value.isoformat()

    @staticmethod
    def _format_uptime(
        uptime: timedelta,
    ) -> str:
        """Formats an uptime duration for display."""

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

        ProductionWatcher returns both monitor results and the events
        collected from those results. The engine queues and announces
        events without interpreting their contents.
        """

        self.running = True
        self.last_tick_at = datetime.now(UTC)

        try:
            results, events = await self.watcher.run()

            self.last_results = results
            self.pending_events.extend(events)

            await self.process_events()
            await self.announce()
            await self.save_state()

            self.tick_count += 1
            self.last_error = None

            self.logger.info(
                "Production cycle completed: %d monitor(s), %d event(s).",
                len(results),
                len(events),
            )

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
        Records queued events before announcement.

        Event interpretation belongs to monitors and publishing belongs
        to ProductionAnnouncer, so this stage only coordinates the flow.
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
        Announces queued events in order.

        An event is removed only after a successful announcement. A
        failed announcement remains at the front of the queue for a
        future cycle.
        """

        while self.pending_events:
            event = self.pending_events.popleft()

            try:
                await self.announcer.announce(event)
                event.mark_announced()

            except Exception:
                self.pending_events.appendleft(event)

                self.logger.exception(
                    "Announcement failed."
                )

                break

    # =====================================================
    # Persistence
    # =====================================================

    async def save_state(self) -> None:
        """
        Persists the storage state used by monitors.

        Storage writes individual updates immediately. Saving here makes
        the end-of-cycle persistence boundary explicit without inventing
        a second runtime-state schema.
        """

        self.storage.save()

    # =====================================================
    # Health Reporting
    # =====================================================

    def health(self) -> dict:
        """Returns the current production runtime health."""

        return {
            "status": (
                "healthy"
                if self.last_error is None
                else "degraded"
            ),
            "running": self.running,
            "started_at": self._iso(self.started_at),
            "uptime_seconds": round(
                self.uptime.total_seconds(),
                1,
            ),
            "uptime": self._format_uptime(self.uptime),
            "tick_count": self.tick_count,
            "last_tick_at": self._iso(self.last_tick_at),
            "monitor_count": self.monitor_count,
            "healthy_monitors": self.healthy_monitor_count,
            "pending_events": self.pending_event_count,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "monitors": [
                {
                    "name": result.monitor,
                    "status": result.status.value,
                    "changed": result.changed,
                    "detail": result.detail,
                    "duration_ms": result.duration_ms,
                    "events": result.event_count,
                }
                for result in self.last_results
            ],
        }

    # =====================================================
    # Information
    # =====================================================

    def info(self) -> dict:
        """Returns descriptive information about Julie ChenBot."""

        return {
            "name": BOT_NAME,
            "version": VERSION,
            "phase": PHASE,
            "build": BUILD,
            "started_at": self._iso(self.started_at),
            "uptime": self._format_uptime(self.uptime),
            "watcher": {
                "registered_monitors": self.monitor_count,
                "healthy_monitors": self.healthy_monitor_count,
            },
        }

    # =====================================================
    # Shutdown
    # =====================================================

    async def shutdown(self) -> None:
        """Stops the engine and persists monitor storage state."""

        self.running = False

        await self.save_state()

        self.logger.info(
            "Production Engine stopped."
        )

    # =====================================================
    # Debug Representation
    # =====================================================

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"ticks={self.tick_count}, "
            f"monitors={self.monitor_count}, "
            f"queued_events={self.pending_event_count}, "
            f"errors={self.error_count})"
        )
