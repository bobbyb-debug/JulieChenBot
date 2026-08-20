"""Tests for chat_messages identity tracking and its migration
(services/ai_service.py): author_id/author_name columns added to the
existing table, old rows preserved untouched, new rows populated, and
provider message builders attributing user turns to their speaker.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import services.ai_service as ai_service


def test_fresh_database_has_author_columns(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat.db")

    connection = ai_service._connection()
    columns = {row[1] for row in connection.execute("PRAGMA table_info(chat_messages)")}
    connection.close()

    assert {"channel_id", "role", "content", "created_at", "author_id", "author_name"} <= columns


def test_migration_adds_columns_to_pre_existing_database(tmp_path: Path, monkeypatch) -> None:
    """Simulates a chat_history.db created before identity tracking
    existed: the old 4-column schema, with real rows already in it.
    Opening it through ai_service._connection() must add the new
    columns without touching a single existing row.
    """

    db_file = tmp_path / "legacy_chat.db"
    monkeypatch.setattr(ai_service, "CHAT_HISTORY_FILE", db_file)

    legacy = sqlite3.connect(db_file)
    legacy.execute(
        """
        CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'model')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    legacy.execute(
        "INSERT INTO chat_messages (channel_id, role, content) VALUES (?, ?, ?)",
        (100, "user", "a message from before identity tracking existed"),
    )
    legacy.commit()
    legacy.close()

    # Opening through the real code path must migrate in place.
    history = ai_service._recent_history(100)

    assert history == [
        ("user", "a message from before identity tracking existed", None)
    ]

    connection = ai_service._connection()
    columns = {row[1] for row in connection.execute("PRAGMA table_info(chat_messages)")}
    row_count = connection.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
    connection.close()

    assert {"author_id", "author_name"} <= columns
    assert row_count == 1  # the legacy row was never touched/deleted


def test_migration_is_idempotent_across_repeated_connections(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat.db")

    # _connection() runs the migration check on every call -- opening
    # it repeatedly must never raise "duplicate column".
    for _ in range(3):
        ai_service._connection().close()


def test_new_messages_persist_author_identity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat.db")

    ai_service.update_and_get_history(
        200, "hello Julie", author_id=555, author_name="Bobby"
    )

    history = ai_service._recent_history(200)
    assert history == [("user", "hello Julie", "Bobby")]


def test_model_replies_have_no_author(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat.db")

    ai_service.update_and_get_history(300, "hi", author_id=1, author_name="Bobby")
    ai_service.append_ai_response(300, "Good evening, Houseguest.")

    history = ai_service._recent_history(300)
    assert history[-1] == ("model", "Good evening, Houseguest.", None)


def test_clear_history_still_removes_all_rows_for_a_channel(
    tmp_path: Path, monkeypatch
) -> None:
    """/forget (commands/forget.py clear_history()) must keep working
    unchanged after the schema gained identity columns."""

    monkeypatch.setattr(ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat.db")

    ai_service.update_and_get_history(400, "hi", author_id=1, author_name="Bobby")
    ai_service.append_ai_response(400, "hello")

    removed = ai_service.clear_history(400)

    assert removed == 2
    assert ai_service._recent_history(400) == []


# ==========================================================
# Provider message attribution
# ==========================================================


def test_speaker_prefix_applies_only_to_named_user_turns() -> None:
    assert ai_service._speaker_prefix("user", "Bobby") == "Bobby: "
    assert ai_service._speaker_prefix("user", None) == ""
    assert ai_service._speaker_prefix("model", "Bobby") == ""  # never Julie's own turns


def test_groq_messages_attribute_multiple_speakers_in_one_channel() -> None:
    history = [
        ("user", "who is HoH?", "Bobby"),
        ("model", "Yash is HoH.", None),
        ("user", "are you sure?", "Alex"),
    ]
    messages = ai_service._to_groq_messages(history, "system")

    assert messages[1]["content"] == "Bobby: who is HoH?"
    assert messages[2]["content"] == "Yash is HoH."
    assert messages[3]["content"] == "Alex: are you sure?"
