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

        if self.running:
            return

        self.running = True

        self.logger.info(
            "Production scheduler started."
        )

        while self.running:

            try:

                await self.engine.tick()

            except Exception:

                self.logger.exception(
                    "Scheduler encountered an error."
                )

            await asyncio.sleep(
                CHECK_INTERVAL
            )

    # ==========================================================
    # Stop
    # ==========================================================

    def stop(self) -> None:

        self.running = False

        self.logger.info(
            "Production scheduler stopped."
        )