"""
Julie ChenBot HOH Command
==========================

Shows the current Head of Household, from the official facts record
(KnowledgeStore STATE topic "HOH" -- see production/knowledge.py),
set only via /teach update or the admin dashboard. Deliberately does
NOT read the automated, live-feed-driven HouseStatus (production/
house_status.py) -- that value is an unverified observation, never
treated as confirmed fact by any Discord command.
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

        official = discord_service.scheduler.engine.knowledge.active_state("HOH")

        if official is None or not official.content.strip():
            message = (
                "👑 No Head of Household has been confirmed yet this cycle."
            )
        else:
            message = f"👑 **{official.content}** is the current Head of Household."

        await interaction.response.send_message(message)

        logger.info(
            "/hoh used by %s (%s)",
            interaction.user,
            interaction.user.id,
        )
