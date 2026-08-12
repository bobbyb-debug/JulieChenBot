"""
Julie ChenBot HOH Command
==========================

Shows the current Head of Household, from tracked house status.
"""

from __future__ import annotations

import discord

from services.logger import ProductionLogger

logger = ProductionLogger.get("HOH")


def register(discord_service) -> None:
    """Registers the /hoh slash command."""

    @discord_service.command(
        name="hoh",
        description="Shows the current Head of Household.",
    )
    async def hoh(interaction: discord.Interaction):

        status = discord_service.scheduler.engine.watcher.house_status.current

        if not status.hoh:
            message = (
                "👑 No Head of Household has been confirmed yet this cycle."
            )
        else:
            message = f"👑 **{status.hoh}** is the current Head of Household."

        await interaction.response.send_message(message)

        logger.info(
            "/hoh used by %s (%s)",
            interaction.user,
            interaction.user.id,
        )
