"""
Julie ChenBot Nominees Command
===============================

Shows the current nominees, from the official facts record
(KnowledgeStore STATE topic "NOMINEES" -- see production/knowledge.py),
set only via /teach update or the admin dashboard. Deliberately does
NOT read the automated, live-feed-driven HouseStatus (production/
house_status.py) -- that value is an unverified observation, never
treated as confirmed fact by any Discord command.

Registered under two names -- /nominees (canonical) and /noms (short
alias) -- both calling the exact same _show_nominees() implementation
below. There is deliberately only one place that reads official state,
formats the message, and decides what "no nominees yet" looks like:
the two commands can never drift out of sync with each other, because
there is nothing in either of them to drift -- they are both a thin
wrapper around the same function.
"""

from __future__ import annotations

import discord

from services.logger import ProductionLogger

logger = ProductionLogger.get("Nominees")


async def _show_nominees(
    interaction: discord.Interaction,
    discord_service,
    command_name: str,
) -> None:
    """The one implementation both /nominees and /noms call. Same data
    source (engine.knowledge.active_state("NOMINEES")), same
    formatting, same "not confirmed yet" wording -- command_name only
    affects the log line, so /nominees and /noms usage can still be
    told apart in logs without any behavioral difference for the user.
    """

    official = discord_service.scheduler.engine.knowledge.active_state("NOMINEES")

    if official is None or not official.content.strip():
        message = "🎯 No nominees have been confirmed yet this cycle."
    else:
        message = f"🎯 Nominated: **{official.content}**"

    await interaction.response.send_message(message)

    logger.info(
        "/%s used by %s (%s)",
        command_name,
        interaction.user,
        interaction.user.id,
    )


def register(discord_service) -> None:
    """Registers /nominees and its short alias /noms.

    Each is registered exactly once, as two distinct command names --
    this is not the source of the duplicate-command issue fixed in
    services/discord.py (that was two Discord *scopes*, guild and
    global, both registering the same names; this is deliberately two
    different names sharing one implementation).
    """

    @discord_service.command(
        name="nominees",
        description="Shows the current nominees for eviction.",
    )
    async def nominees(interaction: discord.Interaction) -> None:
        await _show_nominees(interaction, discord_service, "nominees")

    @discord_service.command(
        name="noms",
        description="Shows the current nominees for eviction. (Short for /nominees)",
    )
    async def noms(interaction: discord.Interaction) -> None:
        await _show_nominees(interaction, discord_service, "noms")
