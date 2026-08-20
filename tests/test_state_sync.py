"""Tests for production/state_sync.py -- the registry of which taught
STATE topics have a directly comparable automated HouseStatus field,
used for conflict detection (admin_api/conflicts.py) and /teach
update's preview messaging. This module does NOT write to
HouseStatus -- official facts live exclusively in KnowledgeStore STATE
items (see production/knowledge.py); a manual update never touches
HouseStatus, which is exclusively the RSS pipeline's to write.
"""

from __future__ import annotations

from production.state_sync import RECOGNIZED_TOPICS, is_recognized_topic


def test_hoh_topic_is_recognized() -> None:
    assert is_recognized_topic("HOH") is True
    assert is_recognized_topic("hoh") is True  # case-insensitive


def test_all_expected_topics_are_recognized() -> None:
    for topic in ("HOH", "NOMINEES", "VETO_WINNER", "HAVE_NOTS"):
        assert is_recognized_topic(topic) is True


def test_unknown_topic_is_not_recognized() -> None:
    assert is_recognized_topic("FAVORITE_SNACK") is False


def test_topic_matching_is_case_insensitive() -> None:
    assert is_recognized_topic("hoh") is True
    assert is_recognized_topic("Nominees") is True


def test_recognized_topics_constant_is_exactly_the_documented_set() -> None:
    assert set(RECOGNIZED_TOPICS) == {"HOH", "NOMINEES", "VETO_WINNER", "HAVE_NOTS"}
