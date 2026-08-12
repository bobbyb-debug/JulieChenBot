"""
Julie ChenBot Recap Command
============================

Summarizes recent live-feed updates in Julie's voice, using the
rolling recap buffer the engine records as it announces real RSS
updates.
"""

from __future__ import annotations

import discord

from services.ai_service import generate_recap
from services.logger import ProductionLogger

logger = ProductionLogger.get("Recap")


def register(discord_service) -> None:
    """Registers the /recap slash command."""

    @discord_service.command(
        name="recap",
        description="Summarizes recent live feed updates.",
    )
    async def recap(interaction: discord.Interaction):

        await interaction.response.defer()

        engine = discord_service.scheduler.engine
        entries = engine.recent_updates(limit=20)

        summary = await generate_recap(entries)

        embed = discord.Embed(
            title="📼 Recent Feed Recap",
            description=summary,
            color=0x9B59B6,
        )

        if entries:
            embed.set_footer(
                text=f"Based on the last {len(entries)} update(s)."
            )

        await interaction.followup.send(embed=embed)

        logger.info(
            "/recap used by %s (%s): %s entries",
            interaction.user,
            interaction.user.id,
            len(entries),
        )
