"""Tests for timezone-safe ProductionEvent timestamps."""

from datetime import UTC, datetime, timedelta

from production.events import EventType, ProductionEvent


def test_new_event_uses_timezone_aware_utc_timestamp() -> None:
    event = ProductionEvent(
        source="Test",
        event_type=EventType.SYSTEM,
        title="Test",
        detail="Test event",
    )

    assert event.created_at.tzinfo is UTC
    assert event.created_at.utcoffset() == timedelta(0)


def test_naive_legacy_timestamp_is_normalized_to_utc() -> None:
    legacy_timestamp = datetime(2026, 8, 9, 6, 0, 0)

    event = ProductionEvent(
        source="Test",
        event_type=EventType.SYSTEM,
        title="Test",
        detail="Legacy timestamp",
        created_at=legacy_timestamp,
    )

    assert event.created_at == legacy_timestamp.replace(tzinfo=UTC)
    assert event.created_at.tzinfo is UTC


def test_aware_timestamp_is_converted_to_utc() -> None:
    source_timestamp = datetime.fromisoformat("2026-08-09T01:00:00-05:00")

    event = ProductionEvent(
        source="Test",
        event_type=EventType.SYSTEM,
        title="Test",
        detail="Timezone conversion",
        created_at=source_timestamp,
    )

    assert event.created_at == datetime.fromisoformat("2026-08-09T06:00:00+00:00")


def test_age_seconds_uses_aware_utc_now() -> None:
    event = ProductionEvent(
        source="Test",
        event_type=EventType.SYSTEM,
        title="Test",
        detail="Age test",
        created_at=datetime.now(UTC) - timedelta(seconds=5),
    )

    assert 4 <= event.age_seconds <= 7


# ==========================================================
# delivered_to serialization (A3 durable event delivery)
# ==========================================================
#
# delivered_to is a Python set, which json.dumps() cannot serialize
# directly (see database/storage.py Storage.save()). to_dict()/
# from_dict() are what ProductionEngine._persist_pending_events()/
# _load_pending_events() use to make it JSON-safe.


def test_to_dict_serializes_delivered_to_as_sorted_list() -> None:
    event = ProductionEvent(
        source="Test",
        event_type=EventType.HOH_CHANGED,
        title="Test",
        detail="Partial delivery",
        delivered_to={"live-updates", "house-status"},
    )

    data = event.to_dict()

    assert data["delivered_to"] == ["house-status", "live-updates"]


def test_from_dict_restores_delivered_to_as_set() -> None:
    original = ProductionEvent(
        source="Test",
        event_type=EventType.HOH_CHANGED,
        title="Test",
        detail="Partial delivery",
        delivered_to={"house-status"},
    )

    restored = ProductionEvent.from_dict(original.to_dict())

    assert restored.delivered_to == {"house-status"}
    assert isinstance(restored.delivered_to, set)


def test_from_dict_defaults_missing_delivered_to_to_empty_set() -> None:
    """Old persisted dicts (written before delivered_to existed) must
    still load cleanly, with nothing marked delivered."""

    data = ProductionEvent(
        source="Test",
        event_type=EventType.SYSTEM,
        title="Test",
        detail="Legacy record",
    ).to_dict()
    del data["delivered_to"]

    restored = ProductionEvent.from_dict(data)

    assert restored.delivered_to == set()
