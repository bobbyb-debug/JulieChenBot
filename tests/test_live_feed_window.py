"""Tests for production/live_feed_window.py -- the deterministic
time-window parser and subject-keyword selector behind Julie's
conversational recent-live-feed retrieval. See that module's own
docstring for why "since X"-style anchored windows are deliberately
NOT parsed, and why subject-keyword extraction relies on
capitalization rather than a general stopword-filtered keyword list.
"""

from __future__ import annotations

from production.live_feed_window import (
    DEFAULT_WINDOW_HOURS,
    JUST_NOW_WINDOW_HOURS,
    MAX_RECENT_FEED_ITEMS,
    MAX_WINDOW_HOURS,
    MIN_WINDOW_HOURS,
    OVERNIGHT_WINDOW_HOURS,
    PART_OF_DAY_WINDOW_HOURS,
    TODAY_WINDOW_HOURS,
    extract_subject_keywords,
    parse_recent_window,
    select_recent_updates,
)


# ==========================================================
# Time parsing -- explicit windows
# ==========================================================


def test_last_hour_bare():
    assert parse_recent_window("What happened in the last hour?") == 1.0


def test_last_2_hours():
    assert parse_recent_window("What happened in the last 2 hours?") == 2.0


def test_last_5_hours():
    assert parse_recent_window("What happened in the last 5 hours?") == 5.0


def test_last_12_hours():
    assert parse_recent_window("Catch me up on the last 12 hours") == 12.0


def test_last_24_hours():
    assert parse_recent_window("What happened in the last 24 hours?") == 24.0


def test_fractional_hours():
    assert parse_recent_window("What happened in the last 1.5 hours?") == 1.5


def test_last_few_hours_maps_to_default():
    assert parse_recent_window("Anything happen in the last few hours?") == DEFAULT_WINDOW_HOURS


# ==========================================================
# Time parsing -- named windows
# ==========================================================


def test_overnight():
    assert parse_recent_window("What happened overnight?") == OVERNIGHT_WINDOW_HOURS


def test_today():
    assert parse_recent_window("Catch me up on today.") == TODAY_WINDOW_HOURS


def test_this_morning():
    assert parse_recent_window("What happened this morning?") == PART_OF_DAY_WINDOW_HOURS


def test_this_afternoon():
    assert parse_recent_window("What happened this afternoon?") == PART_OF_DAY_WINDOW_HOURS


def test_tonight():
    assert parse_recent_window("What's going on tonight?") == PART_OF_DAY_WINDOW_HOURS


def test_just_now():
    assert parse_recent_window("What happened just now?") == JUST_NOW_WINDOW_HOURS


def test_recently():
    assert parse_recent_window("What has Devens been up to recently?") == DEFAULT_WINDOW_HOURS


def test_what_have_i_missed():
    assert parse_recent_window("What have I missed?") == DEFAULT_WINDOW_HOURS


def test_catch_me_up():
    assert parse_recent_window("Catch me up.") == DEFAULT_WINDOW_HOURS


def test_whats_been_happening():
    assert parse_recent_window("What's been happening in the house?") == DEFAULT_WINDOW_HOURS


# ==========================================================
# Malformed / ambiguous / unsupported -- must never guess
# ==========================================================


def test_ordinary_question_returns_none():
    assert parse_recent_window("Who is the current HOH?") is None


def test_since_event_is_not_supported_and_returns_none():
    # Deliberate limitation -- see module docstring. Must not invent a
    # window by guessing how long ago "the veto" happened.
    assert parse_recent_window("Has anything changed since the veto?") is None


def test_last_week_is_not_this_modules_concern():
    # "last week" is historical_retrieval.py's territory, not a recent
    # live-feed window -- must not be misread as a recent-hours request.
    assert parse_recent_window("What happened last week?") is None


def test_bare_last_with_no_unit_returns_none():
    assert parse_recent_window("What happened last?") is None


def test_empty_string_returns_none():
    assert parse_recent_window("") is None


# ==========================================================
# Bounding -- very large/small windows must be clamped, never trusted
# verbatim.
# ==========================================================


def test_very_large_window_is_clamped():
    assert parse_recent_window("What happened in the last 500 hours?") == MAX_WINDOW_HOURS


def test_zero_hours_is_clamped_to_the_floor():
    assert parse_recent_window("What happened in the last 0 hours?") == MIN_WINDOW_HOURS


def test_explicit_window_within_bounds_is_unclamped():
    assert parse_recent_window("What happened in the last 10 hours?") == 10.0


# ==========================================================
# Priority -- most-specific-first, same style as
# production/historical_retrieval.py and production/reaction_engine.py.
# ==========================================================


def test_explicit_hours_beats_recently():
    text = "What happened recently, like in the last 2 hours?"
    assert parse_recent_window(text) == 2.0


# ==========================================================
# Subject-keyword extraction
# ==========================================================


def test_extract_subject_keywords_single_name():
    assert extract_subject_keywords("What has Devens been up to recently?") == ["devens"]


def test_extract_subject_keywords_name_after_generic_opener():
    assert extract_subject_keywords("Anything happen with LaLa in the last few hours?") == [
        "lala"
    ]


def test_extract_subject_keywords_multiple_names():
    result = extract_subject_keywords("What have Angela and Dee been talking about?")
    assert result == ["angela", "dee"]


def test_extract_subject_keywords_no_name_present():
    assert extract_subject_keywords("What did the houseguests do overnight?") == []


def test_extract_subject_keywords_generic_question_is_empty():
    assert extract_subject_keywords("What happened in the last hour?") == []


def test_extract_subject_keywords_all_lowercase_yields_nothing():
    # Documented limitation, not a silent bug -- see module docstring.
    assert extract_subject_keywords("what has devens been up to recently") == []


def test_extract_subject_keywords_never_returns_sentence_first_word():
    # "What" starts the sentence and is capitalized purely by English
    # grammar -- must never be treated as a name candidate.
    assert "what" not in extract_subject_keywords("What has Devens been up to?")


def test_extract_subject_keywords_common_words_do_not_leak_through():
    # Regression guard for the false-positive risk found during review:
    # an earlier version let "been"/"anything"/"happen"/"talking"
    # through as false subject candidates.
    result = extract_subject_keywords("What has Devens been up to recently?")
    for common_word in ("been", "up", "to", "recently"):
        assert common_word not in result


# ==========================================================
# select_recent_updates() -- subject narrowing, fallback-to-full-window,
# and the item-count bound.
# ==========================================================


def test_select_recent_updates_no_subject_returns_full_window():
    updates = ["Drew nominated Devens.", "LaLa did laundry."]
    selected, keywords = select_recent_updates(updates, "What happened in the last hour?")
    assert selected == updates
    assert keywords == []


def test_select_recent_updates_narrows_to_matching_subject():
    updates = ["Drew nominated Devens.", "LaLa did laundry.", "Devens talked strategy."]
    selected, keywords = select_recent_updates(
        updates, "What has Devens been up to recently?"
    )
    assert selected == ["Drew nominated Devens.", "Devens talked strategy."]
    assert keywords == ["devens"]


def test_select_recent_updates_falls_back_to_full_window_when_subject_has_no_matches():
    # The name is real, but nothing in the (already time-windowed)
    # updates happens to mention them -- must not report a
    # fabricated-looking empty player-specific result; fall back to
    # the full window instead (see module docstring's trust-boundary
    # note).
    updates = ["Drew nominated LaLa.", "LaLa did laundry."]
    selected, keywords = select_recent_updates(
        updates, "What has Devens been up to recently?"
    )
    assert selected == updates
    assert keywords == []


def test_select_recent_updates_empty_updates_stays_empty():
    selected, keywords = select_recent_updates([], "What happened in the last hour?")
    assert selected == []
    assert keywords == []


def test_select_recent_updates_bounds_item_count_to_most_recent():
    updates = [f"update {i}" for i in range(30)]
    selected, _keywords = select_recent_updates(updates, "What happened today?")
    assert len(selected) == MAX_RECENT_FEED_ITEMS
    assert selected == updates[-MAX_RECENT_FEED_ITEMS:]


def test_select_recent_updates_subject_filtered_result_also_bounded():
    updates = [f"Devens update {i}" for i in range(30)]
    selected, keywords = select_recent_updates(
        updates, "What has Devens been up to recently?"
    )
    assert len(selected) == MAX_RECENT_FEED_ITEMS
    assert keywords == ["devens"]
