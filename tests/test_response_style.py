"""Tests for production/response_style.py -- the deterministic,
no-AI-call layer that decides HOW Julie should host a reply (never
WHAT is true). Covers intent classification, greeting/conversation-
start detection, and catchphrase-repetition detection.
"""

from __future__ import annotations

from production.response_style import (
    GREETING_GAP_MINUTES,
    ResponseIntent,
    build_response_guidance,
    classify_intent,
)


# ==========================================================
# classify_intent() -- BANTER
# ==========================================================


def test_reaction_without_question_mark_is_banter():
    assert classify_intent("lol that nomination is fucking wild") == ResponseIntent.BANTER


def test_reaction_still_banter_even_with_a_game_keyword():
    """Message FORM (a reaction, no question) wins over game-keyword
    content -- this is what lets Scenario D (banter about a
    nomination) be treated as banter rather than a fact lookup."""

    assert classify_intent("omg the veto ceremony was wild") == ResponseIntent.BANTER


def test_a_real_question_with_a_reaction_opener_is_not_forced_into_banter():
    """"?" present -> this is an actual question, not pure reaction --
    BANTER only applies to message form (no question), not merely
    starting with a casual word."""

    assert classify_intent("omg who is the veto holder?") != ResponseIntent.BANTER


# ==========================================================
# classify_intent() -- DRAMATIC
# ==========================================================


def test_diamond_power_of_veto_is_dramatic():
    assert classify_intent("Tell me about the DPOV situation.") == ResponseIntent.DRAMATIC


def test_blindside_is_dramatic():
    assert classify_intent("who got blindsided this week?") == ResponseIntent.DRAMATIC


def test_dramatic_term_in_retrieved_historical_context_still_counts():
    """A plain-sounding question can still be dramatic if the
    retrieved material itself describes a big swing."""

    result = classify_intent(
        "what happened during Taylor's HOH?",
        historical_context="Taylor executed a brutal backdoor plan.",
    )
    assert result == ResponseIntent.DRAMATIC


# ==========================================================
# classify_intent() -- HISTORICAL
# ==========================================================


def test_retrieved_historical_context_makes_it_historical():
    result = classify_intent(
        "what was going on then?",
        historical_context="Some background material.",
    )
    assert result == ResponseIntent.HISTORICAL


def test_day_n_language_is_historical_even_with_no_retrieved_material():
    """Scenario E: a Day-N question with nothing reliably retrieved
    must still be recognized as historical-shaped, so the rendered
    guidance can tell Julie to say she doesn't know rather than
    invent something."""

    assert classify_intent("What happened on Day 12?", historical_context="") == (
        ResponseIntent.HISTORICAL
    )


def test_earlier_language_is_historical():
    assert classify_intent("what happened earlier this season?") == ResponseIntent.HISTORICAL


# ==========================================================
# classify_intent() -- DIRECT_FACT
# ==========================================================


def test_who_is_hoh_is_direct_fact():
    assert classify_intent("Who is the current HoH?") == ResponseIntent.DIRECT_FACT


def test_who_are_the_have_nots_is_direct_fact():
    assert classify_intent("Who are the have-nots") == ResponseIntent.DIRECT_FACT


def test_who_won_veto_is_direct_fact():
    assert classify_intent("Who won veto?") == ResponseIntent.DIRECT_FACT


# ==========================================================
# classify_intent() -- GENERAL fallback, and priority ordering
# ==========================================================


def test_ordinary_conversation_with_no_game_content_is_general():
    assert classify_intent("How's your day going?") == ResponseIntent.GENERAL


def test_compound_question_with_historical_half_is_historical_not_direct_fact():
    """The exact compound example from the personality-overhaul brief:
    a current-state question combined with a historical one should
    lean toward HISTORICAL so Julie can address both halves, not get
    locked into a terse DIRECT_FACT answer that ignores the second
    half."""

    result = classify_intent(
        "Who is the current HoH, and what happened during Taylor's HoH?"
    )
    assert result == ResponseIntent.HISTORICAL


def test_dramatic_term_outranks_historical_language():
    result = classify_intent(
        "what happened earlier with the diamond power of veto?"
    )
    assert result == ResponseIntent.DRAMATIC


# ==========================================================
# build_response_guidance() -- conversation start / greeting
# ==========================================================


def test_no_prior_activity_is_a_conversation_start():
    guidance = build_response_guidance(
        "hi", history=[("user", "hi", "Alex")], minutes_since_last_message=None
    )
    assert guidance.is_conversation_start is True


def test_recent_activity_is_not_a_conversation_start():
    guidance = build_response_guidance(
        "nominees?",
        history=[
            ("user", "who is hoh?", "Alex"),
            ("model", "Dee.", None),
            ("user", "nominees?", "Alex"),
        ],
        minutes_since_last_message=1.0,
    )
    assert guidance.is_conversation_start is False


def test_a_real_gap_resumes_as_a_conversation_start():
    guidance = build_response_guidance(
        "hey",
        history=[("user", "hey", "Alex")],
        minutes_since_last_message=GREETING_GAP_MINUTES + 5,
    )
    assert guidance.is_conversation_start is True


def test_gap_boundary_is_inclusive_of_the_threshold():
    guidance = build_response_guidance(
        "hey",
        history=[("user", "hey", "Alex")],
        minutes_since_last_message=GREETING_GAP_MINUTES,
    )
    assert guidance.is_conversation_start is True


def test_just_under_the_gap_threshold_is_still_ongoing():
    guidance = build_response_guidance(
        "hey",
        history=[("user", "hey", "Alex")],
        minutes_since_last_message=GREETING_GAP_MINUTES - 0.01,
    )
    assert guidance.is_conversation_start is False


# ==========================================================
# build_response_guidance() -- catchphrase repetition detection
# ==========================================================


def test_detects_a_catchphrase_used_in_julies_immediately_prior_reply():
    guidance = build_response_guidance(
        "nominees?",
        history=[
            ("user", "who is hoh?", "Alex"),
            ("model", "Good evening, Houseguests! Expect the unexpected--Dee is HOH.", None),
            ("user", "nominees?", "Alex"),
        ],
        minutes_since_last_message=0.5,
    )
    assert "expect the unexpected" in guidance.recently_used_phrases


def test_no_catchphrase_flagged_when_last_reply_used_none():
    guidance = build_response_guidance(
        "nominees?",
        history=[
            ("user", "who is hoh?", "Alex"),
            ("model", "Dee.", None),
            ("user", "nominees?", "Alex"),
        ],
        minutes_since_last_message=0.5,
    )
    assert guidance.recently_used_phrases == []


def test_only_julies_most_recent_reply_is_checked_not_older_ones():
    """An older reply using a catchphrase shouldn't keep flagging
    forever once Julie has already moved on."""

    guidance = build_response_guidance(
        "have-nots?",
        history=[
            ("user", "hi", "Alex"),
            ("model", "Good evening, Houseguests!", None),
            ("user", "who is hoh?", "Alex"),
            ("model", "Dee.", None),
            ("user", "have-nots?", "Alex"),
        ],
        minutes_since_last_message=0.5,
    )
    assert guidance.recently_used_phrases == []


def test_the_current_users_own_message_is_never_scanned_for_catchphrases():
    """history's last entry is the just-appended current user turn --
    it must be excluded from the "Julie's last reply" scan even if it
    happened to contain matching text."""

    guidance = build_response_guidance(
        "Expect the unexpected, right?",
        history=[
            ("user", "hi", "Alex"),
            ("model", "Dee.", None),
            ("user", "Expect the unexpected, right?", "Alex"),
        ],
        minutes_since_last_message=0.5,
    )
    assert guidance.recently_used_phrases == []


# ==========================================================
# Determinism
# ==========================================================


def test_classification_is_deterministic():
    args = ("Who is the current HoH, and what happened during Taylor's HoH?",)
    assert classify_intent(*args) == classify_intent(*args)


def test_guidance_is_deterministic_for_identical_input():
    history = [
        ("user", "who is hoh?", "Alex"),
        ("model", "Good evening, Houseguests!", None),
        ("user", "nominees?", "Alex"),
    ]
    first = build_response_guidance("nominees?", history=history, minutes_since_last_message=1.0)
    second = build_response_guidance("nominees?", history=history, minutes_since_last_message=1.0)
    assert first == second
