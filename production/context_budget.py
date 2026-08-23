"""
Julie ChenBot Context Budget
================================

Deterministic token-budget management for the Groq request Julie's
chat path builds -- the fix for a real production incident: Groq's
`on_demand` service tier rejects a single request (prompt tokens PLUS
the reserved completion-token allowance) once it exceeds the model's
TPM limit, with a 413 "Request too large" error. Railway logs showed
this exact failure for openai/gpt-oss-120b:

    Request too large for model `openai/gpt-oss-120b`
    service tier `on_demand`
    TPM Limit 8000, Requested 9108   (and, moments later, Requested 9200)

Root cause traced (not guessed) by measuring the real prompt-assembly
path: `_try_groq_chat()` (see services/ai_service.py) passes
`max_completion_tokens=2000` on every call, and Groq's pre-flight TPM
check counts prompt tokens + that reserved allowance together -- so
"Requested 9108" means an estimated ~7108 prompt tokens, not 9108
alone. Two genuinely unbounded growth paths fed that: (1)
format_learned_knowledge() (services/ai_service.py) rendered EVERY
active administrator-taught KnowledgeItem with no relevance filter or
cap -- a full season's accumulated facts, unconditionally, on every
single reply; (2) conversation history was capped by MESSAGE COUNT
(CHAT_CONTEXT_MESSAGES, 24) but never by size, so a run of longer
replies (exactly what the KNOWLEDGE_SUMMARY and hosting-guidance work
now legitimately produces) could accumulate well past a few thousand
tokens on its own. Hamsterwatch/memory retrieval were already
reasonably bounded (production/hamsterwatch_context.py's DEFAULT_LIMIT
and services/ai_service.py's MAX_HISTORICAL_CONTENT_CHARS; MemoryStore
.recall()'s own limit=5 default) -- this module does not change either
of those, only adds a matching bound for the two paths that had none.

No tokenizer dependency is added: gpt-oss-120b's own tokenizer isn't
published as a lightweight importable package, and any general-purpose
tokenizer (tiktoken et al.) would estimate a DIFFERENT model's
encoding anyway -- an approximation either way. estimate_tokens() uses
the same "~4 characters per English token" rule of thumb Groq/OpenAI's
own documentation cites for rough sizing, rounded UP (never
underestimate a block's real cost), backstopped by SAFETY_MARGIN_TOKENS
below for whatever the heuristic still misses.

This module only decides HOW MUCH of each already-retrieved,
already-formatted context source survives into one prompt -- it never
retrieves anything itself, never mutates KnowledgeStore/
HistoricalEventStore/HamsterwatchArchive/MemoryStore, and a block that
doesn't fit the budget is dropped ENTIRELY (never partially/silently
truncated mid-block), so nothing is ever presented as complete when it
isn't.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from production.hamsterwatch_context import extract_keywords
from production.knowledge import KnowledgeItem, KnowledgeType

# ==========================================================
# Token estimation -- see module docstring for why char/4, not a real
# tokenizer.
# ==========================================================

TOKEN_CHAR_RATIO = 4


def estimate_tokens(text: str) -> int:
    """Rough, deliberately-round-up token estimate for `text`. Never
    underestimates a non-empty string's cost (ceiling division), since
    an optimistic estimate here is exactly what would let a request
    slip past the budget and repeat the production incident."""

    if not text:
        return 0
    return (len(text) + TOKEN_CHAR_RATIO - 1) // TOKEN_CHAR_RATIO


# ==========================================================
# The actual provider constraint (see module docstring's root-cause
# trace) and the safety-margined budget derived from it.
# ==========================================================

# Groq's actual documented on_demand-tier TPM limit for this model --
# the literal number named in the Railway 413 error this module exists
# to prevent. Not derived/computed -- a real, observed provider limit.
GROQ_TPM_LIMIT = 8000

# Must match max_completion_tokens passed to Groq's chat.completions.
# create() in services/ai_service.py's _try_groq_chat() -- Groq's TPM
# check counts prompt tokens and this reserved allowance TOGETHER, not
# prompt tokens alone (see module docstring).
RESERVED_OUTPUT_TOKENS = 2000

# Deliberately NOT zero, and deliberately not tiny: absorbs char/4
# estimation error, real provider tokenization variance, and leaves an
# actual margin below the hard limit rather than targeting it exactly
# -- roughly 12.5% of the total limit held back on top of the output
# reservation above.
SAFETY_MARGIN_TOKENS = 1000

# The real, enforced ceiling on everything this module assembles for
# the prompt (system instruction + every context block + conversation
# history combined). Total request estimate (this + RESERVED_OUTPUT_
# TOKENS) tops out at 7000 -- comfortably under GROQ_TPM_LIMIT's 8000.
MAX_PROMPT_TOKENS = GROQ_TPM_LIMIT - RESERVED_OUTPUT_TOKENS - SAFETY_MARGIN_TOKENS

# ==========================================================
# Per-source bounds -- the two previously-unbounded growth paths (see
# module docstring), plus a shared cap for conversation history.
# ==========================================================

# Non-RULE administrator-taught items (FACT/CORRECTION) surviving into
# one prompt -- see select_relevant_knowledge_items(). RULEs are never
# capped by this: they're standing instructions Julie must always
# follow, not perishable facts, and are typically few in practice.
MAX_RELEVANT_KNOWLEDGE_ITEMS = 10

# Per-item cap on a single /remember entry's rendered length -- an
# unusually long memory can no longer alone consume an outsized share
# of the budget. Same order of magnitude as services/ai_service.py's
# existing MAX_HISTORICAL_CONTENT_CHARS for a Hamsterwatch article,
# scaled down since a remembered note is meant to be a short aside,
# not a full article.
MAX_MEMORY_ITEM_CHARS = 400

# The discretionary slice of MAX_PROMPT_TOKENS reserved specifically
# for conversation history -- independent of the fact-block budget
# below, since history isn't one droppable block but an ordered
# sequence of prior turns that should degrade gracefully (drop the
# oldest first) rather than vanish entirely.
MAX_HISTORY_TOKENS = 1500


def truncate_for_budget(text: str, max_chars: int) -> tuple[str, bool]:
    """Collapses internal whitespace to one line, then truncates to
    `max_chars` at the nearest word boundary if needed. Returns
    (text, was_truncated) -- same shape as services/ai_service.py's
    existing _bounded_historical_text() for a Hamsterwatch article
    (kept as a separate function rather than sharing code across
    modules for two genuinely different content types, so a future
    change to one's truncation rules can't silently affect the
    other)."""

    collapsed = " ".join(text.split())

    if len(collapsed) <= max_chars:
        return collapsed, False

    truncated = collapsed[:max_chars].rsplit(" ", 1)[0]
    return truncated + "...", True


def select_relevant_knowledge_items(
    items: list[KnowledgeItem],
    user_text: str,
    *,
    max_items: int = MAX_RELEVANT_KNOWLEDGE_ITEMS,
) -> list[KnowledgeItem]:
    """Bounds administrator-taught FACT/CORRECTION items to at most
    `max_items`, keyword-scored against `user_text` -- the fix for
    format_learned_knowledge() previously rendering EVERY active item
    unconditionally (see module docstring's root-cause trace). Every
    RULE is always kept (see MAX_RELEVANT_KNOWLEDGE_ITEMS's docstring).

    Reuses production/hamsterwatch_context.py's extract_keywords() --
    the same deterministic technique production/memory.py's
    MemoryStore.recall() and that module's own retrieval already use
    for an analogous problem, not a new algorithm invented here.

    When `user_text` has no extractable keywords (a broad question
    like "tell me everything you know" -- "tell" is itself a stopword,
    and a genuinely topic-free question has nothing to score against),
    falls back to the `max_items` most RECENTLY updated items instead
    of an arbitrary/unstable ordering -- consistent with how
    ADMINISTRATOR-MAINTAINED FACTS are already described elsewhere in
    this codebase as "the administrator's most recent word on each
    topic."
    """

    rules = [item for item in items if item.type == KnowledgeType.RULE]
    others = [item for item in items if item.type != KnowledgeType.RULE]

    if len(others) <= max_items:
        return rules + others

    keywords = set(extract_keywords(user_text))

    if keywords:
        def score(item: KnowledgeItem) -> int:
            return len(keywords & set(extract_keywords(item.content)))

        ranked = sorted(others, key=lambda item: (score(item), item.updated_at), reverse=True)
    else:
        ranked = sorted(others, key=lambda item: item.updated_at, reverse=True)

    return rules + ranked[:max_items]


def trim_history_to_budget(
    history: list[tuple[str, str, str | None]],
    *,
    max_tokens: int = MAX_HISTORY_TOKENS,
) -> list[tuple[str, str, str | None]]:
    """Drops the OLDEST entries first until the remaining history's
    estimated size fits `max_tokens` -- always keeps at least the most
    recent entry (the current turn just appended by
    update_and_get_history()), even if it alone exceeds `max_tokens`,
    since the question actually being asked must never be the thing
    dropped."""

    if not history:
        return history

    total = sum(estimate_tokens(text) for _role, text, _author in history)
    if total <= max_tokens:
        return history

    trimmed = list(history)
    while len(trimmed) > 1 and total > max_tokens:
        _role, text, _author = trimmed.pop(0)
        total -= estimate_tokens(text)

    return trimmed


@dataclass(slots=True)
class ContextBudgetReport:
    """Safe-to-log summary of one request's budget decisions -- labels
    and counts only, NEVER raw block content (see module docstring and
    log_line() below). Every field here is deliberately something that
    could be posted in a public bug report without leaking anything."""

    max_prompt_tokens: int = MAX_PROMPT_TOKENS
    reserved_output_tokens: int = RESERVED_OUTPUT_TOKENS
    included: list[str] = field(default_factory=list)
    trimmed: list[str] = field(default_factory=list)
    estimated_input_tokens: int = 0

    @property
    def estimated_total_tokens(self) -> int:
        return self.estimated_input_tokens + self.reserved_output_tokens

    def log_line(self) -> str:
        """One-line, content-free summary safe for real production
        logs (see the module this is used from for exactly what NEVER
        appears here: no prompt text, no knowledge/memory content, no
        credentials)."""

        return (
            "AI context budget: "
            f"input={self.estimated_input_tokens} "
            f"reserved_output={self.reserved_output_tokens} "
            f"estimated_total={self.estimated_total_tokens} "
            f"budget={self.max_prompt_tokens + self.reserved_output_tokens} "
            f"included={','.join(self.included) or 'none'} "
            f"trimmed={','.join(self.trimmed) or 'none'}"
        )


def allocate_context_budget(
    blocks: list[tuple[str, str]],
    *,
    max_prompt_tokens: int = MAX_PROMPT_TOKENS,
) -> tuple[dict[str, str], ContextBudgetReport]:
    """Greedily includes each (label, text) block from `blocks`, IN
    THE ORDER GIVEN (the caller's priority order, highest first), as
    long as it still fits the remaining budget. A block that doesn't
    fit is dropped ENTIRELY -- see module docstring for why this never
    partially truncates a block instead.

    Returns (included, report): `included` maps label -> text for
    every block that survived (an empty/falsy block is simply never
    added, at no cost); `report` is safe to log directly (see
    ContextBudgetReport).
    """

    report = ContextBudgetReport(max_prompt_tokens=max_prompt_tokens)
    remaining = max_prompt_tokens
    included: dict[str, str] = {}

    for label, text in blocks:
        if not text:
            continue
        cost = estimate_tokens(text)
        if cost <= remaining:
            included[label] = text
            remaining -= cost
            report.included.append(label)
        else:
            report.trimmed.append(label)

    report.estimated_input_tokens = max_prompt_tokens - remaining
    return included, report
