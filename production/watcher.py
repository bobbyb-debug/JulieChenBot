"""
Julie ChenBot Production Watcher
================================

Coordinates every monitoring system used by Julie ChenBot.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Optional

from database.storage import Storage
from production.competition import CompetitionMonitor
from production.events import ProductionEvent
from production.hamsterwatch import HamsterwatchMonitor
from production.house_image import HouseImageMonitor
from production.house_status import HouseStatusMonitor
from production.monitors import Monitor, MonitorResult, MonitorStatus
from production.quickview import BBUpdatesMonitor, QuickviewMonitor
from production.x_updates import XUpdatesMonitor
from services.logger import ProductionLogger

logger = ProductionLogger.get("Watcher")


@dataclass(slots=True)
class FailedMonitor:
    """Records a monitor whose construction raised.

    Makes the failure observable through ProductionWatcher.snapshot()
    instead of only ever appearing as a startup crash traceback.
    `error` is a plain exception message (str(exc)), not a full
    traceback and never the exception object itself -- enough for
    diagnostics without risking anything sensitive ending up in a
    status payload. The full traceback still goes to the log via
    logger.error(..., exc_info=True) in ProductionWatcher._construct().
    """

    name: str
    error: str


class ProductionWatcher:
    """Coordinates every production monitor."""

    def __init__(self, storage: Optional[Storage] = None) -> None:
        self.storage = storage or Storage()
        self.monitors: list[Monitor] = []
        self.failed_monitors: list[FailedMonitor] = []
        self.house_status: HouseStatusMonitor
        self.house_image: HouseImageMonitor
        self.competition: CompetitionMonitor
        self.hamsterwatch: HamsterwatchMonitor
        self.quickview: QuickviewMonitor
        self.bb_updates: BBUpdatesMonitor
        self.x_updates: XUpdatesMonitor
        self._register_builtin_monitors()
        logger.info("Production Watcher initialized.")

    def _construct(
        self,
        name: str,
        factory: Callable[[], Monitor],
    ) -> Optional[Monitor]:
        """Constructs one monitor, isolating a constructor failure so
        it cannot prevent the rest of ProductionWatcher from
        initializing.

        Returns the constructed monitor, or None if construction
        raised. A None return means the caller must not register it
        and must not assign it to a named attribute -- there is
        nothing to assign. The failure is recorded on
        self.failed_monitors (see FailedMonitor) and logged at ERROR
        level with the full traceback.
        """

        try:
            return factory()
        except Exception as exc:
            logger.error(
                "Monitor %s failed to initialize: %s",
                name,
                exc,
                exc_info=True,
            )
            self.failed_monitors.append(FailedMonitor(name=name, error=str(exc)))
            return None

    def _register_builtin_monitors(self) -> None:
        """Registers Julie's built-in monitoring systems.

        HamsterwatchMonitor is the one monitor whose construction
        performs real I/O: it builds a HamsterwatchArchive, which
        creates/opens a SQLite database and runs FTS5 schema DDL (see
        database/hamsterwatch_archive.py) -- and can therefore
        realistically raise (a SQLite build without FTS5, an
        unwritable database directory, a corrupted existing archive
        file). Its construction goes through _construct() so a
        failure there is recorded and skipped rather than aborting
        every other monitor's initialization.

        Every other monitor here is pure in-memory construction
        (verified: none of them perform filesystem, network, or
        database I/O), so they are constructed directly, unguarded,
        exactly as before. HouseStatusMonitor and CompetitionMonitor
        are additionally relied on as named attributes elsewhere
        (commands/hoh.py, commands/nominees.py, commands/veto.py,
        commands/recap.py, production/engine.py, services/discord.py)
        -- isolating construction that cannot realistically fail
        would add complexity without closing any real risk, and for
        those two specifically would trade a startup crash for a
        guaranteed AttributeError the first time any of those call
        sites runs.
        """
        self.house_status = HouseStatusMonitor(storage=self.storage)
        self.register(self.house_status)

        self.house_image = HouseImageMonitor(storage=self.storage)
        self.register(self.house_image)

        self.competition = CompetitionMonitor()
        self.register(self.competition)

        hamsterwatch = self._construct(
            "HamsterwatchMonitor",
            lambda: HamsterwatchMonitor(storage=self.storage),
        )
        if hamsterwatch is not None:
            self.hamsterwatch = hamsterwatch
            self.register(self.hamsterwatch)

        # Quickview and BBUpdates both watch the same bbusaupdates board
        # RSS already covers, but hash the entire page (ads, counters,
        # timestamps included) rather than extracting real content, so
        # every "changed" notification says only "content changed" with
        # nothing in it. BBUpdatesMonitor is the JokersUpdates page's own
        # "(old)" legacy duplicate of Quickview's page, so between the
        # two of them and RSS, three monitors were watching one board.
        # Kept instantiated (in case anything references them directly)
        # but not registered, so they no longer run or post.
        self.quickview = QuickviewMonitor(storage=self.storage)

        self.bb_updates = BBUpdatesMonitor(storage=self.storage)

        self.x_updates = XUpdatesMonitor(storage=self.storage)
        self.register(self.x_updates)

    def register(self, monitor: Monitor) -> None:
        """Registers a monitor."""
        self.monitors.append(monitor)
        logger.info("Registered monitor: %s", monitor.name)

    async def run(self) -> tuple[list[MonitorResult], list[ProductionEvent]]:
        """Executes every registered monitor and collects its events."""
        results: list[MonitorResult] = []
        events: list[ProductionEvent] = []

        for monitor in self.monitors:
            started = time.perf_counter()
            try:
                result = await monitor.run()
            except Exception as exc:
                logger.exception("Monitor %s failed.", monitor.name)
                result = MonitorResult(
                    monitor=monitor.name,
                    status=MonitorStatus.UNHEALTHY,
                    changed=False,
                    detail=str(exc),
                    checked_at=datetime.now(UTC),
                )

            result.duration_ms = round((time.perf_counter() - started) * 1000, 2)
            results.append(result)
            if result.events:
                events.extend(result.events)

        return results, events

    @property
    def total_monitors(self) -> int:
        return len(self.monitors)

    @property
    def enabled_monitors(self) -> int:
        return sum(monitor.enabled for monitor in self.monitors)

    @property
    def disabled_monitors(self) -> int:
        return self.total_monitors - self.enabled_monitors

    def snapshot(self) -> dict:
        """Returns a snapshot of the current watcher state."""
        return {
            "total_monitors": self.total_monitors,
            "enabled_monitors": self.enabled_monitors,
            "disabled_monitors": self.disabled_monitors,
            "failed_monitors": [
                {"name": failed.name, "error": failed.error}
                for failed in self.failed_monitors
            ],
            "monitors": [
                {
                    "name": monitor.name,
                    "enabled": monitor.enabled,
                    "last_status": (
                        monitor.last_result.status.value
                        if monitor.last_result
                        else "never_run"
                    ),
                }
                for monitor in self.monitors
            ],
        }
