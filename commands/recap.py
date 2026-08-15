"""
Julie ChenBot Recap Command
============================

Summarizes recent live-feed updates in Julie's voice, combining:

    - current tracked game state (HOH, nominees, veto, have-nots)
    - the rolling recap buffer the engine records as it announces
      real Joker's Updates live-feed updates
    - a small, targeted slice of Hamsterwatch history relevant to
      who's actually in the game right now — never the whole
      archive
    - active administrator-taught RULEs only (see production/
      knowledge.py) -- not FACTs/CORRECTIONs. Rules are standing
      instructions ("the house-status image is authoritative for
      Have-Nots") that are relevant to every recap regardless of what
      it's about, the same way Hamsterwatch retrieval already avoids
      dumping the whole archive: FACTs/CORRECTIONs are about specific,
      possibly unrelated game facts (e.g. "Yash is HoH"), and recap
      has no cheap, reliable way to tell whether any given one is
      actually relevant to this particular summary without building a
      real relevance-matching system just for this -- so rather than
      risk pulling in stale, unrelated facts, they're left out of
      recap entirely. /chat and mentions/DMs still get the full active
      set via services/discord.py generate_ai_reply(), where a human
      is asking a specific question and can judge relevance themselves.

Retrieval (which Hamsterwatch articles are relevant) and generation
(the actual AI call) stay separate: this command decides what's
relevant using the tracked house status, HamsterwatchArchive decides
how to find it, and ai_service.generate_recap decides how to phrase
it — each source clearly labeled so the model never blurs Joker's
Updates with Hamsterwatch commentary.
"""

from __future__ import annotations

import discord

from database.hamsterwatch_archive import ArchivedArticle, HamsterwatchArchive
from production.house_status import HouseStatus
from production.knowledge import KnowledgeType
from services.ai_service import format_game_state, format_learned_knowledge, generate_recap
from services.logger import ProductionLogger

logger = ProductionLogger.get("Recap")

# A handful of relevant excerpts, never the whole archive. Content is
# also truncated per-entry so even a hit-heavy query stays a small,
# bounded addition to the AI prompt.
HAMSTERWATCH_CONTEXT_LIMIT = 5
HAMSTERWATCH_EXCERPT_CHARS = 400


def _player_keywords(house_status: HouseStatus) -> list[str]:
    """Collects known player names from tracked house status.

    Used to retrieve Hamsterwatch history relevant to who's actually
    in the game right now, rather than falling back to "just the most
    recent articles" every time.
    """

    names: list[str] = []
    if house_status.hoh:
        names.append(house_status.hoh)
    names.extend(house_status.nominees)
    if house_status.veto_holder:
        names.append(house_status.veto_holder)
    names.extend(house_status.have_nots)
    return list(dict.fromkeys(name for name in names if name))


def _format_hamsterwatch_entry(article: ArchivedArticle) -> str:
    """Formats one archived article for the AI prompt: labeled with
    its BB day/date and truncated to a bounded excerpt."""

    label_parts = []
    if article.bb_day:
        label_parts.append(f"Day {article.bb_day}")
    if article.article_date:
        label_parts.append(article.article_date)
    label = " - ".join(label_parts) if label_parts else article.heading

    excerpt = article.content
    if len(excerpt) > HAMSTERWATCH_EXCERPT_CHARS:
        excerpt = excerpt[:HAMSTERWATCH_EXCERPT_CHARS].rsplit(" ", 1)[0] + "…"

    return f"[{label}] {article.heading}: {excerpt}"


def register(discord_service) -> None:
    """Registers the /recap slash command."""

    @discord_service.command(
        name="recap",
        description="Summarizes recent live feed updates.",
    )
    async def recap(interaction: discord.Interaction):

        await interaction.response.defer()

        engine = discord_service.scheduler.engine
        entries = engine.recent_updates(limit=20)

        house_status = engine.watcher.house_status.current
        competition = engine.watcher.competition.current
        game_state = format_game_state(house_status, competition)
        # RULE-only: see the module docstring for why FACT/CORRECTION
        # knowledge is deliberately excluded from /recap specifically.
        active_rules = [
            item
            for item in engine.knowledge.active_items()
            if item.type == KnowledgeType.RULE
        ]
        knowledge = format_learned_knowledge(active_rules)

        archive = HamsterwatchArchive()
        hamsterwatch_articles = archive.find_relevant(
            _player_keywords(house_status), limit=HAMSTERWATCH_CONTEXT_LIMIT
        )
        hamsterwatch_entries = [
            _format_hamsterwatch_entry(article) for article in hamsterwatch_articles
        ]

        summary = await generate_recap(
            entries,
            game_state=game_state,
            hamsterwatch_entries=hamsterwatch_entries,
            knowledge=knowledge,
        )

        embed = discord.Embed(
            title="📼 Recent Feed Recap",
            description=summary,
            color=0x9B59B6,
        )

        footer_parts = []
        if entries:
            footer_parts.append(f"{len(entries)} Joker's Update(s)")
        if hamsterwatch_articles:
            footer_parts.append(f"{len(hamsterwatch_articles)} Hamsterwatch recap(s)")
        if footer_parts:
            embed.set_footer(text="Based on " + " + ".join(footer_parts))

        await interaction.followup.send(embed=embed)

        logger.info(
            "/recap used by %s (%s): %s update(s), %s Hamsterwatch source(s)",
            interaction.user,
            interaction.user.id,
            len(entries),
            len(hamsterwatch_articles),
        )
