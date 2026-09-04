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


def _split_taught_names(value: str) -> list[str]:
    """Splits a taught multi-name STATE value ("Devens, LaLa, Taylor")
    into individual names -- the exact inverse of admin_api/
    conflicts.py house_status_value()'s ", ".join(...) formatting, so
    a value taught in the same shape /teach update's own preview shows
    round-trips back into the same tuple HouseStatus expects."""

    return [name.strip() for name in value.split(",") if name.strip()]


def sync_house_status_from_knowledge(
    house_status: HouseStatus, knowledge: "KnowledgeStore"
) -> HouseStatus:
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

    Pure function: never mutates `house_status` or `knowledge` --
    `knowledge.active_state()` is a read-only lookup, and this returns
    a new HouseStatus rather than assigning into anything. The caller
    (production/engine.py ProductionEngine.reconcile_game_state_from_knowledge())
    owns deciding what to do with the result and any persistence.
    """

    fields = house_status.to_dict()

    for topic, field in _STRING_TOPIC_FIELDS.items():
        item = knowledge.active_state(topic)
        if item is None:
            continue
        value = item.content.strip()
        fields[field] = "" if value.lower() in _UNCONFIRMED_VALUES else value

    for topic, field in _LIST_TOPIC_FIELDS.items():
        item = knowledge.active_state(topic)
        if item is None:
            continue
        value = item.content.strip()
        fields[field] = (
            [] if value.lower() in _UNCONFIRMED_VALUES else _split_taught_names(value)
        )

    for topic, field in _BOOL_TOPIC_FIELDS.items():
        item = knowledge.active_state(topic)
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

    return HouseStatus.from_dict(fields)
