"""
Julie ChenBot Production Announcer
=================================

Responsible for publishing ProductionEvents.

The Announcer is the final stage of Julie's production
pipeline. It receives ProductionEvents from the
ProductionEngine and decides how to publish them.

Today:
    • Professional logging

Future:
    • Discord
    • TikTok overlays
    • Web dashboard
    • Push notifications
"""

from __future__ import annotations

from services.logger import ProductionLogger

from production.events import (
    EventSeverity,
    ProductionEvent,
)

logger = ProductionLogger.get("Announcer")


class ProductionAnnouncer:
    """
    Publishes ProductionEvents.

    The ProductionEngine should never communicate
    directly with Discord. It simply forwards events
    to the announcer.
    """

    def __init__(self) -> None:

        logger.info(
            "Production Announcer initialized."
        )

    # =====================================================
    # Publish
    # =====================================================

    async def announce(
        self,
        event: ProductionEvent,
    ) -> None:
        """
        Publishes a production event.

        Future versions will dispatch to Discord,
        TikTok, dashboards, and other outputs.
        """

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