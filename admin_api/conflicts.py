"""
Julie ChenBot Admin API — Conflict Detection
==============================================

Compares taught STATE knowledge (production/knowledge.py) against the
live, automated-source-driven HouseStatus (production/house_status.py)
for every recognized topic (production/state_sync.py
RECOGNIZED_TOPICS).

Under normal operation these always agree: /teach update (and this
API's /state/apply) writes both together in one step (see
commands/teach.py _StateUpdateConfirmView.handle_confirm and
admin_api/routes.py state_apply()). A disagreement here means an
automated source (RSS/live-feed parsing) moved HouseStatus after the
last taught STATE, without a moderator ever seeing or approving the
new value.

This is read-only surfacing for a moderator to investigate -- it never
resolves anything automatically and never writes anything.
"""

from __future__ import annotations

from production.engine import ProductionEngine
from production.house_status import HouseStatus
from production.state_sync import RECOGNIZED_TOPICS


def house_status_value(house_status: HouseStatus, topic: str) -> str:
    """Returns the HouseStatus field value for one recognized topic.

    Mirrors production/state_sync.py apply_state_topic()'s topic ->
    field mapping, in the read direction.
    """

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
    """Returns one entry per recognized topic whose taught STATE and
    live HouseStatus value disagree (or where one exists and the
    other doesn't)."""

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
                        "An automated source has a value for this topic "
                        "with no corresponding taught STATE record."
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
                        "The taught STATE value and the live HouseStatus "
                        "value disagree."
                    ),
                }
            )

    return conflicts
