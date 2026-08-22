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

from production.authorization import is_trusted_moderator
from services.logger import ProductionLogger
from services.message_chunking import send_long_message

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

        author_name = getattr(interaction.user, "display_name", None) or str(
            interaction.user
        )

        reply = await discord_service.generate_ai_reply(
            interaction.user.id,
            interaction.channel_id,
            message,
            author_name=author_name,
            is_moderator=is_trusted_moderator(interaction.user),
        )

        await send_long_message(interaction.followup.send, reply)

        logger.info(
            "/chat used by %s (%s)",
            interaction.user,
            interaction.user.id,
        )
