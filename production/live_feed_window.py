"""
Julie ChenBot Recent Live-Feed Window
========================================

Deterministic, no-AI-call parsing/selection for a conversational
"what happened recently" style question -- the additive counterpart to
production/historical_retrieval.py (verified past events) and
production/hamsterwatch_context.py (third-party recap prose), this
time over production/engine.py's ALREADY-EXISTING recent_updates(hours)
buffer of raw Joker's Updates activity. Mirrors historical_retrieval.py's
own style deliberately: small regex extractors, no AI call, no new
retrieval subsystem.

This module does NOT retrieve anything itself -- engine.recent_updates()
(untouched, unmodified) remains the single source of recent live-feed
text, including its own existing timestamp/clock-skew/dedup guarantees.
This module only decides (1) how large a window a message is actually
asking for, and (2) which of the already-retrieved entries are worth
keeping when the message names a specific player. See services/
ai_service.py's format_recent_live_feed() for how the result is
rendered into the model's context, and services/discord.py's
generate_ai_reply() for the (intentionally thin) wiring between them.

Trust boundary
----------------
Recent live-feed text is EVIDENCE, not confirmed state -- exactly the
same posture services/ai_service.py's SYSTEM_INSTRUCTION already
requires for LIVE FEED OBSERVATION (format_game_state()). Nothing in
this module ever reads or writes OFFICIAL GAME FACTS, HouseStatus, or
CompetitionState; it only selects a subset of already-retrieved,
already-unverified strings.

Deliberately NOT implemented
-------------------------------
"Since the veto" / "since I left" -style anchored windows are
intentionally NOT parsed here. Resolving "the veto" to a wall-clock
timestamp would require an authoritative "when did this official-state
value last change" record, which does not exist anywhere in this
codebase (production/knowledge.py's KnowledgeStore tracks an
updated_at per item, but that reflects when an admin last touched a
STATE topic, not when the underlying game event actually happened --
using it here would risk silently inventing a window that doesn't
match what was actually asked). Rather than guess, a message shaped
like "since X" simply falls through to parse_recent_window() returning
None, same as any other unrecognized phrasing -- see that function's
own docstring.
"""

from __future__ import annotations

import re

from production.context_budget import truncate_for_budget

# ==========================================================
# Time-window parsing
# ==========================================================

# A floor well below any realistic request (15 minutes) -- purely a
# sanity clamp so a degenerate parse (e.g. "last 0 hours") can never
# produce a zero/negative window passed to engine.recent_updates().
MIN_WINDOW_HOURS = 0.25

# Safety cap regardless of what a message asks for -- see module
# docstring's "very large windows must be bounded safely" requirement.
# engine.RECAP_LIMIT (500 buffered entries) already bounds how much
# recent_updates() can ever return in practice, but this cap keeps a
# request from claiming a window far beyond what the buffer's own
# comment ("must comfortably outlast the actual recap window" -- i.e.
# a day) is meant to reliably cover.
MAX_WINDOW_HOURS = 48.0

# "recently" / "last few hours" / a catch-up phrase with no explicit
# number -- a reasonable single-conversation-session default, smaller
# than a full day (see TODAY_WINDOW_HOURS) since these phrases read as
# "since we were last talking," not "the whole day."
DEFAULT_WINDOW_HOURS = 3.0

# "just now" -- same order of magnitude as a bare "last hour".
JUST_NOW_WINDOW_HOURS = 1.0

# "overnight" -- a reasonable predefined sleep-period window. Not
# calendar-aware (Julie doesn't reliably know the user's local time --
# see SYSTEM_INSTRUCTION's existing time-of-day caution), so this is a
# fixed approximation, not a claim about exact clock boundaries.
OVERNIGHT_WINDOW_HOURS = 8.0

# "today" -- reuses the same order of magnitude as engine.py's own
# RECAP_WINDOW_HOURS (a full day), for the same reason: a rolling
# 24-hour lookback, not a claim of calendar-day alignment.
TODAY_WINDOW_HOURS = 24.0

# "this morning" / "this afternoon" / "tonight" -- an approximate
# half-day-ish chunk. Like OVERNIGHT_WINDOW_HOURS, not calendar-aware.
PART_OF_DAY_WINDOW_HOURS = 6.0

_EXPLICIT_HOURS_PATTERN = re.compile(
    r"(?i)\blast\s+(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\b"
)
_LAST_FEW_HOURS_PATTERN = re.compile(r"(?i)\blast\s+few\s+hours?\b")
_BARE_LAST_HOUR_PATTERN = re.compile(r"(?i)\blast\s+hour\b")
_OVERNIGHT_PATTERN = re.compile(r"(?i)\bovernight\b")
_TODAY_PATTERN = re.compile(r"(?i)\btoday\b")
_PART_OF_DAY_PATTERN = re.compile(
    r"(?i)\b(?:this\s+morning|this\s+afternoon|tonight)\b"
)
_JUST_NOW_PATTERN = re.compile(r"(?i)\bjust\s+now\b")
_CATCHUP_PATTERN = re.compile(
    r"(?i)\b(?:what have i missed|what did i miss|catch me up|"
    r"what'?s been happening|what has been happening)\b"
)
_RECENTLY_PATTERN = re.compile(r"(?i)\brecently\b")


def parse_recent_window(text: str) -> float | None:
    """Best-effort, deterministic read on how many hours of recent
    live-feed activity `text` is asking for. Returns None -- never a
    guessed window -- when the message doesn't read as a recent-feed
    request at all, or names a form this module deliberately doesn't
    resolve (see module docstring's "since X" note). Checked
    most-specific-first, mirroring production/historical_retrieval.py's
    own priority style:

    1. An explicit "last N hour(s)" always wins, clamped to
       [MIN_WINDOW_HOURS, MAX_WINDOW_HOURS].
    2. "last few hours" / a bare "last hour" / "overnight" / "today" /
       a named part of day / "just now" each map to a fixed,
       documented window.
    3. A catch-up phrase ("what have I missed", "catch me up", ...) or
       a bare "recently" maps to DEFAULT_WINDOW_HOURS.
    4. Anything else returns None.
    """

    explicit = _EXPLICIT_HOURS_PATTERN.search(text)
    if explicit:
        hours = float(explicit.group(1))
        return max(MIN_WINDOW_HOURS, min(hours, MAX_WINDOW_HOURS))

    if _LAST_FEW_HOURS_PATTERN.search(text):
        return DEFAULT_WINDOW_HOURS

    if _BARE_LAST_HOUR_PATTERN.search(text):
        return 1.0

    if _OVERNIGHT_PATTERN.search(text):
        return OVERNIGHT_WINDOW_HOURS

    if _TODAY_PATTERN.search(text):
        return TODAY_WINDOW_HOURS

    if _PART_OF_DAY_PATTERN.search(text):
        return PART_OF_DAY_WINDOW_HOURS

    if _JUST_NOW_PATTERN.search(text):
        return JUST_NOW_WINDOW_HOURS

    if _CATCHUP_PATTERN.search(text) or _RECENTLY_PATTERN.search(text):
        return DEFAULT_WINDOW_HOURS

    return None


# ==========================================================
# Player/subject-specific selection -- see module docstring.
# ==========================================================

# Caps how many already-retrieved entries survive into the rendered
# prompt block, taking the MOST RECENT ones when there are more than
# this -- same reasoning as production/context_budget.py's
# MAX_RELEVANT_KNOWLEDGE_ITEMS: a bounded, predictable upper size
# rather than however many happen to fall in a wide window.
MAX_RECENT_FEED_ITEMS = 15

# A small, defensive backstop for common capitalized words that could
# otherwise look like a name candidate when they're NOT the sentence's
# own first word (already skipped separately -- see
# extract_subject_keywords()) -- e.g. a second clause opening with
# "But"/"Also", or "Julie" as a direct address. Not the primary
# filtering mechanism (capitalized-word POSITION is); just a narrow
# safety net on top of it.
_CAPITALIZED_STOPWORDS = frozenset({
    "julie", "but", "also", "and", "or", "wait", "okay", "ok", "well",
    "anything", "catch",
})


def extract_subject_keywords(text: str) -> list[str]:
    """Player-name candidates worth narrowing a recent-feed window to
    -- see select_recent_updates().

    Deliberately NOT a general keyword extractor. An earlier version
    reused production/hamsterwatch_context.py's extract_keywords()
    (the same primitive production/context_budget.py's
    select_relevant_knowledge_items() relies on), but that function's
    stopword list is tuned for Hamsterwatch archive search, not this
    use case: it let common words like "been"/"anything"/"happen"/
    "talking" through as false "subject" candidates for a message like
    "What has Devens been up to recently?" -- filtering
    recent_updates() on a word that common would match nearly every
    entry, silently defeating the whole point of narrowing.

    Real player names are reliably capitalized in a well-formed
    question -- every example in this feature's own spec is. So this
    looks for a capitalized word, skipping the message's own first
    word (English capitalizes that regardless of whether it's a name)
    and a small defensive stopword set for common capitalized words
    that can appear elsewhere in a sentence. A message typed in all
    lowercase ("what has devens been up to") simply yields no
    candidates -- select_recent_updates() then correctly falls back to
    the FULL window rather than guessing, the same "don't invent a
    match" posture as every other best-effort signal in this feature.
    This is a real, documented limitation (see module docstring), not
    a silent gap.
    """

    words = re.findall(r"[A-Za-z']+", text)
    if len(words) <= 1:
        return []

    return [
        word.lower()
        for word in words[1:]  # skip the sentence's own first word
        if len(word) >= 3
        and word[0].isupper()
        and word.lower() not in _CAPITALIZED_STOPWORDS
    ]


def select_recent_updates(
    updates: list[str], user_text: str
) -> tuple[list[str], list[str]]:
    """Applies subject-keyword narrowing (if any) and the item-count
    bound to `updates` -- already time-windowed by the caller via
    engine.recent_updates(hours=...), untouched here.

    Returns (selected_updates, subject_keywords_used). subject_keywords_used
    is empty whenever no narrowing was actually applied -- either no
    real subject keyword was found in `user_text` (a generic "what
    happened?"-style question), or narrowing to the extracted
    keyword(s) would have produced ZERO matches. The latter case
    deliberately falls back to the FULL window rather than reporting
    an empty, player-specific result: a keyword-matching miss is not
    proof nothing relevant happened (see module docstring's trust-
    boundary note and services/ai_service.py's format_recent_live_feed()
    for how an outright empty window is still handled honestly).
    """

    keywords = extract_subject_keywords(user_text)

    if keywords:
        filtered = [
            update for update in updates
            if any(keyword in update.lower() for keyword in keywords)
        ]
        if filtered:
            return filtered[-MAX_RECENT_FEED_ITEMS:], keywords

    return updates[-MAX_RECENT_FEED_ITEMS:], []
