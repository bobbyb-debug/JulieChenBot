"""Tests for JokersUpdates RSS production parsing."""

from production.competition import CompetitionType
from production.parser import ProductionParser
from production.rss import FeedUpdate


def make_update(title: str, description: str = "") -> FeedUpdate:
    return FeedUpdate(
        guid="test-guid",
        title=title,
        description=description,
        link="https://example.test/update",
        published="2026-08-08T00:00:00Z",
    )


def test_parser_extracts_hoh() -> None:
    parser = ProductionParser()

    parsed = parser.parse(
        make_update("Morgan won HOH")
    )

    assert parsed.recognized is True
    assert parsed.house_status.hoh == "Morgan"
    assert parsed.competition.competition == CompetitionType.HOH
    assert parsed.competition.winner == "Morgan"


def test_parser_extracts_pov_and_preserves_hoh() -> None:
    parser = ProductionParser()
    parser.parse(make_update("Morgan won HOH"))

    parsed = parser.parse(
        make_update("Taylor won the Power of Veto")
    )

    assert parsed.house_status.hoh == "Morgan"
    assert parsed.house_status.veto_holder == "Taylor"
    assert parsed.competition.competition == CompetitionType.POV
    assert parsed.competition.winner == "Taylor"


def test_parser_extracts_nominations_have_nots_and_feed_state() -> None:
    parser = ProductionParser()

    parsed = parser.parse(
        make_update(
            "Nominees are Alex and Jordan. "
            "Have-Nots are Casey and Drew. "
            "Live feeds are down."
        )
    )

    assert parsed.house_status.nominees == ("Alex", "Jordan")
    assert parsed.house_status.have_nots == ("Casey", "Drew")
    assert parsed.house_status.feeds == "down"
    assert set(("nominees", "have_nots", "feeds")).issubset(parsed.fields)


def test_parser_extracts_veto_usage() -> None:
    parser = ProductionParser()

    parsed = parser.parse(
        make_update("The veto was used at the ceremony")
    )

    assert parsed.house_status.veto_used is True
    assert "veto_used" in parsed.fields


def test_parser_ignores_unrecognized_update() -> None:
    parser = ProductionParser()

    parsed = parser.parse(
        make_update("Houseguests are talking in the backyard")
    )

    assert parsed.recognized is False
    assert parsed.fields == ()
    assert parsed.house_status.hoh == ""
    assert parsed.competition.competition == CompetitionType.NONE


# ==========================================================
# False-positive regression: reported/hypothetical speech
# ==========================================================
#
# Observed in production: a real RSS item reading roughly "Drew and
# Melody on the couch talking in the bathroom. Drew brings up if
# Yash won HOH" was parsed as a genuine HOH change and announced to
# Discord, even though no HOH change had actually occurred (verified
# against Hamsterwatch's official Power Status, which still showed
# the previous HOH). Two bugs combined to cause this:
#
# 1. re.IGNORECASE case-folds the ENTIRE pattern it's applied to,
#    including the [A-Z] meant to require a capitalized name. Under
#    that flag, [A-Z] matches lowercase letters too, so the greedy
#    repeated name group swallowed the whole preceding sentence as
#    "the name" instead of stopping at a real name boundary.
#
# 2. Nothing distinguished a direct announcement from a houseguest
#    referencing a past or hypothetical outcome in conversation --
#    exactly what BB live-feed recaps are full of.


def test_parser_ignores_hypothetical_mention_of_hoh() -> None:
    """The exact shape of the real production false positive."""
    parser = ProductionParser()

    parsed = parser.parse(
        make_update(
            "Drew and Melody on the couch talking in the bathroom. "
            "Drew brings up if Yash won HOH"
        )
    )

    assert parsed.recognized is False
    assert parsed.house_status.hoh == ""


def test_parser_ignores_recalled_past_result() -> None:
    parser = ProductionParser()

    parsed = parser.parse(
        make_update("They recalled Morgan won HOH earlier this season")
    )

    assert parsed.recognized is False
    assert parsed.house_status.hoh == ""


def test_parser_ignores_wondered_hypothetical() -> None:
    parser = ProductionParser()

    parsed = parser.parse(
        make_update("Drew wondered whether Yash won HOH or not")
    )

    assert parsed.recognized is False


def test_parser_ignores_reported_veto_mention() -> None:
    parser = ProductionParser()

    parsed = parser.parse(
        make_update("Dee said Devens won the veto last week")
    )

    assert parsed.recognized is False
    assert parsed.house_status.veto_holder == ""


def test_parser_does_not_swallow_trailing_words_into_name() -> None:
    """Guards the greedy-match half of the bug directly: even in a
    genuine announcement, the captured name must stop at the real
    name boundary, not run on into following words."""

    parser = ProductionParser()

    parsed = parser.parse(
        make_update("HOH is Taylor after a close final round")
    )

    assert parsed.house_status.hoh == "Taylor"


def test_parser_still_extracts_genuine_hoh_after_fix() -> None:
    """A direct announcement, not preceded by any reported-speech
    marker, must still be recognized."""

    parser = ProductionParser()

    parsed = parser.parse(
        make_update("BREAKING: Yash won HOH in tonight's competition")
    )

    assert parsed.recognized is True
    assert parsed.house_status.hoh == "Yash"


# ==========================================================
# False-positive regression: "have" + word starting with "not"
# ==========================================================
#
# Observed in production: an RSS item containing ordinary conversational
# text like "I have nothing against you" was parsed as a Have-Not
# announcement and produced the corrupted entry "hing against you" in
# Discord. _HAVE_NOT_PATTERN's "nots?" had no trailing word boundary,
# so it matched the first three letters of ANY word starting with
# "not" ("nothing", "noticed", "notified", ...) as long as it followed
# "have ". A trailing \b now requires "not"/"nots" to be a whole word.


def test_parser_still_extracts_genuine_have_not_announcement() -> None:
    """The word-boundary fix must not break legitimate announcements."""

    parser = ProductionParser()

    parsed = parser.parse(
        make_update("The Have-Nots are Chuk, Lyric, Jason and Rome")
    )

    assert parsed.house_status.have_nots == ("Chuk", "Lyric", "Jason", "Rome")
    assert "have_nots" in parsed.fields


def test_parser_extracts_singular_have_not_announcement() -> None:
    """The singular form ('Have-Not is', no trailing 's') must still work."""

    parser = ProductionParser()

    parsed = parser.parse(
        make_update("Have-Not is Jason.")
    )

    assert parsed.house_status.have_nots == ("Jason",)


def test_parser_ignores_have_nothing_against_you() -> None:
    """The exact shape of the real production false positive."""

    parser = ProductionParser()

    parsed = parser.parse(
        make_update("Rome says I have nothing against you, just game talk.")
    )

    assert "have_nots" not in parsed.fields
    assert parsed.house_status.have_nots == ()


def test_parser_ignores_have_noticed() -> None:
    parser = ProductionParser()

    parsed = parser.parse(
        make_update("Chuk says he and Lyric have noticed some shady conversations.")
    )

    assert "have_nots" not in parsed.fields
    assert parsed.house_status.have_nots == ()


def test_parser_ignores_have_notified() -> None:
    parser = ProductionParser()

    parsed = parser.parse(
        make_update("Production says they have notified the houseguests of the twist.")
    )

    assert "have_nots" not in parsed.fields
    assert parsed.house_status.have_nots == ()
