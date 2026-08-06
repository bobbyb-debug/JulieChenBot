"""
Julie ChenBot Scheduler
=======================

Runs background tasks for Julie ChenBot.

Current Tasks
-------------
• Checks JokersUpdates RSS every 60 seconds.
"""

from __future__ import annotations

import asyncio

from config import CHECK_INTERVAL
from production.engine import JokersRSS
from services.logger import ProductionLogger


class Scheduler:
    """
    Julie's background production scheduler.
    """

    def __init__(self) -> None:

        self.logger = ProductionLogger.get("Scheduler")

        self.jokers = JokersRSS()

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

                update = self.jokers.check()

                if update:

                    self.logger.info(
                        "NEW LIVE FEED UPDATE"
                    )

                    self.logger.info(
                        update.title
                    )

                    self.logger.info(
                        update.link
                    )

                else:

                    self.logger.info(
                        "No new live feed updates."
                    )

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