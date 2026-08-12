"""
Julie ChenBot Nominees Command
===============================

Shows the current nominees, from tracked house status.
"""

from __future__ import annotations

import discord

from services.logger import ProductionLogger

logger = ProductionLogger.get("Nominees")


def register(discord_service) -> None:
    """Registers the /nominees slash command."""

    @discord_service.command(
        name="nominees",
        description="Shows the current nominees for eviction.",
    )
    async def nominees(interaction: discord.Interaction):

        status = discord_service.scheduler.engine.watcher.house_status.current

        if not status.nominees:
            message = "🎯 No nominees have been confirmed yet this cycle."
        else:
            names = ", ".join(status.nominees)
            message = f"🎯 Nominated: **{names}**"

        await interaction.response.send_message(message)

        logger.info(
            "/nominees used by %s (%s)",
            interaction.user,
            interaction.user.id,
        )
