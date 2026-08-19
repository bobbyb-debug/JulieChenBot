"""
Julie ChenBot Admin API — Conflict Detection
==============================================

Compares official-facts STATE knowledge (production/knowledge.py --
the authoritative source /hoh, /noms, /nominees, and /veto read)
against the live, automated, RSS-driven HouseStatus (production/
house_status.py) for every topic that has a comparable field on both
(production/state_sync.py RECOGNIZED_TOPICS).

IMPORTANT: the two sides here are NOT equally authoritative. Taught
STATE is the official record, set only via /teach update or the
dashboard's POST /api/v1/state/apply. HouseStatus is an unverified,
automated live-feed observation that Julie's RSS parser updates every
production cycle on its own, with no human confirmation -- it can be
wrong (misparsed, stale, or simply premature). A disagreement here
does not mean "these two sources disagree and someone must decide
which wins" -- it means "the live feed is reporting something the
official record doesn't yet reflect," which the official record
always wins by construction (neither /teach update nor /state/apply
can be overridden by HouseStatus, and HouseStatus is never treated as
authoritative by any Discord command or by the AI chat context -- see
services/ai_service.py format_official_state()/format_game_state()).

This is read-only surfacing for a moderator to investigate -- it never
resolves anything automatically and never writes anything. Its
purpose is purely "the live feed may have new information worth
manually confirming," not "reconcile two equal sources."
"""

from __future__ import annotations

from production.engine import ProductionEngine
from production.house_status import HouseStatus
from production.state_sync import RECOGNIZED_TOPICS


def house_status_value(house_status: HouseStatus, topic: str) -> str:
    """Returns the live-feed HouseStatus field value for one
    recognized topic -- an unverified automated observation, not an
    official fact (see this module's docstring)."""

    if topic == "HOH":
        return house_status.hoh
    if topic == "NOMINEES":
        return ", ".join(house_status.nominees)
    if topic == "VETO_WINNER":
        return house_status.veto_holder
    if topic == "HAVE_NOTS":
        return ", ".join(house_status.have_nots)
    return ""


def detect_conflicts(engine: ProductionEngine) -> list[dict]:
    """Returns one entry per topic where the live-feed observation
    (house_status_value) disagrees with the official record (taught
    STATE), or where the live feed has observed something no official
    value has ever been set for. house_status_value is never
    authoritative -- see this module's docstring."""

    house_status = engine.watcher.house_status.current
    conflicts: list[dict] = []

    for topic in RECOGNIZED_TOPICS:
        live_value = house_status_value(house_status, topic)
        taught = engine.knowledge.active_state(topic)
        taught_value = taught.content if taught is not None else None

        if not live_value and not taught_value:
            continue

        if taught_value is None:
            conflicts.append(
                {
                    "topic": topic,
                    "house_status_value": live_value,
                    "taught_value": None,
                    "reason": (
                        "The live feed has observed a value for this topic, "
                        "but no official value has been set via the "
                        "dashboard yet -- /hoh, /nominees, and /veto will "
                        "show 'not confirmed' until an admin sets it."
                    ),
                }
            )
            continue

        if taught_value.strip().lower() != live_value.strip().lower():
            conflicts.append(
                {
                    "topic": topic,
                    "house_status_value": live_value,
                    "taught_value": taught_value,
                    "reason": (
                        "The live feed disagrees with the official value. "
                        "The live feed is not authoritative -- review it "
                        "and update Official State via the dashboard only "
                        "if it's correct."
                    ),
                }
            )

    return conflicts
