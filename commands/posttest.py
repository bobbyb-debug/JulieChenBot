"""
Julie ChenBot Discord Output Test
=================================

Sends a diagnostic ProductionEvent through the real announcer/output path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import discord

from production.events import EventSeverity, EventType, ProductionEvent
from services.logger import ProductionLogger

logger = ProductionLogger.get("PostTest")


def register(discord_service) -> None:
    """Registers the /posttest slash command."""

    @discord_service.command(
        name="posttest",
        description="Tests Julie's production Discord output.",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def posttest(interaction: discord.Interaction):
        event = ProductionEvent(
            source="Julie ChenBot",
            event_type=EventType.RSS_UPDATE,
            title="DISCORD OUTPUT TEST",
            detail="Julie ChenBot Discord output is working. This message was sent through the production announcer.",
            severity=EventSeverity.INFO,
            created_at=datetime.now(UTC),
            metadata={
                "link": "https://www.jokersupdates.com/",
                "test": True,
            },
        )

        try:
            await discord_service.scheduler.engine.announcer.announce(event)
            event.mark_announced()

            await interaction.response.send_message(
                "✅ Test event sent through the ProductionAnnouncer to #live-updates.",
                ephemeral=True,
            )

            logger.info(
                "/posttest succeeded for %s (%s)",
                interaction.user,
                interaction.user.id,
            )

        except Exception:
            logger.exception("/posttest failed.")

            await interaction.response.send_message(
                "❌ Discord production output test failed. Check Julie's logs.",
                ephemeral=True,
            )
