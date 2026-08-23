"""
Julie ChenBot Historical Event Retrieval
============================================

Deterministic, no-AI-call retrieval over the structured historical
event store (database/historical_events.py) -- the structured
counterpart to production/hamsterwatch_context.py's prose retrieval.
Both exist side by side and stay separate on purpose (see that
module's docstring and services/ai_service.py's format functions):
this module answers "what does Julie verifiably know happened,"
Hamsterwatch answers "what does a source article say happened."

Season handling
-----------------
Phase 1 deliberately has no CURRENT_SEASON concept anywhere in this
application (see database/historical_events.py's module docstring for
why, and the architecture decision this implements). A query that
doesn't name a season is resolved only when it is genuinely
unambiguous: if HistoricalEventStore.known_seasons() returns exactly
one season, that season is used. If it returns more than one, this
module refuses to guess -- it returns an empty/ambiguous result rather
than silently answering from the wrong season. This is a real,
intentional limitation of Phase 1, not an oversight: a smart
current-season resolver is explicitly deferred (see the module
docstring in database/historical_events.py).

Player identity
------------------
"What happened during Taylor's HOH?"-style questions are resolved by
exact (normalized) match against HistoricalEventStore.known_hoh_winners()
-- the set of names Julie already has real, verified data for. This is
deliberately not fuzzy: a name that doesn't exactly match a known
winner (after the same trim/uppercase normalization used at write
time) is simply not recognized as a player-anchored question, and the
caller falls through to "no match" rather than guessing which
houseguest was meant.

A question can name more than one known player ("Compare Taylor's HOH
with Dee's") -- find_known_players_mentioned() (plural) matches every
one, and retrieve_hoh() merges each matched player's verified events
into a single result, so a comparison question retrieves every
relevant record rather than silently answering for only one of the
two players named.

Ambiguity policy
-------------------
When a week matches more than one game cycle (a double or triple
eviction), this module never silently picks one. retrieve_hoh()
returns every matching verified event; the caller (see
services/ai_service.py format_historical_events()) renders all of
them, clearly labeled by cycle, so the model can present the real
situation ("Week 9 was a double eviction...") instead of a guess. An
explicit ordinal ("the second HOH of Week 9") narrows this down
deterministically via extract_ordinal() below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from database.historical_events import HistoricalEvent, HistoricalEventStore

_WEEK_PATTERN = re.compile(r"(?i)\bweek\s+(\d+)\b")
_CYCLE_PATTERN = re.compile(r"(?i)\bcycle\s+(\d+)\b")

_ORDINAL_WORDS = {
    "first": 1, "1st": 1,
    "second": 2, "2nd": 2,
    "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4,
}
_ORDINAL_PATTERN = re.compile(
    r"(?i)\b(" + "|".join(re.escape(word) for word in _ORDINAL_WORDS) + r")\b"
)


def extract_week_number(text: str) -> int | None:
    match = _WEEK_PATTERN.search(text)
    return int(match.group(1)) if match else None


def extract_cycle_number(text: str) -> int | None:
    match = _CYCLE_PATTERN.search(text)
    return int(match.group(1)) if match else None


def extract_ordinal(text: str) -> int | None:
    """Detects "second"/"2nd"-style ordinal language, used to
    disambiguate which cycle of a double/triple eviction a question
    means (e.g. "the second HOH during Week 9")."""

    match = _ORDINAL_PATTERN.search(text)
    return _ORDINAL_WORDS[match.group(1).lower()] if match else None


def find_known_players_mentioned(text: str, store: HistoricalEventStore) -> list[str]:
    """Exact, non-fuzzy match against every houseguest Julie already
    has verified HOH data for -- see module docstring. Returns every
    matching normalized name (in store.known_hoh_winners()'s own
    order), the plural counterpart to find_known_player_mentioned()
    that supports a "Compare Taylor's HOH with Dee's" style question
    naming more than one known player -- a single-name lookup would
    silently return only whichever one happened to be checked first,
    dropping the other player's records from a comparison entirely."""

    known = store.known_hoh_winners()
    if not known:
        return []

    # Strip a trailing possessive ("Taylor's HOH") the same way
    # production/hamsterwatch_context.py already does for keyword
    # extraction -- without it, "Taylor's" never matches the stored,
    # possessive-free name "TAYLOR".
    words = set()
    for raw in re.findall(r"[A-Za-z']+", text):
        word = raw.upper()
        words.add(word[:-2] if word.endswith("'S") else word)

    return [name for name in known if name in words]


def find_known_player_mentioned(text: str, store: HistoricalEventStore) -> str | None:
    """Exact, non-fuzzy match against houseguests Julie already has
    verified HOH data for -- see module docstring. Returns the first
    matching normalized name (see find_known_players_mentioned() for
    the plural, comparison-question-aware version), or None if no
    known winner's name appears as a whole word in the text."""

    matches = find_known_players_mentioned(text, store)
    return matches[0] if matches else None


def _resolve_unambiguous_season(store: HistoricalEventStore) -> int | None:
    """Returns the single season with any recorded data, or None if
    zero or more than one season is present -- see module docstring.
    None means "do not guess," not "no data.\""""

    seasons = store.known_seasons()
    return seasons[0] if len(seasons) == 1 else None


@dataclass(slots=True)
class HistoricalHohResult:
    """What retrieve_hoh() found. `events` may hold more than one
    entry when a week resolves to a double/triple eviction and no
    ordinal narrowed it down -- see module docstring's ambiguity
    policy. Never contains an unverified record (see
    HistoricalEventStore's read methods this module exclusively
    uses)."""

    events: list[HistoricalEvent] = field(default_factory=list)
    matched_week: int | None = None
    multiple_cycles: bool = False
    season_ambiguous: bool = False

    def __bool__(self) -> bool:
        return bool(self.events)


def retrieve_hoh(user_text: str, store: HistoricalEventStore) -> HistoricalHohResult:
    """Deterministically routes a question to the right structured
    HOH lookup. Priority order:

    1. Explicit "Cycle N" reference -> exact cycle lookup (requires an
       unambiguous season; see _resolve_unambiguous_season()).
    2. "Week N" + an ordinal ("the second HOH...") -> resolves to the
       Nth cycle sharing that week, by ascending cycle_sequence_number.
    3. "Week N" alone -> every verified HOH for cycles sharing that
       week. Never picks one when more than one exists.
    4. One or more known HOH winners' names mentioned -> every
       verified cycle each of them won, across the whole store (not
       season-scoped -- a player-anchored question doesn't need season
       disambiguation the way a week-anchored one does, since cycle
       ids are already globally unique). More than one name (e.g.
       "Compare Taylor's HOH with Dee's") returns every matched
       player's events together -- see find_known_players_mentioned().
    5. None of the above -> an empty result. Never a guess.
    """

    season = _resolve_unambiguous_season(store)
    seasons_known = store.known_seasons()

    cycle_number = extract_cycle_number(user_text)
    if cycle_number is not None:
        if season is None:
            return HistoricalHohResult(season_ambiguous=bool(seasons_known))
        event = store.verified_hoh_for_cycle(
            season=season, cycle_sequence_number=cycle_number
        )
        return HistoricalHohResult(events=[event] if event else [])

    week_number = extract_week_number(user_text)
    if week_number is not None:
        if season is None:
            return HistoricalHohResult(season_ambiguous=bool(seasons_known))

        events = store.verified_hoh_for_week(season=season, week_number=week_number)
        ordinal = extract_ordinal(user_text)

        if ordinal is not None and events:
            index = ordinal - 1
            if 0 <= index < len(events):
                return HistoricalHohResult(
                    events=[events[index]], matched_week=week_number
                )
            return HistoricalHohResult(matched_week=week_number)

        return HistoricalHohResult(
            events=events,
            matched_week=week_number,
            multiple_cycles=len(events) > 1,
        )

    players = find_known_players_mentioned(user_text, store)
    if players:
        # Every event self-identifies its own winner (see
        # services/ai_service.py format_historical_events(), which
        # renders each entry's "HOH winner: <name>" independently) --
        # merging more than one player's events into one list needs no
        # special grouping/labeling for a comparison question like
        # "Compare Taylor's HOH with Dee's" to read unambiguously.
        events = [
            event for player in players for event in store.verified_hoh_for_player(player)
        ]
        return HistoricalHohResult(events=events, multiple_cycles=len(events) > 1)

    return HistoricalHohResult()
