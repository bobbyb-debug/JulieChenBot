"""Regression tests for the parser's conditional/hypothetical-language
fix -- the exact production incident where a live-feed sentence shaped
like "If Yash wins HOH again..." was parsed as a completed HOH win of
"If Yash", persisted, and survived across restarts.

See production/parser.py's _LEADING_CONDITIONAL_WORDS docstring for
the exact mechanism: a capitalized "If"/"When"/etc. sitting directly
in front of a name gets swept INTO the win-patterns' own name-capture
group, bypassing the pre-existing _REPORTED_SPEECH_MARKERS check
entirely (that check only looks at text BEFORE the match).

tests/test_parser.py covers the parser's existing, still-passing
behavior -- this file covers only the new guard and the demonstrated
failure class it closes, across every extraction path that shares the
underlying risk (HOH/POV/other-competition winners, nominations, veto
usage).
"""

from __future__ import annotations

from production.competition import CompetitionType
from production.parser import ProductionParser
from production.rss import FeedUpdate


def _parse(title: str, description: str = ""):
    parser = ProductionParser()
    update = FeedUpdate(
        guid="g", title=title, description=description, link="l", published="p"
    )
    return parser.parse(update)


# ==========================================================
# HOH -- the exact production incident
# ==========================================================


def test_yash_won_hoh_still_parses_correctly():
    result = _parse("Yash won HOH.")
    assert "hoh" in result.fields
    assert result.house_status.hoh == "Yash"


def test_drew_wins_hoh_still_parses_correctly():
    result = _parse("Drew wins HOH.")
    assert "hoh" in result.fields
    assert result.house_status.hoh == "Drew"


def test_if_yash_wins_hoh_again_does_not_parse_as_a_completed_win():
    result = _parse("If Yash wins HOH again, he will probably nominate Barrett.")
    assert "hoh" not in result.fields
    assert result.house_status.hoh == ""
    assert result.competition.winner == ""


def test_if_yash_were_to_win_hoh_does_not_parse():
    result = _parse("If Yash were to win HOH, things would change completely.")
    assert "hoh" not in result.fields
    assert result.house_status.hoh == ""


def test_i_think_yash_wins_hoh_next_week_does_not_parse():
    result = _parse("I think Yash wins HOH next week.")
    assert "hoh" not in result.fields


def test_reported_hypothetical_mid_sentence_still_caught():
    result = _parse("Barrett wonders if Yash wins HOH again.")
    assert "hoh" not in result.fields


def test_when_clause_does_not_parse_as_hoh_win():
    result = _parse("When Yash wins HOH, the house will be shocked.")
    assert "hoh" not in result.fields


# ==========================================================
# POV -- shares _is_genuine_announcement() with HOH
# ==========================================================


def test_taylor_won_pov_still_parses_correctly():
    result = _parse("Taylor won the Power of Veto.")
    assert "veto_holder" in result.fields
    assert result.house_status.veto_holder == "Taylor"


def test_if_taylor_wins_veto_does_not_parse():
    result = _parse("If Taylor wins veto, she'll probably save herself.")
    assert "veto_holder" not in result.fields
    assert result.house_status.veto_holder == ""


# ==========================================================
# Other competitions (AI Arena / Battle Back / Luxury) -- also share
# _is_genuine_announcement().
# ==========================================================


def test_luxury_competition_win_still_parses_correctly():
    result = _parse("Melody won the Luxury competition.")
    assert result.competition.competition == CompetitionType.LUXURY
    assert result.competition.winner == "Melody"


def test_if_melody_wins_luxury_does_not_parse():
    result = _parse("If Melody wins Luxury, she'll pick the reward.")
    assert result.competition.winner == ""


# ==========================================================
# Nominations -- a separate extraction path with no prior guard at all.
# ==========================================================


def test_nominees_are_line_still_parses_correctly():
    result = _parse("Nominees are Drew and LaLa.")
    assert "nominees" in result.fields
    assert result.house_status.nominees == ("Drew", "LaLa")


def test_were_nominated_line_still_parses_correctly():
    result = _parse("Drew and LaLa were nominated for eviction.")
    assert "nominees" in result.fields
    assert set(result.house_status.nominees) == {"Drew", "LaLa"}


def test_if_the_nominees_are_does_not_parse():
    result = _parse("If the nominees are Drew and LaLa, that would be huge.")
    assert "nominees" not in result.fields
    assert result.house_status.nominees == ()


def test_if_drew_and_lala_were_nominated_does_not_parse():
    result = _parse("If Drew and LaLa were nominated, the house would flip.")
    assert "nominees" not in result.fields


# ==========================================================
# Veto usage -- plain substring matching, no capture group; needed its
# own position-aware guard (_is_hypothetical_mention()).
# ==========================================================


def test_veto_was_used_still_parses_correctly():
    result = _parse("The veto was used on Drew today.")
    assert "veto_used" in result.fields
    assert result.house_status.veto_used is True


def test_veto_was_not_used_still_parses_correctly():
    result = _parse("The veto was not used this week.")
    assert "veto_used" in result.fields
    assert result.house_status.veto_used is False


def test_if_dee_uses_the_veto_does_not_parse():
    result = _parse("If Dee uses the veto, it changes everything for Drew.")
    assert "veto_used" not in result.fields


# ==========================================================
# Regression guard: a genuine name that happens to START with one of
# the guarded words as a SUBSTRING (not the whole first word) must
# still parse -- the check is on the whole first token, not a prefix.
# ==========================================================


def test_name_containing_but_not_equal_to_a_guard_word_still_parses():
    # "Sinced" is not a real name, but this proves the check is an
    # exact-word match on the first token, not a substring/prefix scan
    # that could false-positive on a real name sharing a prefix.
    result = _parse("Sinced won HOH.")
    assert result.house_status.hoh == "Sinced"
