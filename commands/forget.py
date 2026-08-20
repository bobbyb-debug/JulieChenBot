"""
Julie ChenBot Forget Command
=============================

Clears Julie's persisted Gemini conversation history for the current
channel, so a future conversation starts fresh.
"""

from __future__ import annotations

import discord

from services.ai_service import clear_history
from services.logger import ProductionLogger

logger = ProductionLogger.get("Forget")


def register(discord_service) -> None:
    """Registers the /forget slash command."""

    @discord_service.command(
        name="forget",
        description="Clears Julie's chat memory for this channel.",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def forget(interaction: discord.Interaction):

        removed = clear_history(interaction.channel_id)

        if removed:
            message = (
                f"🧹 Cleared {removed} message(s) of memory for this "
                "channel. Fresh start, Houseguest."
            )
        else:
            message = "There was nothing to forget in this channel."

        await interaction.response.send_message(message, ephemeral=True)

        logger.info(
            "/forget used by %s (%s) in channel %s: %s removed",
            interaction.user,
            interaction.user.id,
            interaction.channel_id,
            removed,
        )
