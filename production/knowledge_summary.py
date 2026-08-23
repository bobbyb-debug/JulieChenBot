"""
Julie ChenBot Knowledge Summary
==================================

Two responsibilities for one feature -- answering a genuine "tell me
everything you know" style question -- kept in this one module because
they're both deterministic, no-AI-call, read-only decisions:

1. is_broad_knowledge_query() -- detects the question shape at all.
2. collect_summary_metadata() -- collects what's SAFE and TRUE to say
   about Julie's knowledge right now, as a small structured object.

Neither function ever calls an AI provider, and collect_summary_metadata()
never returns anything beyond counts/booleans/topic names -- no
knowledge content, no memory content, no article text. See
services/ai_service.py format_knowledge_summary_guidance() for how this
metadata is rendered into the model's prompt: that function narrates
FROM this data, it never invents a capability this module didn't
report, and this module never reports a capability that isn't actually
backed by a real store read.

Critical distinction this module exists to preserve (see the module's
callers): "Julie has a system capable of retrieving historical HOH
information" (a capability -- always true) is NOT the same claim as
"Julie currently has a verified historical HOH record" (actual data --
only true if historical_hoh_known_winners_count > 0). Every field below
reports actual data, never a hardcoded capability list -- see
format_knowledge_summary_guidance() for how the two are distinguished
in the rendered text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from database.hamsterwatch_archive import HamsterwatchArchive
from database.historical_events import HistoricalEventStore
from production.competition import CompetitionState
from production.house_status import HouseStatus
from production.knowledge import KnowledgeItem, KnowledgeType
from production.memory import MemoryStore

_BROAD_SUMMARY_TRIGGERS = (
    "tell me everything",
    "what do you know",
    "what all do you know",
    "what can you do",
    "what are you capable of",
    "what are your capabilities",
    "what knowledge do you have",
    "what information do you have",
    "what do you have access to",
)

# A trigger phrase followed by "about" is USUALLY scoping the question
# down to one specific topic ("tell me everything you know about
# Taylor's HOH") rather than genuinely asking for a capability
# overview -- that belongs to the existing historical/Hamsterwatch
# retrieval, not this feature. But "about" alone is too blunt a signal:
# "tell me everything you know about the history of Big Brother 28" is
# still a genuine broad request (there's no single player/week/cycle
# to look up), and treating it as narrowly scoped left it with nothing
# useful to retrieve at all. _BROAD_ABOUT_CONTINUATIONS carves out the
# generic topics that keep a question broad even with "about" present.
_SCOPING_MARKER = "about"
_BROAD_ABOUT_CONTINUATIONS = ("history", "season", "show", "game", "everything")
# How far past "about" to look for one of those words -- long enough
# for "about the history of Big Brother 28", short enough that a real
# specific topic much further into a long sentence doesn't accidentally
# get read as still-broad.
_ABOUT_LOOKAHEAD_CHARS = 40


def is_broad_knowledge_query(user_text: str) -> bool:
    """True for a genuine "what do you know"/"what can you do" style
    capability question -- see module docstring for why this is
    phrase-based, and _BROAD_ABOUT_CONTINUATIONS's own comment for why
    "about" alone doesn't automatically disqualify a question."""

    lowered = user_text.strip().lower()

    if not any(trigger in lowered for trigger in _BROAD_SUMMARY_TRIGGERS):
        return False

    about_index = lowered.find(_SCOPING_MARKER)
    if about_index == -1:
        return True

    lookahead = lowered[about_index:about_index + _ABOUT_LOOKAHEAD_CHARS]
    return any(word in lookahead for word in _BROAD_ABOUT_CONTINUATIONS)


@dataclass(slots=True)
class KnowledgeSummaryMetadata:
    """What collect_summary_metadata() actually found -- safe,
    high-level, and content-free by construction: every field is a
    count, a boolean, or a bare topic/label name, never a knowledge
    item's content, a memory's content, or an article's text. See the
    module docstring's capability-vs-data distinction."""

    official_state_topics: tuple[str, ...] = field(default_factory=tuple)
    admin_rule_count: int = 0
    admin_fact_count: int = 0
    admin_correction_count: int = 0
    historical_hoh_known_winners_count: int = 0
    historical_hoh_seasons_known: tuple[int, ...] = field(default_factory=tuple)
    hamsterwatch_article_count: int = 0
    live_feed_populated: bool = False
    channel_memory_count: int = 0


def collect_summary_metadata(
    *,
    knowledge_items: list[KnowledgeItem],
    historical_events: HistoricalEventStore,
    hamsterwatch_archive: HamsterwatchArchive | None,
    house_status: HouseStatus,
    competition: CompetitionState,
    memory_store: MemoryStore,
    channel_id: int,
) -> KnowledgeSummaryMetadata:
    """Deterministically collects real, current, safe-to-share
    metadata about what Julie actually knows right now -- no AI call,
    no mutation of anything, and never a store reference retained
    beyond this one read pass.

    `knowledge_items` must already be KnowledgeStore.active_items() --
    this function takes the list, never the store itself, so it has no
    way to call .teach() even by mistake (the same reasoning
    services/ai_service.py's format_learned_knowledge() and
    format_official_state() already rely on).

    `historical_events` and `hamsterwatch_archive` are read via their
    existing read-only methods only (known_hoh_winners(),
    known_seasons(), count()) -- the identical calls
    production/historical_retrieval.py and services/ai_service.py
    already make elsewhere for the same stores.

    `channel_memory_count` is deliberately scoped to `channel_id` only
    -- counting across every channel (or worse, returning content)
    would leak that a *different* channel has memories at all, which
    even a bare number should never do. MemoryStore has no direct
    per-channel count method, so this reads all_items() and filters
    locally; still never touches `.content` on any item -- only counts
    matching entries.
    """

    state_topics = tuple(
        sorted(
            {
                item.topic
                for item in knowledge_items
                if item.type == KnowledgeType.STATE and item.topic
            }
        )
    )
    rule_count = sum(1 for item in knowledge_items if item.type == KnowledgeType.RULE)
    fact_count = sum(1 for item in knowledge_items if item.type == KnowledgeType.FACT)
    correction_count = sum(
        1 for item in knowledge_items if item.type == KnowledgeType.CORRECTION
    )

    known_winners = historical_events.known_hoh_winners()
    known_seasons = tuple(historical_events.known_seasons())

    article_count = hamsterwatch_archive.count() if hamsterwatch_archive is not None else 0

    live_feed_populated = bool(
        house_status.hoh
        or house_status.nominees
        or house_status.veto_holder
        or house_status.have_nots
        or competition.active
    )

    channel_memory_count = sum(
        1 for item in memory_store.all_items() if item.channel_id == channel_id
    )

    return KnowledgeSummaryMetadata(
        official_state_topics=state_topics,
        admin_rule_count=rule_count,
        admin_fact_count=fact_count,
        admin_correction_count=correction_count,
        historical_hoh_known_winners_count=len(known_winners),
        historical_hoh_seasons_known=known_seasons,
        hamsterwatch_article_count=article_count,
        live_feed_populated=live_feed_populated,
        channel_memory_count=channel_memory_count,
    )
