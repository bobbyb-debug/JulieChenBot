"""
Julie ChenBot Chat Command
===========================

An explicit slash-command trigger for talking to Julie, alongside
@mentioning her or DMing her. Uses the same cooldown, game-state
context, and Gemini call as the mention/DM path — nothing here
duplicates that logic.
"""

from __future__ import annotations

import discord

from services.logger import ProductionLogger

logger = ProductionLogger.get("Chat")


def register(discord_service) -> None:
    """Registers the /chat slash command."""

    @discord_service.command(
        name="chat",
        description="Ask Julie ChenBot anything.",
    )
    @discord.app_commands.describe(message="What do you want to ask Julie?")
    async def chat(interaction: discord.Interaction, message: str):

        await interaction.response.defer()

        reply = await discord_service.generate_ai_reply(
            interaction.user.id,
            interaction.channel_id,
            message,
        )

        await interaction.followup.send(reply)

        logger.info(
            "/chat used by %s (%s)",
            interaction.user,
            interaction.user.id,
        )
