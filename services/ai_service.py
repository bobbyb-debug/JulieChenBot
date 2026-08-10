# services/ai_service.py
from __future__ import annotations

import os

from google import genai
from google.genai import types

# Initialize Google GenAI client using the GEMINI_API_KEY environment variable.
# Set GEMINI_API_KEY in .env or your environment before starting the bot.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
ai_client = (
    genai.Client(api_key=GEMINI_API_KEY)
    if GEMINI_API_KEY
    else None
)

# Track memory locally in a dictionary mapped by channel ID.
memory_db: dict[int, list[types.Content]] = {}

# Match the iconic Big Brother production persona.
SYSTEM_INSTRUCTION = (
    "You are Julie ChenBot, the AI-powered Executive Producer companion of this Big Brother "
    "Discord server. Address users playfully as 'Houseguests'. Use your classic lines like "
    "'Expect the unexpected' and 'Good evening, Houseguests' naturally when starting "
    "conversations. Keep responses sharp, highly interactive, witty, and perfectly tailored "
    "for a fast-paced chat channel. Do not talk like a bland assistant; you control the game!"
)


def update_and_get_history(channel_id: int, user_text: str) -> list[types.Content]:
    """Manages chat history per channel to give Julie memory."""

    if channel_id not in memory_db:
        memory_db[channel_id] = []

    memory_db[channel_id].append(
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_text)],
        )
    )

    if len(memory_db[channel_id]) > 16:
        memory_db[channel_id] = memory_db[channel_id][-16:]

    return memory_db[channel_id]


def append_ai_response(channel_id: int, ai_text: str) -> None:
    """Appends Julie's final generated answer back into memory context."""
    if channel_id in memory_db:
        memory_db[channel_id].append(
            types.Content(
                role="model",
                parts=[types.Part.from_text(text=ai_text)],
            )
        )


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
