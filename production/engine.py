"""
Julie ChenBot Production Engine
===============================

Coordinates Julie ChenBot's production monitoring pipeline.

The engine owns runtime state and delegates monitoring to the
ProductionWatcher. Monitor-specific interpretation remains inside the
individual monitor classes.
"""

from __future__ import annotations

import asyncio
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

    RECAP_KEY = "recap_buffer"
    RECAP_LIMIT = 100
    PENDING_EVENTS_KEY = "pending_events"

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
        # Seeded from storage rather than starting empty: a previous
        # process instance may have crashed (or been redeployed) after
        # queuing an event -- or after some but not all Discord
        # destinations for it succeeded -- but before finishing
        # delivery. Recovering those here and feeding them through the
        # exact same pending_events queue that every normal tick uses
        # means recovery re-enters the existing process_events()/
        # announce() path rather than a separate one, and
        # DiscordOutputRouter's existing delivered_to skip (see
        # services/discord_output.py) is what actually prevents a
        # destination that already succeeded before the crash from
        # being resent.
        self.pending_events: deque[ProductionEvent] = deque(
            self._load_pending_events()
        )

        self.logger.info("Production Engine initialized.")

    def _load_pending_events(self) -> list[ProductionEvent]:
        """Loads durably-persisted pending events left by a previous run."""

        recovered: list[ProductionEvent] = []
        for data in self.storage.get(self.PENDING_EVENTS_KEY, []):
            try:
                recovered.append(ProductionEvent.from_dict(data))
            except Exception:
                self.logger.warning(
                    "Discarding unrecoverable pending event: %r", data
                )

        if recovered:
            self.logger.info(
                "Recovered %d pending event(s) from previous run.",
                len(recovered),
            )

        return recovered

    def _persist_pending_events(self) -> None:
        """Durably persists the current pending-event queue.

        Called once after process_events() has assembled this tick's
        full pending list (before any Discord attempt) and once after
        announce() finishes its pass (after Discord has been attempted,
        capturing whatever per-destination delivered_to progress
        DiscordOutputRouter.publish() made -- see
        services/discord_output.py). storage.set() persists atomically
        (see database/storage.py Storage.save()), so once this call
        returns, the on-disk state matches self.pending_events exactly.

        This does not make Discord delivery itself transactional: a
        crash between a channel.send() succeeding and this call
        running still loses that specific destination's delivered_to
        update, since event.delivered_to is mutated in memory by
        DiscordOutputRouter before publish() returns. That is an
        unavoidable at-least-once window -- not a bug this persistence
        is meant to close -- because the Discord API call and this
        process's local disk write can never be one atomic operation.
        """

        self.storage.set(
            self.PENDING_EVENTS_KEY,
            [event.to_dict() for event in self.pending_events],
        )

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
            # check_all() is synchronous and performs a real network
            # fetch (see production/rss.py). Running it directly here
            # would block the whole asyncio event loop -- including
            # Discord's heartbeat and every other monitor -- for the
            # duration of that fetch. asyncio.to_thread() runs it on a
            # worker thread instead, so a slow/stalled feed can no
            # longer freeze the bot.
            rss_updates = await asyncio.to_thread(self.rss.check_all)

            # On first launch, check_all() records the feed and returns
            # nothing. Read the current item so Julie can publish an
            # initial live-feed snapshot immediately.
            if not rss_updates and not had_rss_snapshot:
                initial = await asyncio.to_thread(self.rss.current)
                if initial is not None:
                    rss_updates = [initial]
                    self.logger.info(
                        "RSS initial snapshot loaded: %s",
                        initial.title,
                    )

            if not rss_updates:
                self.logger.info("RSS: no new feed item.")
            else:
                self.logger.info(
                    "RSS: %s update(s) to announce.",
                    len(rss_updates),
                )

            # Oldest first, so the channel reads chronologically.
            for rss_update in rss_updates:
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

        # Durably records this tick's full pending list -- including
        # events not yet attempted -- before any Discord call is made,
        # so a crash before announce() runs still leaves them
        # recoverable on restart rather than lost with the process.
        self._persist_pending_events()

    async def announce(self) -> None:
        """Announces queued events in order."""

        try:
            while self.pending_events:
                event = self.pending_events.popleft()

                try:
                    await self.announcer.announce(event)
                    event.mark_announced()
                    self._record_recap(event)
                except Exception:
                    self.pending_events.appendleft(event)
                    self.logger.exception("Announcement failed.")
                    break
        finally:
            # Runs whether the loop drained everything, broke on a
            # failed event, or announcer.announce() raised something
            # _record_recap() itself couldn't -- self.pending_events
            # always reflects the true state at that point (event
            # fully delivered and popped, or requeued with whatever
            # partial event.delivered_to progress
            # DiscordOutputRouter.publish() made), so one persist call
            # here is enough to keep storage in sync with it.
            self._persist_pending_events()

    def _record_recap(self, event: ProductionEvent) -> None:
        """Appends an announced RSS update to the rolling recap buffer.

        Only real live-feed updates are recorded; monitor-generated
        events (image changes, competition state) are not, since /recap
        is specifically "what happened on the feeds."
        """

        if event.event_type != EventType.RSS_UPDATE:
            return

        buffer = list(self.storage.get(self.RECAP_KEY, []))
        buffer.append(event.detail)

        if len(buffer) > self.RECAP_LIMIT:
            buffer = buffer[-self.RECAP_LIMIT:]

        self.storage.set(self.RECAP_KEY, buffer)

    def recent_updates(self, limit: int = 20) -> list[str]:
        """Returns the most recent announced live-feed updates."""

        buffer = list(self.storage.get(self.RECAP_KEY, []))
        return buffer[-limit:]

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
