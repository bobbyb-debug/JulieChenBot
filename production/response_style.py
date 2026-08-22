"""
Julie ChenBot Response Style / Hosting Guidance
=================================================

Deterministic, per-turn guidance for HOW Julie should host a reply --
never WHAT is true. This is the presentation/reasoning layer that
sits between the assembled facts (OFFICIAL GAME FACTS, ADMINISTRATOR-
TAUGHT KNOWLEDGE, HISTORICAL SEASON CONTEXT, LIVE FEED OBSERVATION,
REMEMBERED CONTEXT -- all built elsewhere, see services/ai_service.py)
and the model's own stylistic execution:

    INFORMATION -> SOURCE/TRUST PRIORITY -> REASONING
        -> RESPONSE INTENT (this module) -> PERSONALITY/HOSTING STYLE
        -> FINAL RESPONSE

This module owns exactly two decisions, both made from information
services/ai_service.py's generate_julie_response() already has in
hand -- no new database, no new AI call, and no competing memory
system (see build_response_guidance()'s parameters):

    1. Response intent -- a rough, deterministic read on what KIND of
       reply a question calls for (a quick fact, a story, a dramatic
       moment, banter, or ordinary conversation), so the model isn't
       nudged toward the same response shape for every message.

    2. Conversational rhythm -- whether this looks like the start of
       a conversation (a brief greeting is fine) or a continuation
       (it isn't), and whether Julie's own last reply already used a
       catchphrase worth varying away from this time.

Both are rendered into one small, clearly-labeled instruction block
by services/ai_service.py's format_response_guidance() -- never a
source of facts, and never itself something to read back to the
Houseguest. The actual wording of any given reply is still entirely
the model's job; this module only decides what KIND of reply fits,
never supplies canned phrases for it to pick from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# A rapid back-and-forth clearly shouldn't re-trigger a greeting, but
# resuming after a real gap (a different session, the next day)
# reasonably reads as a fresh start again. 20 minutes is a judgment
# call, not a precise measurement -- comfortably longer than a normal
# multi-question exchange, comfortably shorter than "a different
# conversation entirely."
GREETING_GAP_MINUTES = 20


class ResponseIntent(str, Enum):
    """A rough, deterministic read on what kind of reply a message
    calls for. Not exhaustive and not mutually authoritative -- just
    enough internal structure that Julie isn't nudged toward the same
    response shape for a one-word fact lookup and a season-defining
    twist. See classify_intent() for how (and in what priority) these
    are chosen.
    """

    DIRECT_FACT = "DIRECT_FACT"
    HISTORICAL = "HISTORICAL"
    DRAMATIC = "DRAMATIC"
    BANTER = "BANTER"
    GENERAL = "GENERAL"


# Message FORM, not content, is what marks a reaction/comment --
# "lol that nomination is wild" is reacting to something, not asking
# a question, even though it mentions a real game term.
_REACTION_STARTERS = (
    "lol", "lmao", "lmfao", "omg", "wow", "damn", "haha", "hahaha",
    "bruh", "yo", "wtf", "no way", "holy",
)

# Deliberately specific, high-signal Big Brother terms for a genuine
# swing in the game -- not every mention of "veto" or "nominee" (that
# territory is ordinary/DIRECT_FACT), just the moments a real host
# would visibly react to.
_DRAMATIC_TERMS = (
    "diamond power of veto", "dpov", "blindside", "backdoor",
    "battle back", "self-evict", "self evict", "expelled", "removed",
    "instant eviction", "double eviction", "triple eviction",
    "special power", "secret power", "coup d'etat", "coup detat",
)

# Language marking a question as being about the past rather than
# right now. Doesn't need to be exhaustive: retrieved HISTORICAL
# SEASON CONTEXT being non-empty for this turn (passed separately, see
# classify_intent()) is the stronger, primary signal -- this list only
# catches historical-shaped questions that happened to retrieve
# nothing (e.g. an ordinary factual question about a moment far enough
# back that Hamsterwatch has no matching entry). Deliberately does NOT
# include a bare "day" check -- "how's your day going" is not a
# historical question, and a real "Day 12"-style reference is caught
# precisely by _DAY_REFERENCE below instead (the same pattern
# production/hamsterwatch_context.py's own extract_bb_day() uses).
_HISTORICAL_SIGNAL_WORDS = (
    "earlier", "before", "used to", "previously", "back when",
    "last week", "during",
)

_DAY_REFERENCE = re.compile(r"(?i)\bday\s+\d+\b")

# A short, direct lookup for one specific piece of current game state
# -- the shape of question that deserves a one-to-three-sentence
# answer, not a hosting monologue.
_DIRECT_FACT_PATTERN = re.compile(
    r"(?i)^\s*(who|what|when|is|are|does|has)\b.{0,40}\b"
    r"(hoh|head of household|nominee|nominees|nominated|veto|pov|"
    r"power of veto|have-?nots?|evicted|eviction)\b"
)

# Catchphrase markers checked against Julie's own most recent reply
# (not every use of "Houseguests", which SYSTEM_INSTRUCTION already
# encourages as a normal address term) -- specifically the fixed
# openers that turned into a mandatory-feeling template.
_CATCHPHRASE_MARKERS = (
    "expect the unexpected",
    "good evening, houseguest",
    "good morning, houseguest",
    "good afternoon, houseguest",
)


def classify_intent(user_text: str, historical_context: str = "") -> ResponseIntent:
    """Best-effort, deterministic read on what kind of reply this
    message calls for. Checked most-specific-first:

    1. A reaction/comment (no question mark, casual opener) is
       BANTER regardless of whether it happens to mention game terms.
    2. A genuinely big-swing game term (DPOV, blindside, backdoor...)
       anywhere in the question or the retrieved historical material
       makes it DRAMATIC.
    3. Retrieved historical material, or historical-shaped language in
       the question itself, makes it HISTORICAL.
    4. A short, direct lookup for one specific current-state topic is
       DIRECT_FACT.
    5. Everything else is GENERAL -- ordinary conversation, no
       special-cased shape.
    """

    text = user_text.strip()
    lowered = text.lower()

    if "?" not in text and lowered.startswith(_REACTION_STARTERS):
        return ResponseIntent.BANTER

    combined_for_drama = lowered + " " + historical_context.lower()
    if any(term in combined_for_drama for term in _DRAMATIC_TERMS):
        return ResponseIntent.DRAMATIC

    if (
        historical_context
        or any(word in lowered for word in _HISTORICAL_SIGNAL_WORDS)
        or _DAY_REFERENCE.search(text)
    ):
        return ResponseIntent.HISTORICAL

    if _DIRECT_FACT_PATTERN.match(text):
        return ResponseIntent.DIRECT_FACT

    return ResponseIntent.GENERAL


def _last_model_reply(history: list[tuple[str, str, str | None]]) -> str | None:
    for role, text, _author in reversed(history):
        if role == "model":
            return text
    return None


@dataclass(slots=True)
class ResponseGuidance:
    """What build_response_guidance() decided for this one reply.
    Presentation-only -- never contains and never implies a game
    fact."""

    intent: ResponseIntent
    is_conversation_start: bool
    recently_used_phrases: list[str] = field(default_factory=list)


def build_response_guidance(
    user_text: str,
    *,
    historical_context: str = "",
    history: list[tuple[str, str, str | None]] | None = None,
    minutes_since_last_message: float | None = None,
) -> ResponseGuidance:
    """Builds this turn's hosting guidance from information
    generate_julie_response() already has in hand.

    `history` is the exact same (role, text, author_name) list already
    fetched for the model itself (see services/ai_service.py
    update_and_get_history()) -- including the just-appended current
    user turn as its last entry, so only entries before that are
    examined here. `minutes_since_last_message` should reflect the
    channel's activity BEFORE this message was appended (see
    services/ai_service.py _minutes_since_last_message()) -- None
    means there was no prior activity at all.
    """

    intent = classify_intent(user_text, historical_context)

    is_conversation_start = (
        minutes_since_last_message is None
        or minutes_since_last_message >= GREETING_GAP_MINUTES
    )

    prior_history = (history or [])[:-1]
    last_reply = _last_model_reply(prior_history)

    recently_used: list[str] = []
    if last_reply:
        lowered_reply = last_reply.lower()
        recently_used = [
            marker for marker in _CATCHPHRASE_MARKERS if marker in lowered_reply
        ]

    return ResponseGuidance(
        intent=intent,
        is_conversation_start=is_conversation_start,
        recently_used_phrases=recently_used,
    )
