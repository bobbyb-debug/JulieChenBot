"""
Julie ChenBot Scheduler
=======================

Runs Julie's Production Engine on a fixed interval.
"""

from __future__ import annotations

import asyncio

from config import CHECK_INTERVAL
from production.engine import ProductionEngine
from services.logger import ProductionLogger


class Scheduler:
    """
    Julie's background production scheduler.
    """

    def __init__(self) -> None:

        self.logger = ProductionLogger.get("Scheduler")

        self.engine = ProductionEngine()

        self.running = False

    # ==========================================================
    # Start
    # ==========================================================

    async def start(self) -> None:
        """
        Starts Julie's production scheduler.

        Executes one Production Engine cycle every
        CHECK_INTERVAL seconds until stopped.
        """

        if self.running:
            return

        self.running = True

        self.logger.info(
            "Production scheduler started."
        )

        while self.running:

            try:

                self.logger.info(
                    "Scheduler invoking engine.tick()."
                )

                await self.engine.tick()

                self.logger.info(
                    "Engine tick completed."
                )

            except Exception:

                self.logger.exception(
                    "Scheduler encountered an unexpected error."
                )

            await asyncio.sleep(
                CHECK_INTERVAL
            )

    # ==========================================================
    # Stop
    # ==========================================================

    def stop(self) -> None:
        """
        Stops the scheduler.
        """

        self.running = False

        self.logger.info(
            "Production scheduler stopped."
        )