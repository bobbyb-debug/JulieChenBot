# services/ai_service.py
from __future__ import annotations

import os
import sqlite3

from google import genai
from google.genai import types

from config import DATABASE

# Initialize Google GenAI client using the GEMINI_API_KEY environment variable.
# Set GEMINI_API_KEY in .env or your environment before starting the bot.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
ai_client = (
    genai.Client(api_key=GEMINI_API_KEY)
    if GEMINI_API_KEY
    else None
)

CHAT_HISTORY_FILE = DATABASE / "chat_history.db"
MAX_CONTEXT_MESSAGES = 16

# Match the iconic Big Brother production persona.
SYSTEM_INSTRUCTION = (
    "You are Julie ChenBot, the AI-powered Executive Producer companion of this Big Brother "
    "Discord server. Address users playfully as 'Houseguests'. Use your classic lines like "
    "'Expect the unexpected' and 'Good evening, Houseguests' naturally when starting "
    "conversations. Keep responses sharp, highly interactive, witty, and perfectly tailored "
    "for a fast-paced chat channel. Do not talk like a bland assistant; you control the game!"
)


def _connection() -> sqlite3.Connection:
    """Opens the local, persistent conversation-history database."""

    connection = sqlite3.connect(CHAT_HISTORY_FILE)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'model')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return connection


def _append_message(channel_id: int, role: str, text: str) -> None:
    """Persists one message so conversation context survives restarts."""

    connection = _connection()

    try:
        connection.execute(
            """
            INSERT INTO chat_messages (channel_id, role, content)
            VALUES (?, ?, ?)
            """,
            (channel_id, role, text),
        )
        connection.commit()
    finally:
        connection.close()


def _recent_history(channel_id: int) -> list[types.Content]:
    """Returns the latest context window in chronological order."""

    connection = _connection()

    try:
        rows = connection.execute(
            """
            SELECT role, content
            FROM chat_messages
            WHERE channel_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (channel_id, MAX_CONTEXT_MESSAGES),
        ).fetchall()
    finally:
        connection.close()

    return [
        types.Content(
            role=role,
            parts=[types.Part.from_text(text=content)],
        )
        for role, content in reversed(rows)
    ]


def update_and_get_history(channel_id: int, user_text: str) -> list[types.Content]:
    """Saves a user message and returns recent persistent conversation context."""

    _append_message(channel_id, "user", user_text)
    return _recent_history(channel_id)


def append_ai_response(channel_id: int, ai_text: str) -> None:
    """Saves Julie's final reply for future conversation context."""

    _append_message(channel_id, "model", ai_text)


async def generate_julie_response(channel_id: int, user_text: str) -> str:
    """Contacts Gemini using the correct context and returns the text."""
    if ai_client is None:
        return (
            "⚠️ Julie cannot answer right now because GEMINI_API_KEY is not configured. "
            "Please set GEMINI_API_KEY in your .env file and restart the bot."
        )

    try:
        conversation_history = update_and_get_history(channel_id, user_text)

        response = ai_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=conversation_history,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                max_output_tokens=400,
                temperature=0.8,
            ),
        )

        append_ai_response(channel_id, response.text)
        return response.text

    except Exception as e:
        print(f"AI Service Error: {e}")
        return (
            "⚠️ *Static feedback on the production headset*... Expect the unexpected, "
            "Houseguests! My processors encountered an error."
        )
