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

from config import BOT_NAME, BUILD, PHASE, VERSION
from database.storage import Storage
from production.announcer import ProductionAnnouncer
from production.events import EventSeverity, EventType, ProductionEvent
from production.monitors import MonitorResult, MonitorStatus
from production.parser import ProductionParser
from production.rss import FeedUpdate, JokersRSS
from production.watcher import ProductionWatcher
from services.logger import ProductionLogger


class ProductionEngine:
    """Coordinates Julie ChenBot's production systems."""

    def __init__(self, storage: Optional[Storage] = None) -> None:
        self.logger = ProductionLogger.get("Engine")
        self.storage = storage or Storage()

        self.rss = JokersRSS(storage=self.storage)
        self.parser = ProductionParser()
        self.watcher = ProductionWatcher(storage=self.storage)
        self.announcer = ProductionAnnouncer()

        self.started_at = datetime.now(UTC)
        self.running = False
        self.tick_count = 0
        self.error_count = 0
        self.last_error: Optional[str] = None
        self.last_tick_at: Optional[datetime] = None

        self.last_results: list[MonitorResult] = []
        self.pending_events: deque[ProductionEvent] = deque()

        self.logger.info("Production Engine initialized.")

    @property
    def uptime(self) -> timedelta:
        return datetime.now(UTC) - self.started_at

    @property
    def monitor_count(self) -> int:
        return self.watcher.total_monitors

    @property
    def healthy_monitor_count(self) -> int:
        return sum(
            result.status == MonitorStatus.HEALTHY
            for result in self.last_results
        )

    @property
    def pending_event_count(self) -> int:
        return len(self.pending_events)

    @staticmethod
    def _iso(value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        return value.isoformat()

    @staticmethod
    def _format_uptime(uptime: timedelta) -> str:
        return str(timedelta(seconds=int(uptime.total_seconds())))

    @staticmethod
    def _rss_event(update: FeedUpdate) -> ProductionEvent:
        """Converts one Joker's Updates item into a publishable event."""

        detail = update.title.strip()
        if update.description and update.description.strip():
            detail = update.description.strip()

        return ProductionEvent(
            source="Joker's Updates",
            event_type=EventType.RSS_UPDATE,
            title="LIVE FEED UPDATE",
            detail=detail,
            severity=EventSeverity.INFO,
            metadata={
                "guid": update.guid,
                "link": update.link,
                "published": update.published,
                "rss_title": update.title,
            },
        )

    async def tick(self) -> None:
        """Executes one complete production cycle."""

        self.running = True
        self.last_tick_at = datetime.now(UTC)

        try:
            had_rss_snapshot = bool(self.storage.last_guid)
            rss_update = self.rss.check()

            # On first launch, check() stores the current item and returns
            # None. Read that same current item so Julie can publish an
            # initial live-feed snapshot immediately.
            if rss_update is None and not had_rss_snapshot:
                rss_update = self.rss.current()
                if rss_update is not None:
                    self.logger.info(
                        "RSS initial snapshot loaded: %s",
                        rss_update.title,
                    )

            if rss_update is None:
                self.logger.info("RSS: no new feed item.")
            else:
                self.logger.info(
                    "RSS update detected: %s",
                    rss_update.title,
                )

                # Every newly surfaced RSS item is a production event. The
                # parser may additionally recognize production-state changes,
                # which are fed into the built-in monitors below.
                self.pending_events.append(
                    self._rss_event(rss_update)
                )

                parsed = self.parser.parse(rss_update)

                if parsed.recognized:
                    self.watcher.house_status.update(parsed.house_status)
                    self.watcher.competition.update(parsed.competition)
                    self.logger.info(
                        "RSS production state applied: %s",
                        ", ".join(parsed.fields),
                    )
                else:
                    self.logger.info(
                        "RSS item contained no recognized production state."
                    )

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
            self.logger.exception("Production cycle failed.")

    async def process_events(self) -> None:
        """Records queued events before announcement."""

        if not self.pending_events:
            return

        self.logger.info(
            "Processing %d production event(s).",
            self.pending_event_count,
        )

        for event in self.pending_events:
            self.logger.info("[%s] %s", event.source, event.title)

    async def announce(self) -> None:
        """Announces queued events in order."""

        while self.pending_events:
            event = self.pending_events.popleft()

            try:
                await self.announcer.announce(event)
                event.mark_announced()
            except Exception:
                self.pending_events.appendleft(event)
                self.logger.exception("Announcement failed.")
                break

    async def save_state(self) -> None:
        """Persists the storage state used by monitors."""
        self.storage.save()

    def health(self) -> dict:
        """Returns the current production runtime health."""

        return {
            "status": "healthy" if self.last_error is None else "degraded",
            "running": self.running,
            "started_at": self._iso(self.started_at),
            "uptime_seconds": round(self.uptime.total_seconds(), 1),
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

    async def shutdown(self) -> None:
        """Stops the engine and persists monitor storage state."""

        self.running = False
        await self.save_state()
        self.logger.info("Production Engine stopped.")

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"ticks={self.tick_count}, "
            f"monitors={self.monitor_count}, "
            f"queued_events={self.pending_event_count}, "
            f"errors={self.error_count})"
        )
