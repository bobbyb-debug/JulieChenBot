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
from production.competition import CompetitionState
from production.events import EventSeverity, EventType, ProductionEvent
from production.house_status import HouseStatus
from production.imgur import ImgurResolver
from production.knowledge import KnowledgeStore
from production.monitors import MonitorResult, MonitorStatus
from production.parser import ProductionParser
from production.rss import FeedUpdate, JokersRSS
from production.watcher import ProductionWatcher
from services.logger import ProductionLogger


class ProductionEngine:
    """Coordinates Julie ChenBot's production systems."""

    RECAP_KEY = "recap_buffer"
    # Sized well above what a single very active 24-hour period of
    # live-feed items could produce -- recent_updates() below filters
    # by time, not count, so this cap only bounds retained raw
    # history and must comfortably outlast the actual recap window.
    RECAP_LIMIT = 500
    # /recap's default lookback window (see recent_updates() below).
    RECAP_WINDOW_HOURS = 24
    PENDING_EVENTS_KEY = "pending_events"
    GAME_STATE_KEY = "game_state"
    # Durable rolling log of *delivered* events (see _record_event_log()
    # below) -- distinct from pending_events, which only ever holds
    # events not yet (fully) delivered. Added for the admin dashboard's
    # Activity/Diagnostics/Event Trace views (see admin_api/), which
    # need "what actually happened recently," not just "what's still
    # queued." Capped well above any realistic single-day volume so it
    # stays a bounded, cheap read/write against the same JSON Storage
    # every other durable key already uses -- no new database.
    EVENT_LOG_KEY = "event_log"
    EVENT_LOG_LIMIT = 200

    def __init__(self, storage: Optional[Storage] = None) -> None:
        self.logger = ProductionLogger.get("Engine")
        self.storage = storage or Storage()

        self.rss = JokersRSS(storage=self.storage)
        self.parser = ProductionParser()
        self.watcher = ProductionWatcher(storage=self.storage)
        self.announcer = ProductionAnnouncer()
        # Resolves the Imgur images behind an (IMG)-tagged RSS item
        # (see production/imgur.py) -- one long-lived instance so its
        # missing-Client-ID warning (config.IMGUR_CLIENT_ID) only logs
        # once per process, not once per (IMG) post.
        self.imgur = ImgurResolver()

        # Administrator-taught knowledge (see production/knowledge.py
        # and commands/teach.py). Unlike RSS-derived game state, this
        # is never touched by tick() -- it changes only when a Discord
        # command mutates it, and KnowledgeStore persists immediately
        # on every mutation rather than waiting for a production cycle.
        self.knowledge = KnowledgeStore(storage=self.storage)

        # ProductionParser and HouseStatusMonitor/CompetitionMonitor all
        # start with blank in-memory state and have no persistence of
        # their own -- a restart otherwise silently forgets HOH,
        # nominees, veto, have-nots, feeds, and competition state until
        # an RSS item happens to re-state the same fact, which Big
        # Brother's live feed rarely does once something's already been
        # announced. Restoring here, right after both are constructed,
        # means every consumer of watcher.house_status.current /
        # watcher.competition.current (commands/hoh.py, nominees.py,
        # veto.py, recap.py, services/discord.py's /chat path) sees the
        # restored state immediately, with no separate restoration path.
        self._load_game_state()

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

    def _load_game_state(self) -> None:
        """Restores the authoritative house-status/competition state
        left by a previous process instance.

        Restores the SAME values into both places that need to agree:
        ProductionParser's own cumulative baseline (so a later partial
        update -- e.g. a new nominees line -- doesn't get merged
        against a blank parser baseline and wipe out fields the parser
        doesn't yet know about, such as an already-known HOH) and the
        watcher monitors' .current (what every command and
        format_game_state() actually reads). This is the one
        authoritative restoration path -- nothing else assigns to
        these attributes at startup.

        Restoring directly into .current, rather than through
        update()/check(), produces no MonitorResult and no events:
        restoring state on startup is not the same as re-announcing
        it, so no Discord message is generated merely because the
        process restarted.

        A missing key (storage.json predates this feature) or
        malformed persisted data (a corrupted/hand-edited file) both
        fall through to a logged warning and leave the already-blank
        defaults in place -- either way, this must never prevent
        Julie from starting.
        """

        data = self.storage.get(self.GAME_STATE_KEY, None)

        if not data:
            return

        try:
            house_status = HouseStatus.from_dict(data.get("house_status", {}))
            competition = CompetitionState.from_dict(data.get("competition", {}))
        except Exception:
            self.logger.warning(
                "Discarding malformed persisted game state: %r", data
            )
            return

        self.parser.house_status = house_status
        self.parser.competition = competition
        self.watcher.house_status.current = house_status
        self.watcher.competition.current = competition

        self.logger.info(
            "Restored game state from previous run: hoh=%r, nominees=%r, "
            "veto_holder=%r, competition=%r, winner=%r",
            house_status.hoh,
            house_status.nominees,
            house_status.veto_holder,
            competition.competition.value,
            competition.winner,
        )

    def _persist_game_state(self) -> None:
        """Durably persists the current authoritative house-status/
        competition state.

        Called only when tick() observes that watcher.run() actually
        promoted a new value into .current this cycle -- not on every
        tick regardless of change, since most ticks have nothing new
        to persist (an RSS item that doesn't change tracked state
        must not trigger a write here).
        """

        self.storage.set(
            self.GAME_STATE_KEY,
            {
                "house_status": self.watcher.house_status.current.to_dict(),
                "competition": self.watcher.competition.current.to_dict(),
            },
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
    def _rss_event(
        update: FeedUpdate, image_urls: list[str] | None = None
    ) -> ProductionEvent:
        """Converts one Joker's Updates item into a publishable event.

        image_urls holds every image resolved for this update (see
        production/imgur.py ImgurResolver, called from tick() below)
        -- [] when the update has no image, same as before this
        existed. metadata["image_urls"] carries the full list;
        metadata["image_url"] is kept alongside it as the first
        resolved image (or FeedUpdate's own image_url, if the RSS
        feed already provided one directly -- see production/rss.py
        _extract_image_url()) so any event recovered from storage by
        A3's durability under the old single-image schema still
        attaches its one image exactly as before. DiscordOutputRouter
        treats an empty image_url/image_urls exactly like an event
        with no image at all -- see services/discord_output.py.
        """

        detail = update.title.strip()
        if update.description and update.description.strip():
            detail = update.description.strip()

        resolved_images = image_urls or []

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
                "image_url": update.image_url
                or (resolved_images[0] if resolved_images else ""),
                "image_urls": resolved_images,
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

                # Synchronous and, for an (IMG)-tagged item, performs
                # real network requests (the Joker's post page, then
                # Imgur's API per image -- see production/imgur.py).
                # Offloaded to a worker thread for the same reason as
                # check_all() above: this must never block the event
                # loop. Every other update returns [] immediately
                # without any network request (see
                # ImgurResolver.resolve_images_for_update()).
                image_urls = await asyncio.to_thread(
                    self.imgur.resolve_images_for_update, rss_update
                )

                # Every newly surfaced RSS item is a production event. The
                # parser may additionally recognize production-state changes,
                # which are fed into the built-in monitors below.
                self.pending_events.append(
                    self._rss_event(rss_update, image_urls)
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

            house_status_before = self.watcher.house_status.current
            competition_before = self.watcher.competition.current

            results, events = await self.watcher.run()
            self.last_results = results
            self.pending_events.extend(events)

            # Persist only when watcher.run() actually promoted a new
            # value into .current this cycle -- comparing before/after
            # here (rather than relying on MonitorResult.changed) is
            # what correctly covers the "first observation" case too:
            # HouseStatusMonitor/CompetitionMonitor both report
            # changed=False for their very first captured state (see
            # production/house_status.py, production/competition.py),
            # even though .current just went from blank to populated --
            # exactly the case that must be persisted so a restart
            # right after the season's first HOH reveal doesn't lose it.
            if (
                self.watcher.house_status.current != house_status_before
                or self.watcher.competition.current != competition_before
            ):
                self._persist_game_state()

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
                    self._record_event_log(event)
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
        events (image changes, competition state, Hamsterwatch) are
        not, since /recap is specifically "what happened on the raw
        Joker's Updates feed." Each entry retains its original event
        timestamp (not just append order) so recent_updates() can
        filter by an actual elapsed-time window rather than a fixed
        count -- see recent_updates() below for why that matters.
        """

        if event.event_type != EventType.RSS_UPDATE:
            return

        buffer = list(self.storage.get(self.RECAP_KEY, []))
        buffer.append(
            {"created_at": event.created_at.isoformat(), "detail": event.detail}
        )

        if len(buffer) > self.RECAP_LIMIT:
            buffer = buffer[-self.RECAP_LIMIT:]

        self.storage.set(self.RECAP_KEY, buffer)

    def recent_updates(self, hours: float | None = None) -> list[str]:
        """Returns live-feed update text from roughly the last `hours`
        hours (default RECAP_WINDOW_HOURS), oldest first.

        Buffer entries are the {"created_at", "detail"} shape written
        by _record_recap() above, or -- from before per-entry
        timestamps existed -- a bare string. A legacy bare string has
        no recoverable timestamp, so rather than guess or assume it's
        still recent, it is simply excluded here; this never crashes
        loading, it just means a legacy entry can no longer be
        time-windowed and ages out of relevance on its own as new
        entries are recorded. An entry timestamped in the future
        (clock skew, corrupted data) is excluded the same way, so it
        can never be miscounted as "recent."

        Duplicate detail text within the window is collapsed (first
        occurrence kept, order preserved) as a defensive safety net --
        RSS's own GUID-based dedup (see production/rss.py
        JokersRSS.check_all()) already prevents the same live-feed
        item from being recorded twice in the normal case.
        """

        window_hours = self.RECAP_WINDOW_HOURS if hours is None else hours
        now = datetime.now(UTC)
        cutoff = now - timedelta(hours=window_hours)

        buffer = list(self.storage.get(self.RECAP_KEY, []))

        recent: list[str] = []
        for entry in buffer:
            if not isinstance(entry, dict):
                continue  # legacy bare-string entry -- no timestamp

            detail = entry.get("detail")
            created_at_raw = entry.get("created_at")
            if not detail or not created_at_raw:
                continue

            try:
                created_at = datetime.fromisoformat(created_at_raw)
            except (ValueError, TypeError):
                continue

            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)

            if created_at < cutoff or created_at > now:
                continue

            recent.append(detail)

        return list(dict.fromkeys(recent))

    def _record_event_log(self, event: ProductionEvent) -> None:
        """Appends one successfully-announced event to the durable
        activity log (EVENT_LOG_KEY), for the admin dashboard's
        Activity/Diagnostics/Event Trace views.

        Unlike _record_recap() (RSS_UPDATE only, plain detail text),
        every event type is recorded here with enough structure to
        show source -> event type -> destination -> outcome. Only
        called after announcer.announce() succeeds (see announce()
        above), so this is genuinely "what was delivered," not "what
        was attempted."
        """

        log = list(self.storage.get(self.EVENT_LOG_KEY, []))
        log.append(
            {
                "event_type": event.event_type.value,
                "source": event.source,
                "title": event.title,
                "detail": event.detail,
                "severity": event.severity.value,
                "created_at": event.created_at.isoformat(),
                "delivered_to": sorted(event.delivered_to),
            }
        )

        if len(log) > self.EVENT_LOG_LIMIT:
            log = log[-self.EVENT_LOG_LIMIT:]

        self.storage.set(self.EVENT_LOG_KEY, log)

    def recent_events(self, limit: int = 50) -> list[dict]:
        """Returns the most recent delivered events, newest first.

        Read-only, defensive against a hand-edited/corrupted log entry
        (skipped, never allowed to break the whole response) -- same
        posture as every other _load()-style reader in this codebase.
        """

        log = list(self.storage.get(self.EVENT_LOG_KEY, []))

        valid = [entry for entry in log if isinstance(entry, dict)]
        return list(reversed(valid))[:limit]

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
