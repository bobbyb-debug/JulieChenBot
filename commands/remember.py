"""
Julie ChenBot Remember Command
================================

Lets any Houseguest explicitly ask Julie to remember something
conversational -- a nickname, a running joke, a preference -- for
later recall in chat (see production/memory.py MemoryStore).

Open to everyone, unlike /teach: this is personal/conversational
memory, not an official game fact and not an administrative function.
It can never change what /hoh, /nominees, or /veto report, and is
never treated as an official game fact by the AI chat context (see
services/ai_service.py format_long_term_memory()).
"""

from __future__ import annotations

import discord

from services.logger import ProductionLogger

logger = ProductionLogger.get("Remember")


def register(discord_service) -> None:
    """Registers the /remember slash command."""

    @discord_service.command(
        name="remember",
        description="Ask Julie to remember something for this conversation.",
    )
    @discord.app_commands.describe(text="What should Julie remember?")
    async def remember(interaction: discord.Interaction, text: str):

        content = text.strip()

        if not content:
            await interaction.response.send_message(
                "There's nothing there to remember, Houseguest.",
                ephemeral=True,
            )
            return

        engine = discord_service.scheduler.engine
        author_name = getattr(interaction.user, "display_name", None) or str(
            interaction.user
        )

        engine.memory.remember(
            interaction.channel_id,
            interaction.user.id,
            author_name,
            content,
        )

        await interaction.response.send_message(
            f"🧠 Got it, I'll remember that: *{content}*"
        )

        logger.info(
            "/remember used by %s (%s) in channel %s: %s",
            interaction.user,
            interaction.user.id,
            interaction.channel_id,
            content,
        )
