"""
Julie ChenBot Official-Topic Registry
=========================================

Identifies which taught STATE topics (production/knowledge.py
KnowledgeStore) have a directly comparable field on the automated,
live-feed-driven HouseStatus (production/house_status.py) -- used by
the admin API's conflict detector (admin_api/conflicts.py) to know
which topics can be diffed between the two, and by /teach update's
preview to warn a moderator when a topic they're setting won't be one
of the ones /hoh, /noms, /nominees, or /veto specifically read.

IMPORTANT: this module does NOT write to HouseStatus. Official game
facts (HOH, nominees, POV winner, have-nots, and anything else an
admin decides to track) live exclusively in KnowledgeStore STATE
items -- see production/knowledge.py KnowledgeStore.active_state().
HouseStatus is a separate, non-authoritative, automated live-feed
observation: it is updated only by production/engine.py's RSS
pipeline and is never written to by a manual /teach update or the
dashboard's POST /api/v1/state/apply. This split is what stops an
automated (and sometimes wrong) live-feed parse from silently
overwriting a manually confirmed fact.
"""

from __future__ import annotations

# Topics with a directly comparable field on HouseStatus (see
# admin_api/conflicts.py house_status_value()). A STATE item can be
# taught for any other topic too (e.g. "EVICTED", "VETO_USED") -- it
# is still recorded as an official fact, it simply has no automated
# equivalent to compare against for conflict detection.
RECOGNIZED_TOPICS = ("HOH", "NOMINEES", "VETO_WINNER", "HAVE_NOTS")


def is_recognized_topic(topic: str) -> bool:
    return topic.strip().upper() in RECOGNIZED_TOPICS
