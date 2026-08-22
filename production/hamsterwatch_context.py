"""
Julie ChenBot Historical Context Retrieval
=============================================

Retrieves relevant Hamsterwatch archive material for a user's
question -- deterministic, FTS5-backed, no AI/embeddings involved.
See database/hamsterwatch_archive.py for the underlying store this
reads from (never writes to) and services/ai_service.py
format_historical_context() for how a result is rendered into the
model's context.

This module owns retrieval *decisions* only: which keywords to
search for, whether a query is really a specific "Day N" lookup, and
-- critically -- refusing to substitute unrelated recent material
for a targeted question that has no real match. It never mutates
KnowledgeStore, HouseStatus, or CompetitionState, and never calls an
AI provider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from database.hamsterwatch_archive import ArchivedArticle, HamsterwatchArchive

# Small and fixed on purpose: this rides on every qualifying /chat
# reply's prompt, so it stays a background-context budget, not a
# research dump. A full-day deep dive or a season timeline is
# deliberately out of scope for this feature (see the module
# docstring above).
DEFAULT_LIMIT = 3

# Same deterministic, no-AI technique already proven for an analogous
# retrieval problem -- see production/memory.py MemoryStore.recall(),
# which this list is deliberately a near-duplicate of, rather than a
# new algorithm invented for this feature. Includes ordinary
# conversational filler ("lol", "thanks", "good morning", "tell me a
# joke") so that chatting with Julie about nothing in particular never
# extracts a "keyword" by accident -- see extract_keywords()'s
# docstring and _VAGUE_CONTEXT_SIGNALS below for how this and a vague-
# but-clearly-BB question are told apart.
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "are", "was", "were", "you", "your",
        "what", "when", "where", "who", "why", "how", "did", "does",
        "with", "that", "this", "have", "has", "had", "not", "but",
        "julie", "please", "can", "could", "would", "should", "about",
        "happened", "happening", "going", "before", "during", "around",
        "between", "there", "here",
        "lol", "thanks", "thank", "good", "morning", "think", "funny",
        "tell", "joke", "hey", "hi", "hello", "yeah", "okay", "cool",
    }
)

# Words that signal "this is a general question about the show" but
# are too generic on their own to be useful FTS5 search terms -- "the
# house" and "this week" describe every single archived entry, so
# searching on them literally would rank essentially at random rather
# than by relevance. Excluded from extract_keywords() for that reason,
# but checked separately by retrieve_historical_context() to decide
# whether a keyword-free query still deserves recent background
# (a genuine "what's going on in the house?") versus nothing at all
# (ordinary chatter with no BB content at all, e.g. "lol").
_VAGUE_CONTEXT_SIGNALS = frozenset(
    {"house", "season", "week", "lately", "recently", "game"}
)

_DAY_REFERENCE = re.compile(r"(?i)\bday\s+(\d+)\b")

_WORD = re.compile(r"[a-z0-9']+")


def _normalize(word: str) -> str:
    return word[:-2] if word.endswith("'s") else word


def extract_keywords(query: str) -> list[str]:
    """Turns free text into search keywords: lowercase, tokenize,
    drop stopwords and very short tokens.

    Deliberately the same shape as MemoryStore.recall()'s own keyword
    extraction (production/memory.py) -- not a new algorithm, and not
    an AI/embedding call. One small addition beyond a plain stopword
    lookup: a trailing "'s" contraction/possessive ("what's", "who's")
    is also checked against the stopword list with the suffix
    stripped, so a purely conversational "what's going on?" is
    correctly recognized as having no real search terms -- without
    that check, "what's" survives as a literal, non-stopword token
    and gets treated as a keyword worth searching for, which it
    is not. The original token (apostrophe intact) is still what
    gets returned/searched on when it's NOT a stopword, so a real
    possessive name (e.g. "O'Connell") is unaffected.

    Also drops _VAGUE_CONTEXT_SIGNALS words ("house", "week", ...):
    they're real signal that the question is about the show, but too
    generic to search FTS5 on directly -- see that set's own comment.
    A query left with no real keywords after both filters may still
    get recent background via retrieve_historical_context()'s vague-
    context-signal check; it just doesn't get a direct keyword search.
    """

    keywords = []
    for word in _WORD.findall(query.lower()):
        if len(word) <= 2:
            continue
        bare = _normalize(word)
        if word in _STOPWORDS or bare in _STOPWORDS:
            continue
        if word in _VAGUE_CONTEXT_SIGNALS or bare in _VAGUE_CONTEXT_SIGNALS:
            continue
        keywords.append(word)
    return keywords


def _has_vague_context_signal(query: str) -> bool:
    """True when the query contains no real search keyword but still
    names something clearly BB/season-related ("the house", "this
    week", "lately") -- the signal retrieve_historical_context() uses
    to decide a keyword-free question still deserves recent background
    rather than nothing at all. See _VAGUE_CONTEXT_SIGNALS.
    """

    for word in _WORD.findall(query.lower()):
        bare = _normalize(word)
        if word in _VAGUE_CONTEXT_SIGNALS or bare in _VAGUE_CONTEXT_SIGNALS:
            return True
    return False


def extract_bb_day(query: str) -> int | None:
    """Detects an explicit Big Brother day reference ("Day 12", "day
    37") in free text -- a stronger, more precise signal than keyword
    search for that exact question shape. Returns None when no such
    reference is present.
    """

    match = _DAY_REFERENCE.search(query)
    return int(match.group(1)) if match else None


@dataclass(slots=True)
class HistoricalContextResult:
    """What retrieve_historical_context() found, and how it found it.

    Carrying `matched_bb_day` (rather than just a bare list) is what
    lets format_historical_context() decide whether to render full
    recap content (an explicit, precise Day-N lookup -- usually one
    section, and literally what was asked for) or a bounded summary
    per entry (a keyword/recency result, potentially several
    unrelated days, where prompt size must stay bounded).
    """

    articles: list[ArchivedArticle] = field(default_factory=list)
    matched_bb_day: int | None = None

    def __bool__(self) -> bool:
        return bool(self.articles)


def retrieve_historical_context(
    query: str,
    archive: HamsterwatchArchive,
    *,
    limit: int = DEFAULT_LIMIT,
) -> HistoricalContextResult:
    """Retrieves Hamsterwatch material relevant to a user's question.

    Four cases, in priority order:

    1. The query explicitly names a BB day ("what happened on Day
       12?") -- returns that day's section(s) directly via
       archive.by_bb_day(), the most precise retrieval available,
       regardless of keyword overlap. If nothing was archived for
       that day, returns an empty result -- it never substitutes a
       different day's material for the one actually asked about.

    2. The query yields real keywords -- returns archive.search()
       results only. A genuine miss (no FTS5 match at all) returns
       an empty result; this deliberately does NOT fall back to
       archive.find_relevant()'s recency-backfill behavior, which
       would hand the model unrelated recent material for a targeted
       question it could not actually answer. find_relevant() is
       still exactly right for /recap's "what's been happening"
       framing -- this function is for a different, more targeted
       question shape and intentionally behaves differently.

    3. The query yields no real keywords, but still clearly names the
       show in general terms ("what's going on in the house?", "what
       happened lately?", "what's happening this week?" -- see
       _VAGUE_CONTEXT_SIGNALS) -- falls back to the most recent
       entries as general background, since the question is plainly
       about the season even though it's too vague to search on.

    4. The query yields no real keywords and no such signal either
       (ordinary chatter -- "lol", "thanks Julie", "good morning",
       "what do you think?", "that's funny", "tell me a joke") --
       returns an empty result. We deliberately do NOT hand random
       Hamsterwatch material to every message with no BB content in
       it; recent() is reserved for questions actually about the show.

    Read-only: never mutates the archive, KnowledgeStore, HouseStatus,
    or CompetitionState, and never calls an AI provider.
    """

    bb_day = extract_bb_day(query)
    if bb_day is not None:
        articles = archive.by_bb_day(bb_day)[:limit]
        return HistoricalContextResult(
            articles=articles, matched_bb_day=bb_day
        )

    keywords = extract_keywords(query)
    if keywords:
        matches = archive.search(keywords, limit=limit)
        return HistoricalContextResult(articles=matches)

    if _has_vague_context_signal(query):
        return HistoricalContextResult(articles=archive.recent(limit=limit))

    return HistoricalContextResult(articles=[])
