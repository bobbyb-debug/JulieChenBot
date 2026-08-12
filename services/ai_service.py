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


def clear_history(channel_id: int) -> int:
    """Deletes all persisted chat history for a channel.

    Returns the number of messages removed.
    """

    connection = _connection()

    try:
        cursor = connection.execute(
            "DELETE FROM chat_messages WHERE channel_id = ?",
            (channel_id,),
        )
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()


def format_game_state(house_status, competition) -> str:
    """Formats currently tracked production data for Gemini's context.

    Only includes facts that are actually known. Explicitly instructs
    Julie not to guess beyond this list, since a wrong confident answer
    is worse than an honest "I don't know yet."
    """

    lines: list[str] = []

    if house_status.hoh:
        lines.append(f"Head of Household: {house_status.hoh}")

    if house_status.nominees:
        lines.append(
            f"Nominees: {', '.join(house_status.nominees)}"
        )

    if house_status.veto_holder:
        used = "used" if house_status.veto_used else "not yet used"
        lines.append(
            f"Power of Veto: held by {house_status.veto_holder} ({used})"
        )

    if house_status.have_nots:
        lines.append(
            f"Have-Nots: {', '.join(house_status.have_nots)}"
        )

    if house_status.feeds:
        lines.append(f"Feed status: {house_status.feeds}")

    if competition.active:
        lines.append(
            f"Competition currently in progress: {competition.competition.value}"
        )
    elif competition.winner:
        lines.append(
            f"Most recent competition winner: {competition.winner} "
            f"({competition.competition.value})"
        )

    if not lines:
        return ""

    return (
        "Current known Big Brother house state. Only state facts from "
        "this list when asked about game status. If something is not "
        "listed here, say you don't know yet rather than guessing:\n"
        + "\n".join(f"- {line}" for line in lines)
    )


def _extract_text(response) -> str:
    """Extracts response text directly from candidate parts, and logs
    the finish reason when generation stopped for any reason other
    than a normal completion.

    Reading parts directly, rather than trusting the response.text
    convenience property, guards against that property silently
    dropping content if a response ever spans multiple parts. The
    finish_reason log is what actually tells us, next time a reply
    cuts off mid-sentence, whether it was a token limit, a safety
    filter, or something else — logging it now costs nothing and
    turns a guess into an answer.
    """

    try:
        candidate = response.candidates[0]
        finish_reason = getattr(candidate, "finish_reason", None)
        parts = getattr(candidate.content, "parts", None) or []
        text = "".join(getattr(part, "text", "") or "" for part in parts)

        reason_name = getattr(finish_reason, "name", str(finish_reason))

        if reason_name not in ("STOP", "None"):
            print(
                f"AI Service: generation finished with reason="
                f"{reason_name} (parts={len(parts)}, "
                f"text_length={len(text)})"
            )

        if text:
            return text

    except Exception as exc:
        print(
            f"AI Service: failed reading response parts directly "
            f"({exc}); falling back to response.text."
        )

    return getattr(response, "text", "") or ""


async def generate_julie_response(
    channel_id: int,
    user_text: str,
    game_state: str = "",
) -> str:
    """Contacts Gemini using the correct context and returns the text.

    game_state, when provided, is real tracked production data (current
    HOH, nominees, veto, etc.) appended to the system instruction so
    Julie answers accurately instead of deflecting on questions she
    actually has data for.
    """
    if ai_client is None:
        return (
            "⚠️ Julie cannot answer right now because GEMINI_API_KEY is not configured. "
            "Please set GEMINI_API_KEY in your .env file and restart the bot."
        )

    try:
        conversation_history = update_and_get_history(channel_id, user_text)

        system_instruction = SYSTEM_INSTRUCTION
        if game_state:
            system_instruction = f"{SYSTEM_INSTRUCTION}\n\n{game_state}"

        response = ai_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=conversation_history,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                max_output_tokens=600,
                temperature=0.8,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )

        reply_text = _extract_text(response)
        append_ai_response(channel_id, reply_text)
        return reply_text

    except Exception as e:
        print(f"AI Service Error: {e}")
        return (
            "⚠️ *Static feedback on the production headset*... Expect the unexpected, "
            "Houseguests! My processors encountered an error."
        )


async def generate_recap(entries: list[str]) -> str:
    """Summarizes recent live-feed updates in Julie's voice.

    Unlike generate_julie_response, this is a one-off call with no
    persisted chat history — a recap is a summary, not a conversation.
    """

    if ai_client is None:
        return (
            "⚠️ Julie cannot summarize right now because GEMINI_API_KEY "
            "is not configured."
        )

    if not entries:
        return "Nothing new to recap yet, Houseguest."

    joined = "\n".join(f"- {entry}" for entry in entries)

    prompt = (
        "Summarize the following recent Big Brother live feed updates "
        "into a short, punchy recap (5-8 sentences max), in character. "
        "Group related moments together. Only use information present "
        "below; do not invent details.\n\n"
        f"{joined}"
    )

    try:
        response = ai_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=[
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=prompt)],
                )
            ],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                max_output_tokens=700,
                temperature=0.7,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return _extract_text(response)

    except Exception as e:
        print(f"AI Recap Error: {e}")
        return "⚠️ *Static feedback on the production headset*... recap unavailable right now."
