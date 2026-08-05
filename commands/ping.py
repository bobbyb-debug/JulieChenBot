"""
Julie ChenBot Ping Command
==========================

Basic health check command.
"""

from __future__ import annotations

import discord

from services.logger import ProductionLogger

logger = ProductionLogger.get("Ping")


def register(discord_service) -> None:
    """
    Registers the /ping slash command.
    """

    @discord_service.command(
        name="ping",
        description="Checks whether Julie ChenBot is online.",
    )
    async def ping(interaction: discord.Interaction):

        logger.info(
            "/ping used by %s (%s)",
            interaction.user,
            interaction.user.id,
        )

        embed = discord.Embed(
            title="🏠 Julie ChenBot",
            description=(
                "Good evening, Houseguests...\n\n"
                "Julie ChenBot is online and monitoring the game."
            ),
            color=discord.Color.blue(),
        )

        embed.add_field(
            name="Status",
            value="🟢 Online",
            inline=True,
        )

        embed.add_field(
            name="Production",
            value="🎥 Watching the Live Feeds",
            inline=True,
        )

        embed.set_footer(
            text="Expect the unexpected."
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )