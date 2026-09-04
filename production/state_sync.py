"""
Julie ChenBot Official-Topic Registry
=========================================

Identifies which taught STATE topics (production/knowledge.py
KnowledgeStore) have a directly comparable field on the automated,
live-feed-driven HouseStatus (production/house_status.py) -- used by
the admin API's conflict detector (admin_api/conflicts.py) to know
which topics can be diffed between the two, and by /teach update's
preview to warn a moderator when a topic they're setting won't be one
of the ones /hoh, /noms, /nominees, or /veto specifically read.

IMPORTANT: this module does NOT write to HouseStatus on its own
initiative. Official game facts (HOH, nominees, POV winner, have-nots,
veto usage, and anything else an admin decides to track) live
exclusively in KnowledgeStore STATE items -- see production/
knowledge.py KnowledgeStore.active_state(). HouseStatus remains a
separate, non-authoritative, automated live-feed observation, updated
moment-to-moment only by production/engine.py's RSS pipeline.

This module owns exactly ONE additional, explicitly one-directional
capability beyond the read-only conflict registry below:
sync_house_status_from_knowledge() re-derives HouseStatus's recognized
fields FROM already-confirmed Knowledge State. This exists to fix a
real production incident: Knowledge State and the separately-persisted
Engine game_state (production/engine.py GAME_STATE_KEY) could diverge
-- a moderator confirms HOH=Drew via /teach update, but the engine's
own persisted HouseStatus snapshot (built independently by the RSS
parser, possibly stale or malformed -- see production/parser.py) keeps
reporting something else, including surviving process restarts via
_load_game_state(). See production/engine.py
ProductionEngine.reconcile_game_state_from_knowledge() for where this
is actually called (both right after a moderator confirms /teach
update, and once at every process startup).

Data flows in exactly one direction here: Knowledge -> HouseStatus,
never the reverse. sync_house_status_from_knowledge() only ever READS
KnowledgeStore and never writes to it -- an automated live-feed
observation can never become (or influence) an official fact merely
because reconciliation ran, which is the same "no feedback loop"
guarantee /teach update's write path already relied on (see
commands/teach.py _StateUpdateConfirmView's own docstring).

Topic aliasing
----------------
Forensic inspection of real production Knowledge found independently-
active duplicate topic keys for the same real-world fact -- "BB
BLOCKBUSTER" alongside "BB_BLOCKBUSTER", "VETO WINNER" alongside
"VETO_WINNER". Root cause, confirmed directly in KnowledgeStore.
active_state(): topic lookup normalizes ONLY via `.strip().upper()` --
it never collapses a space/underscore difference, so two spellings of
what a human considers the same topic are, to that method, two
genuinely different topics that can both be independently active at
once.

This is NOT fixed by changing KnowledgeStore or by deleting/merging
any existing Knowledge entry -- see this module's own "does NOT write"
guarantee above, which extends to never touching Knowledge at all,
destructively or otherwise. Instead, _resolve_active_state() below
gives sync_house_status_from_knowledge() its own alias-aware lookup:
_TOPIC_ALIASES lists every spelling variant known to exist for each
canonical sync-relevant topic, and a lookup checks all of them. When
every active variant agrees (or only one is active), that value is
used, exactly as if there had been no alias to begin with. When active
variants genuinely DISAGREE, this never guesses which one is right --
see sync_house_status_from_knowledge()'s own docstring for exactly
what happens instead (the field is left untouched, and the conflict is
returned to the caller to log, not silently resolved).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from production.house_status import HouseStatus

if TYPE_CHECKING:
    from production.knowledge import KnowledgeStore

# Topics with a directly comparable field on HouseStatus (see
# admin_api/conflicts.py house_status_value()). A STATE item can be
# taught for any other topic too (e.g. "EVICTED", "VETO_USED") -- it
# is still recorded as an official fact, it simply has no automated
# equivalent to compare against for conflict detection.
RECOGNIZED_TOPICS = ("HOH", "NOMINEES", "VETO_WINNER", "HAVE_NOTS")


def is_recognized_topic(topic: str) -> bool:
    return topic.strip().upper() in RECOGNIZED_TOPICS


# ==========================================================
# Knowledge -> HouseStatus synchronization (write direction: one-way).
# ==========================================================

# Deliberately a SUPERSET of RECOGNIZED_TOPICS above, not the same
# constant: RECOGNIZED_TOPICS feeds admin_api/conflicts.py's read-only
# UI, which compares each topic as free text -- a boolean field like
# VETO_USED has no natural "compare as text" role there and was never
# added. It IS a real, well-defined HouseStatus field, though, so it
# belongs in this table. Keeping the two constants separate means
# extending sync coverage here can never silently change what the
# conflict-detection dashboard displays, and vice versa.
_STRING_TOPIC_FIELDS = {
    "HOH": "hoh",
    "VETO_WINNER": "veto_holder",
}
_LIST_TOPIC_FIELDS = {
    "NOMINEES": "nominees",
    "HAVE_NOTS": "have_nots",
}
_BOOL_TOPIC_FIELDS = {
    "VETO_USED": "veto_used",
}

# Free-text values a moderator might teach for a topic that genuinely
# isn't confirmed yet -- these must never become a fabricated engine
# value (e.g. a one-item nominee list literally containing the word
# "unconfirmed" as if it were a houseguest). A string/list field with
# an unconfirmed taught value is reset to its neutral empty default
# (""/[]) rather than left at whatever HouseStatus happened to already
# hold -- stale data must not keep presenting itself as current merely
# because Knowledge stopped affirming it (see this module's docstring
# and the Authority Rules this exists to uphold).
_UNCONFIRMED_VALUES = frozenset({
    "unconfirmed", "unknown", "tbd", "pending", "none", "n/a", "",
})

_TRUE_VALUES = frozenset({"yes", "true", "y"})
_FALSE_VALUES = frozenset({"no", "false", "n"})

# Every spelling variant currently known to exist in production
# Knowledge for the same real-world fact -- see this module's
# docstring's "Topic aliasing" section for the forensic finding this
# closes. A topic not listed here (HOH, NOMINEES today) has no known
# alternate spelling in production, so it maps to itself only; adding
# a variant here later is the entire fix if another one is ever found
# -- no other code changes needed.
_TOPIC_ALIASES: dict[str, tuple[str, ...]] = {
    "HOH": ("HOH",),
    "NOMINEES": ("NOMINEES",),
    "VETO_WINNER": ("VETO_WINNER", "VETO WINNER"),
    "VETO_USED": ("VETO_USED", "VETO USED"),
    "HAVE_NOTS": ("HAVE_NOTS", "HAVE NOTS"),
}


def _resolve_active_state(
    knowledge: "KnowledgeStore", canonical_topic: str
):
    """Alias-aware counterpart to knowledge.active_state(): checks
    every spelling variant of `canonical_topic` (_TOPIC_ALIASES above)
    and returns (item_or_None, had_conflict).

    - No variant has an active item: (None, False) -- exactly like a
      plain active_state() miss; the caller leaves the field untouched.
    - One variant is active, or several are active but all AGREE
      (case/whitespace-insensitive content match): (that item, False).
      When more than one agrees, the most recently updated is returned
      -- the same "freshest wins" behavior a single active_state()
      topic already has over time via teach()'s auto-supersede.
    - Several variants are active with genuinely DIFFERENT content:
      (None, True) -- a real conflict. Never guessed at; the caller
      treats this like "nothing taught" for the field itself, but logs
      it rather than resolving it in silence (see
      sync_house_status_from_knowledge()'s docstring).

    Read-only: active_state() never mutates KnowledgeStore.
    """

    variants = _TOPIC_ALIASES.get(canonical_topic, (canonical_topic,))

    found = []
    checked: set[str] = set()
    for variant in variants:
        normalized = variant.strip().upper()
        if normalized in checked:
            continue
        checked.add(normalized)
        item = knowledge.active_state(variant)
        if item is not None:
            found.append(item)

    if not found:
        return None, False

    distinct_values = {item.content.strip().lower() for item in found}
    if len(distinct_values) > 1:
        return None, True

    return max(found, key=lambda item: item.updated_at), False


def _split_taught_names(value: str) -> list[str]:
    """Splits a taught multi-name STATE value ("Devens, LaLa, Taylor")
    into individual names -- the exact inverse of admin_api/
    conflicts.py house_status_value()'s ", ".join(...) formatting, so
    a value taught in the same shape /teach update's own preview shows
    round-trips back into the same tuple HouseStatus expects."""

    return [name.strip() for name in value.split(",") if name.strip()]


def sync_house_status_from_knowledge(
    house_status: HouseStatus, knowledge: "KnowledgeStore"
):
    """Returns a NEW HouseStatus with every recognized topic's field
    (_STRING_TOPIC_FIELDS/_LIST_TOPIC_FIELDS/_BOOL_TOPIC_FIELDS above)
    re-derived from Knowledge State's current active_state() value for
    that topic. A field is left exactly as `house_status` already had
    it only when Knowledge has NOTHING taught for that topic at all
    (active_state() returns None) -- once Knowledge has an opinion,
    recognized fields always follow it, confirmed or explicitly
    unconfirmed (see _UNCONFIRMED_VALUES).

    Every field with no defined Knowledge mapping (`feeds`, and every
    field on CompetitionState -- see this module's docstring and
    RECOGNIZED_TOPICS' own comment for exactly why those have no
    mapping) passes through completely unchanged: this function never
    invents a value Knowledge State didn't actually confirm, and never
    touches CompetitionState at all.

    Topic lookup is alias-aware (see _resolve_active_state() and this
    module's docstring's "Topic aliasing" section): when a topic has
    more than one active spelling variant in Knowledge and they
    genuinely disagree, that field is left untouched -- exactly like
    "nothing taught" -- rather than silently picking whichever variant
    happened to be found first. Returns (new_house_status,
    conflicted_topics): `conflicted_topics` lists the canonical topic
    name(s) where this happened, for the caller to log -- never raised
    as an exception, since a Knowledge data-quality issue must not be
    able to crash reconciliation or startup.

    Pure function: never mutates `house_status` or `knowledge` --
    `knowledge.active_state()` is a read-only lookup, and this returns
    a new HouseStatus rather than assigning into anything. The caller
    (production/engine.py ProductionEngine.reconcile_game_state_from_knowledge())
    owns deciding what to do with the result, any logging, and any
    persistence.
    """

    fields = house_status.to_dict()
    conflicted_topics: list[str] = []

    def _resolve(topic: str):
        item, conflict = _resolve_active_state(knowledge, topic)
        if conflict:
            conflicted_topics.append(topic)
        return item

    for topic, field in _STRING_TOPIC_FIELDS.items():
        item = _resolve(topic)
        if item is None:
            continue
        value = item.content.strip()
        fields[field] = "" if value.lower() in _UNCONFIRMED_VALUES else value

    for topic, field in _LIST_TOPIC_FIELDS.items():
        item = _resolve(topic)
        if item is None:
            continue
        value = item.content.strip()
        fields[field] = (
            [] if value.lower() in _UNCONFIRMED_VALUES else _split_taught_names(value)
        )

    for topic, field in _BOOL_TOPIC_FIELDS.items():
        item = _resolve(topic)
        if item is None:
            continue
        value = item.content.strip().lower()
        if value in _TRUE_VALUES:
            fields[field] = True
        elif value in _FALSE_VALUES:
            fields[field] = False
        # Any other free text (including an explicit "unconfirmed") is
        # genuinely ambiguous for a strict bool field with no third
        # "unknown" state -- left exactly as house_status already had
        # it rather than guessing True or False either way. See this
        # module's docstring: a documented, deliberate limitation.

    return HouseStatus.from_dict(fields), conflicted_topics
