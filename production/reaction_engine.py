"""
Julie ChenBot Situational Reaction Engine
==========================================

Deterministic, per-turn read on HOW SIGNIFICANT this moment is and HOW
Julie is being engaged with -- never WHAT is true, and never a second
fact source. Sits alongside production/response_style.py (which
already owns "what kind of reply is this" and "does a greeting fit")
as a second, independent presentation-layer signal:

    INFORMATION -> SOURCE/TRUST PRIORITY -> REASONING
        -> RESPONSE INTENT (response_style.py)
        -> SITUATIONAL REACTION (this module)
        -> PERSONALITY/HOSTING STYLE -> FINAL RESPONSE

Why a second module instead of folding this into response_style.py:
response_style.py's ResponseIntent answers "what shape should this
reply take" (a quick fact vs. a story vs. banter). This module answers
a genuinely different question -- "how big a deal is this, and is the
user joking with Julie, teasing her, or challenging something she
said" -- and keeping them separate means a future change to one's
keyword set/scoring can never accidentally change the other's
classification. Both are still just deterministic, no-AI-call reads
over already-available text; the actual reaction (tone, wit, opinion,
whether to change her mind) is always the model's job -- this module
only decides what's worth reacting to and how strongly, exactly the
same division of labor response_style.py already uses.

Deliberately NOT everything the personality spec describes is
classified here. Two categories from that spec are intentionally left
as standing instructions in services/ai_service.py's guidance text
instead of a keyword classifier, because no deterministic check could
do them justice without producing more false positives than value:

    - "the user made a good observation" -- judging whether a
      Houseguest's remark was actually insightful requires real
      language understanding, not a keyword list. Handled instead by
      an always-present instruction to give credit when it's earned,
      which costs nothing on a turn where it doesn't apply.
    - "the user is wrong" / "unknown information" -- these are
      already covered by SYSTEM_INSTRUCTION's existing official-facts
      boundary paragraph and its epistemic-labeling addition; no
      per-turn classification adds anything a standing rule doesn't
      already cover.

This keeps the classifier small and honest about what it can reliably
detect, per this feature's own "don't overengineer" requirement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# ==========================================================
# Situational event -- "what kind of Big Brother moment is this",
# read from the user's own message text. Single-valued, most-
# significant-first priority (same pattern as response_style.py's
# classify_intent()) -- a message can only carry one baseline
# intensity, so ties are broken by which category would most change
# how Julie should react if missed.
# ==========================================================


class SituationalEvent(str, Enum):
    """A rough, deterministic read on what TYPE of game moment (if
    any) this message is about. Not exhaustive -- see module
    docstring -- and not a fact source: this only decides which
    reaction-guidance paragraph fits, never whether the underlying
    event is actually confirmed (see production/hamsterwatch_context.py
    and production/historical_retrieval.py for the systems that
    actually answer that)."""

    BLINDSIDE = "BLINDSIDE"
    ALLIANCE_EXPOSED = "ALLIANCE_EXPOSED"
    EVICTION = "EVICTION"
    SUSPECTED_LYING = "SUSPECTED_LYING"
    HG_CONFLICT = "HG_CONFLICT"
    SPIRALING = "SPIRALING"
    DUMB_MOVE = "DUMB_MOVE"
    GREAT_MOVE = "GREAT_MOVE"
    UNEXPECTED_VOTE = "UNEXPECTED_VOTE"
    POWER_SHIFT = "POWER_SHIFT"
    REPLACEMENT_NOMINEE = "REPLACEMENT_NOMINEE"
    MAJOR_NOMINATION = "MAJOR_NOMINATION"
    VETO_USED = "VETO_USED"
    COMP_WIN = "COMP_WIN"
    NONE = "NONE"


# Deliberately a starting set, not exhaustive -- easy to extend by
# adding a phrase to the right tuple, never by touching classify_event()
# itself. Ordered by category below, most-significant-first, matching
# the priority classify_event() checks in.
_BLINDSIDE_TERMS = (
    "blindside", "blindsided", "didn't see that coming",
    "no one saw that coming", "nobody saw that coming", "shocked the house",
)
_ALLIANCE_EXPOSED_TERMS = (
    "alliance exposed", "alliance was exposed", "alliance revealed",
    "outed the alliance", "found out about the alliance", "alliance is blown",
)
_EVICTION_TERMS = (
    "evicted", "eviction", "voted out", "sent home", "left the house",
    "goodbye message",
)
_SUSPECTED_LYING_TERMS = (
    "lying", "lied", "is he telling the truth", "is she telling the truth",
    "doesn't add up", "caught in a lie", "sketchy story", "story keeps changing",
)
_HG_CONFLICT_TERMS = (
    "fighting", "fight between", "argument", "arguing", "beef between",
    "at odds with", "clashed with", "blew up at",
)
_SPIRALING_TERMS = (
    "spiraling", "spiralling", "breaking down", "crying in the diary room",
    "having a hard time", "struggling emotionally", "losing it",
)
_DUMB_MOVE_TERMS = (
    "terrible move", "bad move", "dumb move", "stupid move", "huge mistake",
    "questionable move", "that was dumb", "why would he do that",
    "why would she do that", "why would they do that",
)
_GREAT_MOVE_TERMS = (
    "great move", "genius move", "brilliant move", "smart move",
    "played that well", "masterclass", "impressive move", "played it perfectly",
)
_UNEXPECTED_VOTE_TERMS = (
    "unexpected vote", "surprise vote", "flipped the vote", "vote flip",
    "voted against the house", "rogue vote",
)
_POWER_SHIFT_TERMS = (
    "power shift", "power just shifted", "changes the power",
    "new power structure", "power dynamic just changed", "flips the house",
)
_REPLACEMENT_NOMINEE_TERMS = (
    "replacement nominee", "renom", "re-nominated", "backdoor",
    "backdoored", "named as the replacement",
)
_MAJOR_NOMINATION_TERMS = (
    "shocking nomination", "surprise nomination", "put up as a pawn",
    "nominated out of nowhere",
)
_VETO_USED_TERMS = (
    "used the veto", "veto was used", "pulled him off the block",
    "pulled her off the block", "used the power of veto", "vetoed",
)
_COMP_WIN_TERMS = (
    "won veto", "won hoh", "won the competition", "won pov", "wins veto",
    "wins hoh", "competition winner", "won the endurance", "won the hoh comp",
)

# (event, keyword tuple) pairs in priority order -- checked top to
# bottom, first match wins. Biggest/rarest moments first (a blindside
# mention should never be reclassified as an ordinary comp win just
# because "won" also appears in the same sentence).
_EVENT_PRIORITY: tuple[tuple[SituationalEvent, tuple[str, ...]], ...] = (
    (SituationalEvent.BLINDSIDE, _BLINDSIDE_TERMS),
    (SituationalEvent.ALLIANCE_EXPOSED, _ALLIANCE_EXPOSED_TERMS),
    (SituationalEvent.EVICTION, _EVICTION_TERMS),
    (SituationalEvent.SUSPECTED_LYING, _SUSPECTED_LYING_TERMS),
    (SituationalEvent.HG_CONFLICT, _HG_CONFLICT_TERMS),
    (SituationalEvent.SPIRALING, _SPIRALING_TERMS),
    (SituationalEvent.DUMB_MOVE, _DUMB_MOVE_TERMS),
    (SituationalEvent.GREAT_MOVE, _GREAT_MOVE_TERMS),
    (SituationalEvent.UNEXPECTED_VOTE, _UNEXPECTED_VOTE_TERMS),
    (SituationalEvent.POWER_SHIFT, _POWER_SHIFT_TERMS),
    (SituationalEvent.REPLACEMENT_NOMINEE, _REPLACEMENT_NOMINEE_TERMS),
    (SituationalEvent.MAJOR_NOMINATION, _MAJOR_NOMINATION_TERMS),
    (SituationalEvent.VETO_USED, _VETO_USED_TERMS),
    (SituationalEvent.COMP_WIN, _COMP_WIN_TERMS),
)

# Baseline intensity (0-4) per event -- see score_intensity() for how
# this combines with the per-message excitement bonus below. Ordinary
# lookups (NONE) start at 0 so an unremarkable question never earns a
# manufactured reaction.
_EVENT_BASE_INTENSITY: dict[SituationalEvent, int] = {
    SituationalEvent.NONE: 0,
    SituationalEvent.VETO_USED: 1,
    SituationalEvent.DUMB_MOVE: 1,
    SituationalEvent.SUSPECTED_LYING: 1,
    SituationalEvent.HG_CONFLICT: 1,
    SituationalEvent.SPIRALING: 1,
    SituationalEvent.COMP_WIN: 2,
    SituationalEvent.GREAT_MOVE: 2,
    SituationalEvent.MAJOR_NOMINATION: 2,
    SituationalEvent.REPLACEMENT_NOMINEE: 2,
    SituationalEvent.UNEXPECTED_VOTE: 2,
    SituationalEvent.POWER_SHIFT: 2,
    SituationalEvent.EVICTION: 3,
    SituationalEvent.ALLIANCE_EXPOSED: 3,
    SituationalEvent.BLINDSIDE: 3,
}

MAX_INTENSITY = 4

# A handful of superlatives that push an already-significant moment to
# the rare top tier -- deliberately narrow, since LEVEL 4 is meant to
# stay rare (see score_intensity()'s docstring).
_SEASON_DEFINING_TERMS = (
    "season-defining", "season defining", "game-changing", "game changing",
    "biggest blindside", "never seen this happen", "changes everything",
)

_REPEATED_PUNCTUATION_PATTERN = re.compile(r"[!?]{2,}")
_CAPS_WORD_PATTERN = re.compile(r"\b[A-Z]{3,}\b")

# Common Big Brother acronyms are routinely written in all-caps as
# ordinary vocabulary (HOH, POV...), not as shouting -- excluded so a
# perfectly ordinary "Who won HOH?" doesn't register as excited. A
# genuine all-caps word outside this set ("WOW", "NO WAY") still
# counts.
_EXCITEMENT_CAPS_EXCLUSIONS = frozenset({
    "HOH", "POV", "DPOV", "DR", "BB", "HG", "HGS", "TV", "AI", "OK", "US",
})


def _has_excitement_markers(text: str) -> bool:
    """True if `text` carries its OWN excitement -- repeated "!"/"?",
    or an all-caps word that isn't just a routine game-term acronym.
    Used only as a bonus signal in score_intensity(); never on its own
    decides an event category."""

    if _REPEATED_PUNCTUATION_PATTERN.search(text):
        return True

    return any(
        word not in _EXCITEMENT_CAPS_EXCLUSIONS
        for word in _CAPS_WORD_PATTERN.findall(text)
    )


def classify_event(text: str) -> SituationalEvent:
    """First-match, most-significant-first read on what kind of game
    moment `text` describes. Returns SituationalEvent.NONE for
    ordinary text -- the common case, and the correct default (see
    module docstring: personality must never manufacture drama)."""

    lowered = text.lower()

    for event, terms in _EVENT_PRIORITY:
        if any(term in lowered for term in terms):
            return event

    return SituationalEvent.NONE


def score_intensity(text: str, event: SituationalEvent) -> int:
    """Deterministic 0-4 significance score. Starts from `event`'s
    baseline, adds up to +1 for the message's own excitement markers
    (repeated "!"/"?" or an ALL-CAPS word -- the user's own signal that
    THEY consider this a big deal, not Julie inventing one), and up to
    +1 more for an explicit season-defining superlative. Clamped to
    [0, MAX_INTENSITY] -- LEVEL 4 is reachable but requires both a
    significant event AND the user's own emphasis, keeping it genuinely
    rare rather than a default ceiling."""

    score = _EVENT_BASE_INTENSITY.get(event, 0)

    if _has_excitement_markers(text):
        score += 1

    lowered = text.lower()
    if any(term in lowered for term in _SEASON_DEFINING_TERMS):
        score += 1

    return max(0, min(MAX_INTENSITY, score))


# ==========================================================
# Social engagement signals -- independent of `event` above, since a
# user can ask an opinion question, joke, or push back on Julie about
# ANY event (or no event at all). Each is a cheap, best-effort
# keyword/pattern check, not a claim of perfect detection -- a missed
# case simply falls back to Julie's ordinary conversational judgment,
# never to a wrong fact (nothing here can ever produce or alter a
# fact, only which optional guidance line gets added).
# ==========================================================

_OPINION_REQUEST_PATTERN = re.compile(
    r"(?i)\b(what do you think|your take|your opinion|what'?s your read|"
    r"who played (that|it) better|who'?s in trouble|who do you trust|"
    r"would you have|was that (a )?(good|smart|bad|dumb) move|"
    r"do you think|who do you think)\b"
)

# Covers both "joking with Julie" and "teasing Julie" from the spec --
# collapsed into one flag because the desired reaction is the same
# shape either way (match the playful energy, tease back, don't get
# defensive) and both are equally approximate to detect by keyword.
#
# Deliberately narrower than an earlier draft: a pattern for "directly
# addressing Julie" (e.g. "Julie, you...") was tried and dropped after
# review -- it false-positives on a serious message aimed at Julie
# ("Julie, you have this wrong" is a real challenge, not banter).
# Laughter/kidding markers are a much more reliable signal that the
# user is actually joking, so that's all this checks for.
_BANTER_PATTERN = re.compile(
    r"(?i)\b(lol|lmao|lmfao|haha+|hehe+|jk|kidding)\b"
)

_CHALLENGE_PATTERN = re.compile(
    r"(?i)\b(you'?re wrong|that'?s wrong|that'?s not (true|right)|"
    r"i disagree|are you sure about that|actually,? (he|she|they|it)|"
    r"no,? (that'?s|you'?re))\b"
)


@dataclass(slots=True)
class ReactionContext:
    """What build_reaction_context() decided for this one reply.
    Presentation-only, exactly like response_style.py's
    ResponseGuidance -- never contains and never implies a game
    fact, and every field is derived purely from the user's own
    message text."""

    event: SituationalEvent = SituationalEvent.NONE
    intensity: int = 0
    opinion_requested: bool = False
    user_banter: bool = False
    user_challenges_julie: bool = False

    def log_line(self) -> str:
        """One-line, content-free summary safe for real production
        logs -- same posture as production/context_budget.py's
        ContextBudgetReport.log_line(): labels/counts/booleans only,
        NEVER the user's raw message text. Exists so a Discord report
        of Julie "overreacting" can be traced back to exactly which
        classification produced it, without adding a new logging
        system or exposing anything to end users -- see
        services/ai_service.py's generate_julie_response(), which logs
        this via the existing ProductionLogger right alongside its
        context-budget log line."""

        return (
            "Reaction: "
            f"event={self.event.value} "
            f"intensity={self.intensity} "
            f"opinion_requested={self.opinion_requested} "
            f"user_banter={self.user_banter} "
            f"user_challenges_julie={self.user_challenges_julie}"
        )


def build_reaction_context(user_text: str) -> ReactionContext:
    """Builds this turn's situational reaction context from the raw
    message text alone -- no store reads, no AI call, same "cheap and
    deterministic" posture as response_style.py's build_response_guidance().
    """

    event = classify_event(user_text)
    intensity = score_intensity(user_text, event)

    return ReactionContext(
        event=event,
        intensity=intensity,
        opinion_requested=bool(_OPINION_REQUEST_PATTERN.search(user_text)),
        user_banter=bool(_BANTER_PATTERN.search(user_text)),
        user_challenges_julie=bool(_CHALLENGE_PATTERN.search(user_text)),
    )
