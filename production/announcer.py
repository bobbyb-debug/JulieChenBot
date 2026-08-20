"""
Julie ChenBot Production Announcer
=================================

Responsible for publishing ProductionEvents.

The Announcer is the final stage of Julie's production
pipeline. It receives ProductionEvents from the
ProductionEngine and dispatches them to configured outputs.
"""

from __future__ import annotations

from typing import Any

from services.logger import ProductionLogger

from production.events import EventSeverity, ProductionEvent

logger = ProductionLogger.get("Announcer")


class ProductionAnnouncer:
    """
    Publishes ProductionEvents.

    The ProductionEngine never communicates directly with Discord.
    Outputs are attached to this announcer and receive events here.
    """

    def __init__(self) -> None:
        self._discord_output: Any | None = None

        logger.info(
            "Production Announcer initialized."
        )

    def bind_discord(self, bot) -> None:
        """Binds the Discord bot as a production output."""

        from services.discord_output import DiscordOutputRouter

        self._discord_output = DiscordOutputRouter(bot)

        logger.info(
            "Discord output bound to Production Announcer."
        )

    async def announce(
        self,
        event: ProductionEvent,
    ) -> None:
        """Publishes a production event to all configured outputs."""

        level = event.severity

        if level == EventSeverity.CRITICAL:
            logger.critical(
                "%s :: %s",
                event.source,
                event.title,
            )
        elif level == EventSeverity.WARNING:
            logger.warning(
                "%s :: %s",
                event.source,
                event.title,
            )
        else:
            logger.info(
                "%s :: %s",
                event.source,
                event.title,
            )

        if self._discord_output is not None:
            await self._discord_output.publish(event)
