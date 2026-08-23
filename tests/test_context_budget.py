"""Tests for production/context_budget.py -- the deterministic,
no-AI-call token-budget system that fixes the real production
incident: Groq's on_demand tier rejecting a request (prompt tokens +
reserved completion tokens) once it exceeds openai/gpt-oss-120b's TPM
limit (413 "Request too large", Railway logs: "TPM Limit 8000,
Requested 9108"/"9200").

See tests/test_context_budget_boundary.py for the integration-level
proof through the real generate_julie_response()/generate_ai_reply()
path.
"""

from __future__ import annotations

from datetime import UTC, datetime

from production.context_budget import (
    MAX_HISTORY_TOKENS,
    MAX_PROMPT_TOKENS,
    MAX_RELEVANT_KNOWLEDGE_ITEMS,
    RESERVED_OUTPUT_TOKENS,
    ContextBudgetReport,
    allocate_context_budget,
    estimate_tokens,
    select_relevant_knowledge_items,
    trim_history_to_budget,
    truncate_for_budget,
)
from production.knowledge import KnowledgeItem, KnowledgeType


def _item(id_, type_, content, *, minutes_ago=0):
    when = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    return KnowledgeItem(
        id=id_, type=type_, content=content, author_id=1,
        created_at=when, updated_at=when,
    )


# ==========================================================
# estimate_tokens(): deterministic, never underestimates
# ==========================================================


def test_estimate_tokens_empty_string_is_zero():
    assert estimate_tokens("") == 0


def test_estimate_tokens_rounds_up_never_down():
    # 1 char is less than a full "token unit" but must still cost 1,
    # never 0 -- underestimating is exactly the failure mode this
    # module exists to prevent.
    assert estimate_tokens("x") == 1
    assert estimate_tokens("xxxx") == 1
    assert estimate_tokens("xxxxx") == 2


def test_estimate_tokens_scales_with_length():
    short = "a" * 40
    long = "a" * 400
    assert estimate_tokens(long) == 10 * estimate_tokens(short)


# ==========================================================
# The actual provider budget derived from the real Groq constraint
# ==========================================================


def test_max_prompt_tokens_leaves_comfortable_margin_below_the_tpm_limit():
    """The literal acceptance criterion: total estimated request
    (MAX_PROMPT_TOKENS + RESERVED_OUTPUT_TOKENS) must be safely under
    Groq's real 8000 TPM limit, not equal to or barely under it."""

    from production.context_budget import GROQ_TPM_LIMIT

    total_ceiling = MAX_PROMPT_TOKENS + RESERVED_OUTPUT_TOKENS
    assert total_ceiling < GROQ_TPM_LIMIT
    assert GROQ_TPM_LIMIT - total_ceiling >= 500  # a real, non-trivial margin


# ==========================================================
# truncate_for_budget()
# ==========================================================


def test_truncate_for_budget_leaves_short_text_untouched():
    text, was_truncated = truncate_for_budget("short note", 400)
    assert text == "short note"
    assert was_truncated is False


def test_truncate_for_budget_cuts_long_text_at_a_word_boundary():
    text, was_truncated = truncate_for_budget("word " * 200, 50)
    assert was_truncated is True
    assert len(text) <= 53  # 50 + "..." headroom
    assert not text.rstrip(".").endswith("wor")  # never a broken mid-word cut


def test_truncate_for_budget_collapses_whitespace():
    text, _ = truncate_for_budget("a\n\n\nb   c", 100)
    assert text == "a b c"


# ==========================================================
# select_relevant_knowledge_items(): the fix for format_learned_
# knowledge() previously rendering EVERY active item unconditionally
# ==========================================================


def test_small_knowledge_set_is_returned_untouched():
    items = [_item(i, KnowledgeType.FACT, f"Fact {i}") for i in range(3)]
    assert select_relevant_knowledge_items(items, "who is HOH?") == items


def test_large_knowledge_set_is_bounded_to_max_items():
    items = [_item(i, KnowledgeType.FACT, f"Fact number {i} about the game") for i in range(50)]
    selected = select_relevant_knowledge_items(items, "who is HOH?")
    assert len(selected) <= MAX_RELEVANT_KNOWLEDGE_ITEMS


def test_relevant_fact_survives_trimming_over_unrelated_ones():
    items = [_item(i, KnowledgeType.FACT, f"Unrelated filler fact number {i}") for i in range(30)]
    items.append(
        _item(999, KnowledgeType.FACT, "Taylor was evicted on a shocking blindside vote.")
    )

    selected = select_relevant_knowledge_items(items, "What do you know about Taylor?")

    assert any(item.id == 999 for item in selected)


def test_rules_are_never_trimmed_regardless_of_relevance_or_count():
    rules = [_item(i, KnowledgeType.RULE, f"Standing rule {i}") for i in range(5)]
    facts = [_item(100 + i, KnowledgeType.FACT, f"Unrelated fact {i}") for i in range(30)]

    selected = select_relevant_knowledge_items(rules + facts, "who is HOH?")

    for rule in rules:
        assert rule in selected


def test_no_keywords_falls_back_to_most_recently_updated():
    """A genuinely topic-free broad question ("tell me everything you
    know") has nothing to score against -- falls back to recency,
    matching how ADMINISTRATOR-MAINTAINED FACTS are already described
    elsewhere as "the administrator's most recent word.\""""

    old = datetime(2026, 1, 1, tzinfo=UTC)
    new = datetime(2026, 8, 1, tzinfo=UTC)
    items = [
        KnowledgeItem(
            id=i, type=KnowledgeType.FACT, content=f"Filler fact {i}", author_id=1,
            created_at=old, updated_at=old,
        )
        for i in range(30)
    ]
    newest = KnowledgeItem(
        id=999, type=KnowledgeType.FACT, content="The most recently taught fact.",
        author_id=1, created_at=new, updated_at=new,
    )
    items.append(newest)

    selected = select_relevant_knowledge_items(items, "tell me everything you know")

    assert newest in selected


# ==========================================================
# trim_history_to_budget()
# ==========================================================


def test_small_history_is_returned_untouched():
    history = [("user", "hi", "Bobby"), ("model", "hey!", None)]
    assert trim_history_to_budget(history) == history


def test_large_history_is_trimmed_to_fit_the_budget():
    history = [("user", "message " * 100, "Bobby") for _ in range(20)] + [
        ("model", "reply " * 100, None) for _ in range(20)
    ]
    trimmed = trim_history_to_budget(history)

    total = sum(estimate_tokens(text) for _role, text, _author in trimmed)
    assert total <= MAX_HISTORY_TOKENS


def test_trimming_drops_oldest_messages_first():
    history = [("user", f"message {i}", "Bobby") for i in range(50)]
    # Make each message large enough that trimming is actually required.
    history = [(role, text * 50, author) for role, text, author in history]

    trimmed = trim_history_to_budget(history)

    assert trimmed[-1] == history[-1]  # most recent survives
    assert trimmed[0] != history[0]  # oldest was dropped


def test_the_current_turn_is_never_dropped_even_if_it_alone_exceeds_budget():
    huge_current_turn = [("user", "x" * 50000, "Bobby")]
    trimmed = trim_history_to_budget(huge_current_turn)
    assert trimmed == huge_current_turn


# ==========================================================
# allocate_context_budget()
# ==========================================================


def test_all_blocks_included_when_comfortably_under_budget():
    blocks = [("a", "short block a"), ("b", "short block b"), ("c", "short block c")]
    included, report = allocate_context_budget(blocks)

    assert set(included) == {"a", "b", "c"}
    assert report.trimmed == []


def test_lower_priority_blocks_dropped_first_when_over_budget():
    blocks = [
        ("high_priority", "x" * 100),
        ("low_priority", "y" * (MAX_PROMPT_TOKENS * 5)),  # deliberately far too large
    ]
    included, report = allocate_context_budget(blocks)

    assert "high_priority" in included
    assert "low_priority" not in included
    assert report.trimmed == ["low_priority"]


def test_empty_block_costs_nothing_and_is_simply_omitted():
    blocks = [("present", "some content"), ("empty", "")]
    included, report = allocate_context_budget(blocks)

    assert "present" in included
    assert "empty" not in included
    assert "empty" not in report.trimmed  # never even attempted, not "too big"


def test_a_block_that_does_not_fit_is_never_partially_truncated():
    """The core design guarantee: a block either survives whole, or is
    dropped whole -- never presented as if complete when it isn't."""

    oversized_content = "y" * (MAX_PROMPT_TOKENS * 10)
    blocks = [("oversized", oversized_content)]
    included, _report = allocate_context_budget(blocks)

    assert "oversized" not in included


def test_report_estimated_input_tokens_matches_what_was_actually_included():
    blocks = [("a", "a" * 40), ("b", "b" * 40)]
    _included, report = allocate_context_budget(blocks)

    assert report.estimated_input_tokens == estimate_tokens("a" * 40) + estimate_tokens("b" * 40)


# ==========================================================
# ContextBudgetReport.log_line(): safe to log, never raw content
# ==========================================================


def test_log_line_never_contains_block_content():
    secret_content = "SUPER_SECRET_ADMIN_KNOWLEDGE_CONTENT_MUST_NOT_APPEAR"
    blocks = [("knowledge", secret_content)]
    _included, report = allocate_context_budget(blocks)

    line = report.log_line()

    assert secret_content not in line
    assert "knowledge" in line  # the LABEL is fine to log, just not content


def test_log_line_is_a_single_line_with_expected_fields():
    report = ContextBudgetReport(
        included=["official_state", "knowledge"], trimmed=["historical_context"],
        estimated_input_tokens=1234,
    )
    line = report.log_line()

    assert "\n" not in line
    assert "input=1234" in line
    assert "reserved_output=" in line
    assert "estimated_total=" in line
    assert "budget=" in line
    assert "included=official_state,knowledge" in line
    assert "trimmed=historical_context" in line


def test_log_line_reports_none_for_empty_included_or_trimmed():
    report = ContextBudgetReport()
    line = report.log_line()

    assert "included=none" in line
    assert "trimmed=none" in line
