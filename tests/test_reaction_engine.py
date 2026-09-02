"""Tests for production/reaction_engine.py -- the deterministic
situational-reaction classifier behind Julie's personality upgrade
(event significance, intensity, and social-engagement signals). See
that module's own docstring for why some spec categories (a "good
observation", correcting a wrong user, uncertainty) are deliberately
NOT classified here -- they're standing instructions in
services/ai_service.py instead.
"""

from __future__ import annotations

from production.reaction_engine import (
    MAX_INTENSITY,
    ReactionContext,
    SituationalEvent,
    build_reaction_context,
    classify_event,
    score_intensity,
)


# ==========================================================
# classify_event() -- one representative phrase per category, plus the
# ordinary/no-match default.
# ==========================================================


def test_classify_event_blindside():
    assert classify_event("That was a total blindside!") == SituationalEvent.BLINDSIDE


def test_classify_event_alliance_exposed():
    assert (
        classify_event("Their alliance was exposed at the veto meeting")
        == SituationalEvent.ALLIANCE_EXPOSED
    )


def test_classify_event_eviction():
    assert classify_event("Drew was evicted tonight") == SituationalEvent.EVICTION


def test_classify_event_suspected_lying():
    assert (
        classify_event("I think Barrett is lying about that conversation")
        == SituationalEvent.SUSPECTED_LYING
    )


def test_classify_event_hg_conflict():
    assert (
        classify_event("There was a huge argument in the kitchen")
        == SituationalEvent.HG_CONFLICT
    )


def test_classify_event_spiraling():
    assert (
        classify_event("Taylor was crying in the diary room again")
        == SituationalEvent.SPIRALING
    )


def test_classify_event_dumb_move():
    assert (
        classify_event("That was such a dumb move by Drew")
        == SituationalEvent.DUMB_MOVE
    )


def test_classify_event_great_move():
    assert (
        classify_event("Honestly a genius move by Dee")
        == SituationalEvent.GREAT_MOVE
    )


def test_classify_event_unexpected_vote():
    assert (
        classify_event("That was a total vote flip nobody expected")
        == SituationalEvent.UNEXPECTED_VOTE
    )


def test_classify_event_power_shift():
    assert (
        classify_event("This completely changes the power in the house")
        == SituationalEvent.POWER_SHIFT
    )


def test_classify_event_replacement_nominee():
    assert (
        classify_event("Dee named a replacement nominee after the veto")
        == SituationalEvent.REPLACEMENT_NOMINEE
    )


def test_classify_event_major_nomination():
    assert (
        classify_event("That was a shocking nomination this week")
        == SituationalEvent.MAJOR_NOMINATION
    )


def test_classify_event_veto_used():
    assert (
        classify_event("Dee used the power of veto on Drew")
        == SituationalEvent.VETO_USED
    )


def test_classify_event_comp_win():
    assert classify_event("Yash won HOH again") == SituationalEvent.COMP_WIN


def test_classify_event_ordinary_defaults_to_none():
    assert classify_event("Who is the current HOH?") == SituationalEvent.NONE
    assert classify_event("Good morning Julie") == SituationalEvent.NONE


def test_classify_event_priority_blindside_beats_comp_win():
    # A message that mentions both a comp win term and a blindside term
    # must resolve to the more significant category -- see
    # _EVENT_PRIORITY's ordering.
    text = "Yash won veto but honestly the real story is that blindside"
    assert classify_event(text) == SituationalEvent.BLINDSIDE


def test_classify_event_priority_eviction_beats_veto_used():
    text = "Dee used the veto, but the real headline is Drew got evicted"
    assert classify_event(text) == SituationalEvent.EVICTION


def test_classify_event_priority_alliance_exposed_beats_dumb_move():
    text = "That was such a dumb move -- it exposed the whole alliance was exposed"
    assert classify_event(text) == SituationalEvent.ALLIANCE_EXPOSED


# ==========================================================
# False-positive guards -- ordinary Big Brother acronyms must never
# read as shouting/excitement on their own. Explicitly required by
# the personality spec: HOH, POV, DPOV, BB, HG.
# ==========================================================


def test_hoh_acronym_alone_is_not_excitement():
    assert score_intensity("Who won HOH?", SituationalEvent.NONE) == 0


def test_pov_acronym_alone_is_not_excitement():
    assert score_intensity("Who has POV?", SituationalEvent.NONE) == 0


def test_dpov_acronym_alone_is_not_excitement():
    assert score_intensity("Is there a DPOV in play?", SituationalEvent.NONE) == 0


def test_bb_acronym_alone_is_not_excitement():
    assert score_intensity("Is this a new BB twist?", SituationalEvent.NONE) == 0


def test_hg_acronym_alone_is_not_excitement():
    assert score_intensity("Which HG is safest?", SituationalEvent.NONE) == 0


def test_genuine_all_caps_word_still_counts_as_excitement():
    # A real shouted word (not a routine acronym) must still register.
    plain = score_intensity("That was a big move.", SituationalEvent.GREAT_MOVE)
    shouted = score_intensity("That was a HUGE move.", SituationalEvent.GREAT_MOVE)
    assert shouted == plain + 1


# ==========================================================
# ReactionContext.log_line() -- content-free debug observability (see
# module docstring and services/ai_service.py's generate_julie_response()).
# ==========================================================


def test_log_line_is_content_free_and_labeled():
    ctx = build_reaction_context("Can you believe that blindside?!")
    line = ctx.log_line()
    assert "event=BLINDSIDE" in line
    assert "intensity=" in line
    assert "opinion_requested=" in line
    assert "user_banter=" in line
    assert "user_challenges_julie=" in line
    # Never the raw user message text.
    assert "believe" not in line.lower()


# ==========================================================
# score_intensity() -- baseline, excitement bonus, superlative bonus,
# clamping.
# ==========================================================


def test_score_intensity_ordinary_question_is_zero():
    assert score_intensity("Who is the current HOH?", SituationalEvent.NONE) == 0


def test_score_intensity_uses_event_baseline():
    assert score_intensity("Dee used the power of veto.", SituationalEvent.VETO_USED) == 1
    assert score_intensity("Drew was evicted.", SituationalEvent.EVICTION) == 3


def test_score_intensity_excitement_bonus_from_punctuation():
    plain = score_intensity("Yash won veto.", SituationalEvent.COMP_WIN)
    excited = score_intensity("Yash won veto???", SituationalEvent.COMP_WIN)
    assert excited == plain + 1


def test_score_intensity_excitement_bonus_from_all_caps():
    plain = score_intensity("Drew was evicted.", SituationalEvent.EVICTION)
    excited = score_intensity("DREW was evicted.", SituationalEvent.EVICTION)
    assert excited == plain + 1


def test_score_intensity_season_defining_bonus_reaches_max():
    text = "That blindside was completely game-changing!!!"
    assert score_intensity(text, SituationalEvent.BLINDSIDE) == MAX_INTENSITY


def test_score_intensity_never_exceeds_max():
    # Baseline (3) + excitement (+1) + superlative (+1) would be 5
    # without clamping.
    text = "SEASON-DEFINING blindside, this changes everything!!!"
    assert score_intensity(text, SituationalEvent.BLINDSIDE) <= MAX_INTENSITY


def test_score_intensity_never_negative():
    assert score_intensity("", SituationalEvent.NONE) == 0


# ==========================================================
# Social engagement flags -- independent of event/intensity.
# ==========================================================


def test_opinion_requested_detected():
    ctx = build_reaction_context("What do you think about that move?")
    assert ctx.opinion_requested is True


def test_opinion_not_requested_on_plain_factual_question():
    ctx = build_reaction_context("Who is the current HOH?")
    assert ctx.opinion_requested is False


def test_user_banter_detected_from_laughter_marker():
    ctx = build_reaction_context("lol I knew Barrett was going to do that")
    assert ctx.user_banter is True


def test_user_banter_not_triggered_by_serious_direct_address():
    # Regression guard for a dropped heuristic (see reaction_engine.py's
    # _BANTER_PATTERN docstring): directly addressing Julie is NOT by
    # itself banter -- "Julie, you have this wrong" is a real
    # challenge, not a joke, and must not be misclassified.
    ctx = build_reaction_context("Julie, you have this completely wrong")
    assert ctx.user_banter is False


def test_user_banter_false_on_plain_question():
    ctx = build_reaction_context("Who won the veto competition?")
    assert ctx.user_banter is False


def test_user_challenges_julie_detected():
    ctx = build_reaction_context("That's wrong, he literally told Melody he's voting Devens out.")
    assert ctx.user_challenges_julie is True


def test_user_challenges_julie_false_on_plain_agreement():
    ctx = build_reaction_context("Okay that makes sense, thanks!")
    assert ctx.user_challenges_julie is False


# ==========================================================
# build_reaction_context() -- determinism and independence of fields.
# ==========================================================


def test_build_reaction_context_is_deterministic():
    text = "That was a total blindside, what do you think??"
    a = build_reaction_context(text)
    b = build_reaction_context(text)
    assert a == b


def test_build_reaction_context_ordinary_message_is_all_defaults():
    ctx = build_reaction_context("Who is the current HOH?")
    assert ctx == ReactionContext()
