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
