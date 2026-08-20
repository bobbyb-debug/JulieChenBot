"""
Julie ChenBot Teach Command
============================

Lets an authorized administrator explicitly teach Julie durable,
authoritative knowledge -- facts, behavioral rules, corrections, and
current game-state values -- that outrank the AI's own inference and
the automated game-state/live-feed sources when they conflict.

See production/knowledge.py for the persistence model (KnowledgeStore,
backed by the existing Storage abstraction) and services/ai_service.py's
format_learned_knowledge() for how this reaches the AI. This command
family is a human-authoritative knowledge system, not a replacement
for the automated production monitors -- it never touches
HouseStatusMonitor, CompetitionMonitor, or HouseImageMonitor. /teach
update writes official-facts STATE items to KnowledgeStore only (see
production/state_sync.py for which topics have a comparable automated
field, used for conflict detection); it does not and must not mutate
HouseStatus, which stays exclusively the RSS pipeline's to write.

Permissions -- two tiers, deliberately different mechanisms:

    - /teach fact, /teach rule, /teach correction, /teach forget:
      gated by Discord's own client-side default_permissions
      (administrator=True) -- full server administrator only, enforced
      by Discord itself before the interaction reaches this code.
    - /teach batch, /teach update: a trusted-moderator tier (see
      _is_trusted_moderator() below) -- a full administrator always
      qualifies, or any user holding the Discord role configured via
      config.TRUSTED_MODERATOR_ROLE_ID (env var
      TRUSTED_MODERATOR_ROLE_ID). Enforced explicitly in each
      callback, not via default_permissions, because Discord's
      default_permissions can only express permission bits
      (administrator, manage_guild, ...) -- it has no way to name one
      specific configured role. If TRUSTED_MODERATOR_ROLE_ID is unset,
      these two behave as administrator-only in practice.
    - /teach list: open to everyone (read-only).

This is a widening of who can run batch/update, never a narrowing of
who can run the four original mutations -- those are completely
unaffected by TRUSTED_MODERATOR_ROLE_ID.
"""

from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands

from production.batch_teach import (
    BatchPlan,
    apply_plan,
    build_plan,
    parse_batch,
    parse_state_updates,
)
from config import TRUSTED_MODERATOR_ROLE_ID
from production.knowledge import KnowledgeItem, KnowledgeType
from production.state_sync import is_recognized_topic
from services.logger import ProductionLogger

logger = ProductionLogger.get("Teach")


def _is_trusted_moderator(interaction: discord.Interaction) -> bool:
    """Authorizes /teach batch and /teach update specifically -- a
    two-tier model distinct from every other /teach mutation.

    /teach fact, /teach rule, /teach correction, and /teach forget
    stay exactly as before: gated by Discord's own client-side
    default_permissions(administrator=True), enforced by Discord
    itself before the interaction ever reaches this code (see
    test_teach_command.py). That mechanism can only express "full
    server administrator" -- it has no concept of a specific
    configured role, which is what Part 10 of this session's
    architecture request asked for: a trusted-moderator tier that
    does not require full administrator.

    Discord's app_commands permission system offers no declarative
    way to restrict a command to one specific role (only to
    permission bits like `administrator`), so batch/update
    deliberately carry NO default_permissions restriction at all --
    every user can see them in the command picker -- and authorization
    is instead enforced here, explicitly, on every invocation. A full
    administrator always qualifies (nothing is taken away from
    admins); otherwise the user must hold the role configured via
    config.TRUSTED_MODERATOR_ROLE_ID (env var TRUSTED_MODERATOR_ROLE_ID)
    -- an explicit configured role, never an arbitrary hardcoded user
    ID. If that role is unset, only administrators qualify.
    """

    permissions = getattr(interaction.user, "guild_permissions", None)
    if permissions is not None and permissions.administrator:
        return True

    if not TRUSTED_MODERATOR_ROLE_ID:
        return False

    roles = getattr(interaction.user, "roles", None) or []
    return any(getattr(role, "id", None) == TRUSTED_MODERATOR_ROLE_ID for role in roles)


async def _reject_unauthorized(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "You need to be a trusted moderator or server administrator to use "
        "this command, Houseguest.",
        ephemeral=True,
    )

TYPE_ICONS = {
    KnowledgeType.FACT: "📌",
    KnowledgeType.RULE: "📜",
    KnowledgeType.CORRECTION: "✏️",
    KnowledgeType.STATE: "🎯",
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
    if item.topic is not None:
        embed.add_field(name="Topic", value=item.topic, inline=True)
    if item.supersedes is not None:
        embed.add_field(
            name="Supersedes", value=f"#{item.supersedes}", inline=True
        )
    return embed


# ==========================================================
# Batch training (/teach batch)
# ==========================================================


def _batch_preview_embed(plan: BatchPlan) -> discord.Embed:
    """Renders exactly what a confirmed batch would write, and nothing
    is written until a moderator actually clicks Confirm -- see
    _BatchConfirmView below."""

    embed = discord.Embed(title="🎓 Training Batch", color=0x9B59B6)

    summary_lines = []
    if plan.fact_count:
        summary_lines.append(f"📌 {plan.fact_count} Fact(s)")
    if plan.rule_count:
        summary_lines.append(f"📜 {plan.rule_count} Rule(s)")
    if plan.state_count:
        summary_lines.append(f"🎯 {plan.state_count} Current-State Update(s)")
    embed.description = (
        "\n".join(summary_lines) if summary_lines else "Nothing valid to train on."
    )

    if plan.conflicts:
        conflict_lines = []
        for conflict in plan.conflicts:
            current = conflict.current_value or "*(not set)*"
            conflict_lines.append(
                f"**{conflict.topic}**\nCurrent: {current}\nNew: {conflict.new_value}"
            )
        embed.add_field(
            name="⚠️ Potential changes",
            value="\n\n".join(conflict_lines)[:1024],
            inline=False,
        )

    if plan.invalid:
        invalid_lines = [
            f"Line {line.line_number}: {line.error}" for line in plan.invalid
        ]
        embed.add_field(
            name="❌ Could not parse",
            value="\n".join(invalid_lines)[:1024],
            inline=False,
        )

    if plan.valid:
        embed.set_footer(
            text="Confirm to write these changes, or Cancel to discard everything."
        )
    else:
        embed.set_footer(text="Nothing valid to confirm -- fix the lines and retry.")

    return embed


class _BatchConfirmView(discord.ui.View):
    """Confirm/Cancel gate for one /teach batch preview.

    Zero writes happen anywhere until handle_confirm() actually runs
    -- building the plan (parse_batch/build_plan) never touches the
    KnowledgeStore. Only the moderator who ran the command may confirm
    or cancel it. On timeout, buttons disable and nothing is written
    (matching Cancel's outcome) -- a batch a moderator walks away from
    must never silently apply itself.
    """

    # Overridden by _StateUpdateConfirmView so shared messages ("...
    # cancelled", "... timed out") read correctly for /teach update
    # too, without duplicating the whole class.
    _NOUN = "Batch"

    def __init__(
        self,
        *,
        plan: BatchPlan,
        knowledge,
        author_id: int,
        requester_id: int,
    ) -> None:
        super().__init__(timeout=120)
        self.plan = plan
        self.knowledge = knowledge
        self.author_id = author_id
        self.requester_id = requester_id
        self.message: Optional[discord.Message] = None

        if not plan.valid:
            for child in self.children:
                if getattr(child, "label", None) == "Confirm":
                    child.disabled = True

    async def _finish(self, interaction: discord.Interaction, content: str) -> None:
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(content=content, embed=None, view=self)

    async def handle_confirm(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the moderator who started this batch can confirm it.",
                ephemeral=True,
            )
            return

        written = apply_plan(self.plan, self.knowledge, self.author_id)

        logger.info(
            "/teach batch confirmed by %s: %d item(s) written.",
            self.requester_id,
            len(written),
        )

        await self._finish(interaction, f"✅ Trained {len(written)} item(s).")

    async def handle_cancel(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the moderator who started this batch can cancel it.",
                ephemeral=True,
            )
            return

        logger.info(
            "/teach %s cancelled by %s: zero writes.",
            self._NOUN.lower(),
            self.requester_id,
        )

        await self._finish(interaction, f"❌ {self._NOUN} cancelled. Nothing was written.")

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True

        logger.info(
            "/teach %s timed out for %s: zero writes.",
            self._NOUN.lower(),
            self.requester_id,
        )

        if self.message is not None:
            try:
                await self.message.edit(
                    content=f"⌛ {self._NOUN} timed out. Nothing was written.",
                    view=self,
                )
            except Exception:
                # Best-effort cosmetic update -- the message may already
                # be gone or the webhook token may have expired. Either
                # way, zero writes already happened; nothing to recover.
                pass

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.handle_confirm(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.handle_cancel(interaction)


# ==========================================================
# Manual current-state updates (/teach update)
# ==========================================================


def _state_update_preview_embed(plan: BatchPlan) -> discord.Embed:
    embed = discord.Embed(title="🎯 Current State Update", color=0x9B59B6)

    if plan.valid:
        embed.description = "\n".join(
            f"**{line.topic}** → {line.content}" for line in plan.valid
        )
    else:
        embed.description = "Nothing valid to update."

    if plan.conflicts:
        conflict_lines = []
        for conflict in plan.conflicts:
            current = conflict.current_value or "*(not confirmed)*"
            conflict_lines.append(
                f"**{conflict.topic}**\nCurrent: {current}\nNew: {conflict.new_value}"
            )
        embed.add_field(
            name="⚠️ Potential changes",
            value="\n\n".join(conflict_lines)[:1024],
            inline=False,
        )

    unrecognized = sorted(
        {line.topic for line in plan.valid if not is_recognized_topic(line.topic)}
    )
    if unrecognized:
        embed.add_field(
            name="ℹ️ Not linked to a structured command",
            value=(
                ", ".join(unrecognized)
                + " will be recorded as knowledge but won't change /hoh, "
                "/noms, /nominees, or /veto."
            ),
            inline=False,
        )

    if plan.invalid:
        invalid_lines = [
            f"Line {line.line_number}: {line.error}" for line in plan.invalid
        ]
        embed.add_field(
            name="❌ Could not parse",
            value="\n".join(invalid_lines)[:1024],
            inline=False,
        )

    if plan.valid:
        embed.set_footer(
            text="Confirm to apply these changes, or Cancel to discard everything."
        )
    else:
        embed.set_footer(text="Nothing valid to confirm -- fix the lines and retry.")

    return embed


class _StateUpdateConfirmView(_BatchConfirmView):
    """Like _BatchConfirmView, but on confirm writes each line as an
    official-facts STATE item in KnowledgeStore -- the sole
    authoritative source /hoh, /noms, /nominees, and /veto read (see
    commands/hoh.py, nominees.py, veto.py). This is what makes a
    manual /teach update take effect immediately.

    Deliberately does NOT touch HouseStatus (production/
    house_status.py): that object is the automated, live-feed-driven
    observation layer, updated only by production/engine.py's RSS
    pipeline. Keeping this write path from ever touching it is the
    whole point -- an automated parse must never be able to silently
    overwrite what a moderator just confirmed here, and a moderator's
    confirmed update must never be silently overwritten by the next
    automated parse either.

    A topic with no comparable HouseStatus field (see production/
    state_sync.py RECOGNIZED_TOPICS) is still written as an official
    fact the same way -- it simply has nothing to be diffed against
    for conflict detection, and the preview embed says so up front
    (see _state_update_preview_embed()).
    """

    _NOUN = "Update"

    def __init__(self, *, engine, **kwargs) -> None:
        super().__init__(**kwargs)
        self.engine = engine

    async def handle_confirm(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the moderator who started this update can confirm it.",
                ephemeral=True,
            )
            return

        written = apply_plan(self.plan, self.knowledge, self.author_id)

        applied_topics = [
            item.topic
            for item in written
            if item.topic and is_recognized_topic(item.topic)
        ]

        logger.info(
            "/teach update confirmed by %s: %d item(s) written, official "
            "state changed for: %s",
            self.requester_id,
            len(written),
            applied_topics,
        )

        summary = f"✅ Updated {len(written)} item(s)."
        if applied_topics:
            summary += f" Official state changed: {', '.join(applied_topics)}."

        await self._finish(interaction, summary)


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
            type_label = item.type.value.upper()
            if item.topic is not None:
                type_label += f" ({item.topic})"
            line = (
                f"**#{item.id} — {type_label}** {icon}\n"
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

    @teach.command(
        name="batch",
        description="Train Julie from a pasted multi-line batch (FACT:/RULE:/STATE: lines).",
    )
    @app_commands.describe(
        text=(
            "One FACT:, RULE:, or STATE: instruction per line, e.g. "
            '"STATE: HOH = Yash". Nothing is written until you confirm.'
        )
    )
    async def batch(interaction: discord.Interaction, text: str) -> None:
        if not _is_trusted_moderator(interaction):
            await _reject_unauthorized(interaction)
            return

        engine = discord_service.scheduler.engine

        lines = parse_batch(text)
        plan = build_plan(lines, engine.knowledge)

        if not lines:
            await interaction.response.send_message(
                "Paste some FACT:/RULE:/STATE: lines to train Julie with, Houseguest.",
                ephemeral=True,
            )
            return

        embed = _batch_preview_embed(plan)
        view = _BatchConfirmView(
            plan=plan,
            knowledge=engine.knowledge,
            author_id=interaction.user.id,
            requester_id=interaction.user.id,
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

        logger.info(
            "/teach batch previewed by %s (%s): %d valid, %d invalid, %d conflict(s).",
            interaction.user,
            interaction.user.id,
            len(plan.valid),
            len(plan.invalid),
            len(plan.conflicts),
        )

    @teach.command(
        name="update",
        description="Manually set current game state (HOH, Nominees, Veto Winner, Have-Nots).",
    )
    @app_commands.describe(
        text=(
            'One "TOPIC: value" per line, e.g. "HOH: Yash". '
            "Nothing changes until you confirm."
        ),
        reason="Optional: why you're making this manual update (recorded for provenance).",
    )
    async def update(
        interaction: discord.Interaction,
        text: str,
        reason: Optional[str] = None,
    ) -> None:
        if not _is_trusted_moderator(interaction):
            await _reject_unauthorized(interaction)
            return

        engine = discord_service.scheduler.engine

        lines = parse_state_updates(text, note=reason)
        plan = build_plan(lines, engine.knowledge)

        if not lines:
            await interaction.response.send_message(
                'Give me at least one "TOPIC: value" line, e.g. "HOH: Yash".',
                ephemeral=True,
            )
            return

        embed = _state_update_preview_embed(plan)
        view = _StateUpdateConfirmView(
            engine=engine,
            plan=plan,
            knowledge=engine.knowledge,
            author_id=interaction.user.id,
            requester_id=interaction.user.id,
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

        logger.info(
            "/teach update previewed by %s (%s): %d valid, %d invalid, %d conflict(s).",
            interaction.user,
            interaction.user.id,
            len(plan.valid),
            len(plan.invalid),
            len(plan.conflicts),
        )

    discord_service.bot.tree.add_command(teach)
