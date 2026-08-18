"""
Julie ChenBot Manual State -> Structured Game State Bridge
=============================================================

Maps a moderator's /teach update (or /teach batch STATE: line) topic
onto the SAME HouseStatus object /hoh, /noms, /nominees, and /veto
already read (production/house_status.py) -- so a manual current-state
update takes effect immediately for those commands, with no second,
competing game-state store.

This module does not persist anything, does not touch Discord, and
does not touch KnowledgeStore -- it only knows how to turn one
(topic, value) pair into a replacement HouseStatus. Persisting the
result (production/engine.py ProductionEngine._persist_game_state())
and recording provenance (production/knowledge.py KnowledgeStore) are
the caller's job -- see commands/teach.py's /teach update.
"""

from __future__ import annotations

from dataclasses import replace

from production.house_status import HouseStatus

# Canonical topic names that map to a real HouseStatus field. A STATE
# item can be taught for any other topic too (see production/
# knowledge.py) -- it's still recorded as knowledge/audit history,
# it just doesn't affect any structured command like /hoh.
RECOGNIZED_TOPICS = ("HOH", "NOMINEES", "VETO_WINNER", "HAVE_NOTS")


def is_recognized_topic(topic: str) -> bool:
    return topic.strip().upper() in RECOGNIZED_TOPICS


def _split_names(value: str) -> tuple[str, ...]:
    """Splits a comma- and/or "and"-separated list of names (e.g.
    "Angela, Dee" or "Angela and Dee") into a tuple, trimming
    whitespace and dropping empty entries."""

    normalized = value.replace(" and ", ",").replace(" & ", ",")
    return tuple(part.strip() for part in normalized.split(",") if part.strip())


def apply_state_topic(topic: str, value: str, current: HouseStatus) -> HouseStatus:
    """Returns a new HouseStatus with `topic` set to `value`.

    Returns `current` unchanged if `topic` isn't recognized -- callers
    should check is_recognized_topic() first if they need to warn a
    moderator that a topic won't affect any structured command;
    this function itself degrades safely either way.

    A VETO_WINNER update also resets veto_used to False: a freshly
    manually-set veto winner has not, by definition, been used yet.
    """

    normalized_topic = topic.strip().upper()

    if normalized_topic == "HOH":
        return replace(current, hoh=value.strip())

    if normalized_topic == "NOMINEES":
        return replace(current, nominees=_split_names(value))

    if normalized_topic == "VETO_WINNER":
        return replace(current, veto_holder=value.strip(), veto_used=False)

    if normalized_topic == "HAVE_NOTS":
        return replace(current, have_nots=_split_names(value))

    return current
