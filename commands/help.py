"""
Julie ChenBot Help Command
===========================

Tells users what Julie can actually do. Kept as a single hand-written
list rather than generated from the command tree, so the wording can
explain each command in plain terms instead of just repeating its
one-line slash-command description.
"""

from __future__ import annotations

import discord

from services.logger import ProductionLogger

logger = ProductionLogger.get("Help")


def register(discord_service) -> None:
    """Registers the /help slash command."""

    @discord_service.command(
        name="help",
        description="Shows what Julie ChenBot can do.",
    )
    async def help_command(interaction: discord.Interaction):

        embed = discord.Embed(
            title="🎥 Julie ChenBot — What I Can Do",
            description=(
                "I watch the live feeds and post real updates as they "
                "happen. Here's everything you can ask me directly."
            ),
            color=0x3498DB,
        )

        embed.add_field(
            name="💬 Talk to me",
            value=(
                "**@mention me** or **DM me** anytime — I'll reply in "
                "character, and I remember our conversation.\n"
                "`/chat <message>` — same thing, as a slash command.\n"
                "`/forget` — wipes my memory of this channel's "
                "conversation, fresh start."
            ),
            inline=False,
        )

        embed.add_field(
            name="🏠 Game state",
            value=(
                "`/hoh` — current Head of Household\n"
                "`/nominees` — current nominees\n"
                "`/veto` — Power of Veto holder and status\n\n"
                "These only know what's actually been confirmed on the "
                "live feeds — if nothing's been announced yet, I'll say "
                "so rather than guess."
            ),
            inline=False,
        )

        embed.add_field(
            name="📼 Feed recap",
            value=(
                "`/recap` — a quick summary of recent live feed "
                "updates, in my own words."
            ),
            inline=False,
        )

        embed.add_field(
            name="🔧 Diagnostics",
            value=(
                "`/status` — my current health: uptime, monitors, "
                "last error if any\n"
                "`/ping` — quick online check\n"
                "`/posttest` — sends a test post to confirm Discord "
                "output is working"
            ),
            inline=False,
        )

        embed.set_footer(
            text=(
                "Live updates post automatically to #live-updates and "
                "#house-status — no command needed for those."
            )
        )

        await interaction.response.send_message(embed=embed)

        logger.info(
            "/help used by %s (%s)",
            interaction.user,
            interaction.user.id,
        )
