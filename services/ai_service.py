# services/ai_service.py
from __future__ import annotations

import os
import sqlite3

from google import genai
from google.genai import types
from groq import Groq

from config import DATABASE

# ==========================================================
# Providers
# ==========================================================
#
# Groq is tried first (free, fast, open-weight models, no hidden
# "thinking" tokens to trip over). Gemini is the fallback if Groq
# isn't configured or its call fails for any reason - rate limit,
# quota, network error, anything. Neither provider's outage takes
# Julie's chat down alone.
#
# GROQ_MODEL: openai/gpt-oss-120b. Groq deprecated llama-3.3-70b-
# versatile in June 2026 and recommends this as the replacement
# for general-purpose/quality workloads (console.groq.com/docs/
# deprecations) - a genuinely open-weight model (OpenAI's own
# open-source release), just hosted on Groq's infrastructure.

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
ai_client = (
    genai.Client(api_key=GEMINI_API_KEY)
    if GEMINI_API_KEY
    else None
)
GEMINI_MODEL = "gemini-3.6-flash"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
groq_client = (
    Groq(api_key=GROQ_API_KEY)
    if GROQ_API_KEY
    else None
)
GROQ_MODEL = "openai/gpt-oss-120b"

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


# ==========================================================
# Conversation history
# ==========================================================
#
# Stored and returned in a plain, provider-agnostic shape -
# list[tuple[role, text]], with role always "user" or "model" -
# and converted into each provider's own required format only at
# call time (_to_gemini_contents / _to_groq_messages below). This
# is what lets Groq and Gemini share one history without either
# provider's SDK shape leaking into storage.


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


def _recent_history(channel_id: int) -> list[tuple[str, str]]:
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

    return [(role, content) for role, content in reversed(rows)]


def update_and_get_history(
    channel_id: int,
    user_text: str,
) -> list[tuple[str, str]]:
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


def _to_gemini_contents(
    history: list[tuple[str, str]],
) -> list[types.Content]:
    """Converts stored (role, text) history into Gemini's Content shape."""

    return [
        types.Content(role=role, parts=[types.Part.from_text(text=text)])
        for role, text in history
    ]


def _to_groq_messages(
    history: list[tuple[str, str]],
    system_instruction: str,
) -> list[dict]:
    """Converts stored (role, text) history into OpenAI-shaped messages.

    Groq's API is OpenAI-compatible: role must be "system", "user", or
    "assistant" - "model" (Gemini's convention) is remapped here.
    """

    messages = [{"role": "system", "content": system_instruction}]

    for role, text in history:
        messages.append({
            "role": "assistant" if role == "model" else "user",
            "content": text,
        })

    return messages


# ==========================================================
# Game state (provider-agnostic - plain text either way)
# ==========================================================


def format_game_state(house_status, competition) -> str:
    """Formats currently tracked production data for the model's context.

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


# ==========================================================
# Gemini response parsing
# ==========================================================


def _extract_gemini_text(response) -> str:
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
                f"AI Service: Gemini finished with reason="
                f"{reason_name} (parts={len(parts)}, "
                f"text_length={len(text)})"
            )

        if text:
            return text

    except Exception as exc:
        print(
            f"AI Service: failed reading Gemini response parts directly "
            f"({exc}); falling back to response.text."
        )

    return getattr(response, "text", "") or ""


# ==========================================================
# Per-provider calls
# ==========================================================
#
# Each of these returns the reply text, or None on ANY failure -
# missing API key, network error, rate limit, quota, malformed
# response, anything. None means "try the next provider," never
# an exception the caller has to handle. This is what makes the
# fallback in generate_julie_response/generate_recap a plain
# sequential check rather than nested try/except.


def _try_groq_chat(
    messages: list[dict],
    max_tokens: int,
    temperature: float,
) -> str | None:

    if groq_client is None:
        return None

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            max_completion_tokens=max_tokens,
            temperature=temperature,
        )

        text = response.choices[0].message.content

        return text or None

    except Exception as exc:
        print(f"Groq Error: {exc}")
        return None


def _try_gemini_chat(
    contents,
    system_instruction: str,
    max_tokens: int,
    temperature: float,
) -> str | None:

    if ai_client is None:
        return None

    try:
        response = ai_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                max_output_tokens=max_tokens,
                temperature=temperature,
            ),
        )

        return _extract_gemini_text(response) or None

    except Exception as exc:
        print(f"AI Service Error: {exc}")
        return None


# ==========================================================
# Public entry points
# ==========================================================


async def generate_julie_response(
    channel_id: int,
    user_text: str,
    game_state: str = "",
) -> str:
    """Generates Julie's reply: Groq first, Gemini if Groq can't answer.

    game_state, when provided, is real tracked production data (current
    HOH, nominees, veto, etc.) appended to the system instruction so
    Julie answers accurately instead of deflecting on questions she
    actually has data for.
    """

    history = update_and_get_history(channel_id, user_text)

    system_instruction = SYSTEM_INSTRUCTION
    if game_state:
        system_instruction = f"{SYSTEM_INSTRUCTION}\n\n{game_state}"

    reply_text = _try_groq_chat(
        _to_groq_messages(history, system_instruction),
        max_tokens=2000,
        temperature=0.8,
    )

    if reply_text is None:
        reply_text = _try_gemini_chat(
            _to_gemini_contents(history),
            system_instruction,
            max_tokens=2000,
            temperature=0.8,
        )

    if reply_text is None:
        return (
            "⚠️ *Static feedback on the production headset*... Expect the unexpected, "
            "Houseguests! Both my Groq and Gemini feeds are down right now."
        )

    append_ai_response(channel_id, reply_text)
    return reply_text


async def generate_recap(
    entries: list[str],
    *,
    game_state: str = "",
    hamsterwatch_entries: list[str] | None = None,
) -> str:
    """Summarizes recent live-feed updates in Julie's voice: Groq
    first, Gemini if Groq can't answer.

    Combines up to three clearly-labeled sources in one prompt so the
    model never blurs where information came from:

        - game_state: real tracked production data (current HOH,
          nominees, veto, etc.) — same input format as
          generate_julie_response's game_state.
        - entries: recent Joker's Updates live-feed update strings.
        - hamsterwatch_entries: a small, pre-selected slice of
          Hamsterwatch recap history (never the whole archive —
          callers are expected to retrieve only what's relevant via
          HamsterwatchArchive.find_relevant before calling this).

    Unlike generate_julie_response, this is a one-off call with no
    persisted chat history — a recap is a summary, not a conversation.
    """

    if not entries and not hamsterwatch_entries and not game_state:
        return "Nothing new to recap yet, Houseguest."

    sections: list[str] = []

    if game_state:
        sections.append(f"=== CURRENT GAME STATE ===\n{game_state}")

    if entries:
        joined_entries = "\n".join(f"- {entry}" for entry in entries)
        sections.append(
            f"=== RECENT JOKER'S UPDATES (live feed, right now) ===\n{joined_entries}"
        )

    if hamsterwatch_entries:
        joined_hamsterwatch = "\n\n".join(hamsterwatch_entries)
        sections.append(
            "=== RELEVANT HAMSTERWATCH HISTORY (Dingo's Hamsterwatch recap "
            f"site, background context on earlier days) ===\n{joined_hamsterwatch}"
        )

    prompt = (
        "Summarize what's happening in the Big Brother house right now "
        "into a short, punchy recap (5-8 sentences max), in character. "
        "Group related moments together. Only use information present "
        "below; do not invent details. The sections below come from "
        "different sources on purpose - keep that straight: Joker's "
        "Updates is live, up-to-the-minute feed activity; Hamsterwatch "
        "is a fan recap site providing background on earlier days. Never "
        "present Hamsterwatch commentary as if it were a live Joker's "
        "Updates report, or vice versa.\n\n" + "\n\n".join(sections)
    )

    reply_text = _try_groq_chat(
        [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": prompt},
        ],
        max_tokens=2000,
        temperature=0.7,
    )

    if reply_text is None:
        reply_text = _try_gemini_chat(
            [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            SYSTEM_INSTRUCTION,
            max_tokens=2000,
            temperature=0.7,
        )

    if reply_text is None:
        return (
            "⚠️ *Static feedback on the production headset*... recap unavailable — "
            "both Groq and Gemini are down right now."
        )

    return reply_text
