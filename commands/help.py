"""
Julie ChenBot Help Command
===========================

Tells users what Julie can actually do. Kept as a single hand-written
list rather than generated from the command tree, so the wording can
explain each command in plain terms instead of just repeating its
one-line slash-command description.

/forget, /posttest, and /status are Administrator-only (Discord
enforces this itself via default_permissions on each command), so
this help text only lists them for admins — showing a command a
regular member can't actually run just invites a confusing "you
don't have permission" error.
"""

from __future__ import annotations

import discord

from services.logger import ProductionLogger

logger = ProductionLogger.get("Help")


def _is_admin(interaction: discord.Interaction) -> bool:
    """Checks administrator status, safely, even outside a guild.

    interaction.user is a discord.Member (with guild_permissions) in
    a server, but a plain discord.User (with no such attribute) in a
    DM. Treat that missing-attribute case as non-admin rather than
    raising.
    """

    permissions = getattr(interaction.user, "guild_permissions", None)
    return bool(permissions and permissions.administrator)


def register(discord_service) -> None:
    """Registers the /help slash command."""

    @discord_service.command(
        name="help",
        description="Shows what Julie ChenBot can do.",
    )
    async def help_command(interaction: discord.Interaction):

        is_admin = _is_admin(interaction)

        embed = discord.Embed(
            title="🎥 Julie ChenBot — What I Can Do",
            description=(
                "I watch the live feeds and post real updates as they "
                "happen. Here's everything you can ask me directly."
            ),
            color=0x3498DB,
        )

        chat_lines = [
            "**@mention me** or **DM me** anytime — I'll reply in "
            "character, and I remember our conversation.",
            "`/chat <message>` — same thing, as a slash command.",
        ]

        if is_admin:
            chat_lines.append(
                "`/forget` — wipes my memory of this channel's "
                "conversation, fresh start. *(Admin only)*"
            )

        embed.add_field(
            name="💬 Talk to me",
            value="\n".join(chat_lines),
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

        teach_lines = [
            "`/teach list` — shows what I've been explicitly taught."
        ]
        if is_admin:
            teach_lines.extend(
                [
                    "`/teach fact <text>` — teach me a fact I should "
                    "treat as ground truth. *(Admin only)*",
                    "`/teach rule <text>` — teach me a behavioral "
                    "rule/source-of-truth instruction. *(Admin only)*",
                    "`/teach correction <text>` — correct something "
                    "I previously believed. *(Admin only)*",
                    "`/teach forget <id>` — deactivates a taught "
                    "item. *(Admin only)*",
                ]
            )

        embed.add_field(
            name="🧠 Teach me",
            value="\n".join(teach_lines),
            inline=False,
        )

        if is_admin:
            embed.add_field(
                name="🔧 Diagnostics *(Admin only)*",
                value=(
                    "`/status` — my current health: uptime, monitors, "
                    "last error if any\n"
                    "`/ping` — quick online check\n"
                    "`/posttest` — sends a test post to confirm "
                    "Discord output is working"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="🔧 Diagnostics",
                value="`/ping` — quick online check",
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
            "/help used by %s (%s), admin=%s",
            interaction.user,
            interaction.user.id,
            is_admin,
        )
