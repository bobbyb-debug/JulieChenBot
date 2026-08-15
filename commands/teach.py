"""
Julie ChenBot Teach Command
============================

Lets an authorized administrator explicitly teach Julie durable,
authoritative knowledge -- facts, behavioral rules, and corrections --
that outrank the AI's own inference and the automated game-state/
live-feed sources when they conflict.

See production/knowledge.py for the persistence model (KnowledgeStore,
backed by the existing Storage abstraction) and services/ai_service.py's
format_learned_knowledge() for how this reaches the AI. This command
family is a human-authoritative knowledge system, not a replacement
for the automated production monitors -- it does not touch
HouseStatusMonitor, CompetitionMonitor, or HouseImageMonitor.
"""

from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands

from production.knowledge import KnowledgeItem, KnowledgeType
from services.logger import ProductionLogger

logger = ProductionLogger.get("Teach")

TYPE_ICONS = {
    KnowledgeType.FACT: "📌",
    KnowledgeType.RULE: "📜",
    KnowledgeType.CORRECTION: "✏️",
}

# Discord embed description limit is 4096 characters; leave headroom
# for the truncation note itself.
LIST_CHAR_LIMIT = 3900


def _knowledge_embed(item: KnowledgeItem) -> discord.Embed:
    embed = discord.Embed(
        title="🧠 Learned",
        description=item.content,
        color=0x9B59B6,
    )
    embed.add_field(name="Knowledge ID", value=f"#{item.id}", inline=True)
    embed.add_field(name="Type", value=item.type.value.title(), inline=True)
    if item.supersedes is not None:
        embed.add_field(
            name="Supersedes", value=f"#{item.supersedes}", inline=True
        )
    return embed


def register(discord_service) -> None:
    """Registers the /teach command group."""

    teach = app_commands.Group(
        name="teach",
        description="Teach Julie durable, authoritative knowledge.",
    )

    async def _teach(
        interaction: discord.Interaction,
        knowledge_type: KnowledgeType,
        text: str,
        supersedes: Optional[int] = None,
    ) -> None:
        stripped = text.strip()

        if not stripped:
            await interaction.response.send_message(
                "Teach me something with actual content, Houseguest.",
                ephemeral=True,
            )
            return

        engine = discord_service.scheduler.engine

        try:
            item = engine.knowledge.teach(
                knowledge_type, stripped, interaction.user.id, supersedes=supersedes
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(embed=_knowledge_embed(item))

        logger.info(
            "/teach %s #%s taught by %s (%s): %s%s",
            knowledge_type.value,
            item.id,
            interaction.user,
            interaction.user.id,
            item.content,
            f" (supersedes #{supersedes})" if supersedes is not None else "",
        )

    @teach.command(
        name="fact",
        description="Teach Julie a fact she should treat as ground truth.",
    )
    @app_commands.describe(
        text="The fact to teach Julie.",
        supersedes="Optional: Knowledge ID this fact replaces (see /teach list).",
    )
    @app_commands.default_permissions(administrator=True)
    async def fact(
        interaction: discord.Interaction,
        text: str,
        supersedes: Optional[int] = None,
    ) -> None:
        await _teach(interaction, KnowledgeType.FACT, text, supersedes)

    @teach.command(
        name="rule",
        description="Teach Julie a behavioral rule or source-of-truth instruction.",
    )
    @app_commands.describe(text="The rule to teach Julie.")
    @app_commands.default_permissions(administrator=True)
    async def rule(interaction: discord.Interaction, text: str) -> None:
        await _teach(interaction, KnowledgeType.RULE, text)

    @teach.command(
        name="correction",
        description="Correct something Julie previously believed.",
    )
    @app_commands.describe(
        text="The correction.",
        supersedes=(
            "Optional: Knowledge ID this correction replaces "
            "(see /teach list) -- deactivates it automatically."
        ),
    )
    @app_commands.default_permissions(administrator=True)
    async def correction(
        interaction: discord.Interaction,
        text: str,
        supersedes: Optional[int] = None,
    ) -> None:
        await _teach(interaction, KnowledgeType.CORRECTION, text, supersedes)

    @teach.command(
        name="list",
        description="Shows Julie's active learned knowledge.",
    )
    async def list_knowledge(interaction: discord.Interaction) -> None:
        engine = discord_service.scheduler.engine
        items = engine.knowledge.active_items()

        if not items:
            await interaction.response.send_message(
                "🧠 Julie hasn't been taught anything yet.",
                ephemeral=True,
            )
            return

        lines = []
        for item in items:
            icon = TYPE_ICONS.get(item.type, "🧠")
            taught = item.created_at.strftime("%b %d, %Y")
            line = (
                f"**#{item.id} — {item.type.value.upper()}** {icon}\n"
                f"{item.content}\n"
                f"Taught: {taught}"
            )
            if item.supersedes is not None:
                line += f" · supersedes #{item.supersedes}"
            lines.append(line)

        description = "\n\n".join(lines)
        truncated = len(description) > LIST_CHAR_LIMIT
        if truncated:
            description = description[:LIST_CHAR_LIMIT].rsplit("\n\n", 1)[0]
            description += f"\n\n…and {len(items)} total item(s). Some are hidden."

        embed = discord.Embed(
            title="🧠 Julie's Learned Knowledge",
            description=description,
            color=0x9B59B6,
        )
        embed.set_footer(text=f"{len(items)} active item(s)")

        await interaction.response.send_message(embed=embed)

        logger.info(
            "/teach list used by %s (%s): %d active item(s)",
            interaction.user,
            interaction.user.id,
            len(items),
        )

    @teach.command(
        name="forget",
        description="Deactivates a previously taught knowledge item.",
    )
    @app_commands.describe(item_id="The Knowledge ID to forget (see /teach list).")
    @app_commands.default_permissions(administrator=True)
    async def forget(interaction: discord.Interaction, item_id: int) -> None:
        engine = discord_service.scheduler.engine
        removed = engine.knowledge.forget(item_id)

        if removed:
            message = f"🧠 Forgot knowledge #{item_id}."
        else:
            message = f"There's no active knowledge #{item_id} to forget."

        await interaction.response.send_message(message, ephemeral=True)

        logger.info(
            "/teach forget #%s used by %s (%s): %s",
            item_id,
            interaction.user,
            interaction.user.id,
            removed,
        )

    discord_service.bot.tree.add_command(teach)
