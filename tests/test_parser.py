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
