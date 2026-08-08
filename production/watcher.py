"""
Julie ChenBot Production Watcher
================================

Coordinates every monitoring system used by Julie ChenBot.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Optional

from database.storage import Storage
from production.competition import CompetitionMonitor
from production.events import ProductionEvent
from production.house_status import HouseStatusMonitor
from production.monitors import Monitor, MonitorResult, MonitorStatus
from services.logger import ProductionLogger

logger = ProductionLogger.get("Watcher")


class ProductionWatcher:
    """Coordinates every production monitor."""

    def __init__(
        self,
        storage: Optional[Storage] = None,
    ) -> None:
        self.storage = storage or Storage()
        self.monitors: list[Monitor] = []
        self.house_status: HouseStatusMonitor
        self.competition: CompetitionMonitor

        self._register_builtin_monitors()

        logger.info("Production Watcher initialized.")

    # ======================================================
    # Built-in Monitors
    # ======================================================

    def _register_builtin_monitors(self) -> None:
        """Registers Julie's built-in monitoring systems."""

        self.house_status = HouseStatusMonitor(
            storage=self.storage,
        )
        self.register(self.house_status)

        self.competition = CompetitionMonitor()
        self.register(self.competition)

        # Future monitors can be registered here as they become ready.

    # ======================================================
    # Registration
    # ======================================================

    def register(self, monitor: Monitor) -> None:
        """Registers a monitor."""

        self.monitors.append(monitor)
        logger.info("Registered monitor: %s", monitor.name)

    # ======================================================
    # Execution
    # ======================================================

    async def run(
        self,
    ) -> tuple[list[MonitorResult], list[ProductionEvent]]:
        """Executes every registered monitor and collects its events."""

        results: list[MonitorResult] = []
        events: list[ProductionEvent] = []

        for monitor in self.monitors:
            started = time.perf_counter()

            try:
                result = await monitor.run()

            except Exception as exc:
                logger.exception(
                    "Monitor %s failed.",
                    monitor.name,
                )

                result = MonitorResult(
                    monitor=monitor.name,
                    status=MonitorStatus.UNHEALTHY,
                    changed=False,
                    detail=str(exc),
                    checked_at=datetime.now(UTC),
                )

            result.duration_ms = round(
                (time.perf_counter() - started) * 1000,
                2,
            )

            results.append(result)

            if result.events:
                events.extend(result.events)

        return results, events

    # ======================================================
    # Statistics
    # ======================================================

    @property
    def total_monitors(self) -> int:
        return len(self.monitors)

    @property
    def enabled_monitors(self) -> int:
        return sum(monitor.enabled for monitor in self.monitors)

    @property
    def disabled_monitors(self) -> int:
        return self.total_monitors - self.enabled_monitors

    # ======================================================
    # Snapshot
    # ======================================================

    def snapshot(self) -> dict:
        """Returns a snapshot of the current watcher state."""

        return {
            "total_monitors": self.total_monitors,
            "enabled_monitors": self.enabled_monitors,
            "disabled_monitors": self.disabled_monitors,
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
