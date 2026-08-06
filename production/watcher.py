"""
Julie ChenBot Production Watcher
================================

Coordinates every monitoring system used by Julie ChenBot.

The ProductionWatcher owns every Monitor and executes them
once per production cycle.

Responsibilities
----------------
• Register monitors
• Execute monitors
• Measure execution time
• Collect ProductionEvents
• Return MonitorResults

The Production Engine owns exactly one ProductionWatcher.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from database.storage import Storage

from production.events import ProductionEvent
from production.monitors import (
    Monitor,
    MonitorResult,
    MonitorStatus,
)

from production.house_status import HouseStatusMonitor
from production.competition import CompetitionMonitor

from services.logger import ProductionLogger

logger = ProductionLogger.get("Watcher")


class ProductionWatcher:
    """
    Coordinates every production monitor.
    """

    def __init__(
        self,
        storage: Optional[Storage] = None,
    ) -> None:

        self.storage = storage or Storage()

        #
        # Registered monitors
        #

        self.monitors: list[Monitor] = []

        self._register_builtin_monitors()

        logger.info(
            "Production Watcher initialized."
        )

    # ======================================================
    # Built-in Monitors
    # ======================================================

    def _register_builtin_monitors(self) -> None:
        """
        Registers Julie's built-in monitoring systems.
        """

        self.register(
            HouseStatusMonitor(
                storage=self.storage,
            )
        )

        self.register(
            CompetitionMonitor()
        )

        #
        # Future monitors
        #
        # self.register(RSSMonitor(...))
        # self.register(ImageMonitor(...))
        # self.register(FeedMonitor(...))
        # self.register(TimelineMonitor(...))
        # self.register(ApiMonitor(...))
        # self.register(AIMonitor(...))

    # ======================================================
    # Registration
    # ======================================================

    def register(
        self,
        monitor: Monitor,
    ) -> None:
        """
        Registers a monitor.
        """

        self.monitors.append(
            monitor
        )

        logger.info(
            "Registered monitor: %s",
            monitor.name,
        )

    # ======================================================
    # Execution
    # ======================================================

    async def run(
        self,
    ) -> tuple[
        list[MonitorResult],
        list[ProductionEvent],
    ]:
        """
        Executes every registered monitor.

        Returns
        -------
        tuple
            (
                MonitorResults,
                ProductionEvents
            )
        """

        results: list[
            MonitorResult
        ] = []

        events: list[
            ProductionEvent
        ] = []

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

                    checked_at=datetime.utcnow(),
                )

            result.duration_ms = round(
                (
                    time.perf_counter()
                    - started
                )
                * 1000,
                2,
            )

            results.append(
                result
            )

            if result.events:

                events.extend(
                    result.events
                )

        return (
            results,
            events,
        )

    # ======================================================
    # Statistics
    # ======================================================

    @property
    def total_monitors(
        self,
    ) -> int:
        """
        Returns the total number of monitors.
        """

        return len(
            self.monitors
        )

    @property
    def enabled_monitors(
        self,
    ) -> int:
        """
        Returns the number of enabled monitors.
        """

        return sum(
            monitor.enabled
            for monitor in self.monitors
        )

    @property
    def disabled_monitors(
        self,
    ) -> int:
        """
        Returns the number of disabled monitors.
        """

        return (
            self.total_monitors
            - self.enabled_monitors
        )

    # ======================================================
    # Snapshot
    # ======================================================

    def snapshot(
        self,
    ) -> dict:
        """
        Returns a snapshot of the current watcher state.
        """

        return {

            "total_monitors":
                self.total_monitors,

            "enabled_monitors":
                self.enabled_monitors,

            "disabled_monitors":
                self.disabled_monitors,

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

                for monitor

                in self.monitors

            ],
        }