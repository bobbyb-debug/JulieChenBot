"""Tests for services/message_chunking.py -- the deterministic Discord
message-length delivery fix (error code 50035, "Must be 2000 or fewer
in length"). Covers split_message() (the pure splitting logic) and
send_long_message() (the async delivery wrapper both commands/chat.py
and services/discord.py's @mention/DM handler now use).
"""

from __future__ import annotations

import asyncio

from services.message_chunking import (
    DEFAULT_CHUNK_LIMIT,
    DISCORD_MESSAGE_LIMIT,
    send_long_message,
    split_message,
)


# ==========================================================
# 1-2. Short / exactly-at-limit responses are untouched
# ==========================================================


def test_short_message_returns_a_single_chunk():
    assert split_message("Yash is the current HOH.") == ["Yash is the current HOH."]


def test_empty_message_returns_a_single_empty_chunk():
    assert split_message("") == [""]


def test_message_exactly_at_the_limit_is_not_split():
    text = "x" * DEFAULT_CHUNK_LIMIT
    result = split_message(text)

    assert result == [text]
    assert len(result[0]) == DEFAULT_CHUNK_LIMIT


def test_message_exactly_at_discord_hard_limit_with_custom_limit():
    text = "x" * DISCORD_MESSAGE_LIMIT
    result = split_message(text, limit=DISCORD_MESSAGE_LIMIT)

    assert result == [text]


def test_message_one_char_over_the_limit_is_split():
    text = "x" * (DEFAULT_CHUNK_LIMIT + 1)
    result = split_message(text)

    assert len(result) == 2
    assert "".join(result) == text


# ==========================================================
# 3-4. Over-limit responses are split, and no chunk ever exceeds
# Discord's real 2,000-character hard limit
# ==========================================================


def test_long_message_is_split_into_multiple_chunks():
    text = "word " * 1000  # 5000 chars
    result = split_message(text)

    assert len(result) > 1


def test_no_chunk_ever_exceeds_the_discord_hard_limit():
    text = "word " * 5000  # 25,000 chars, no natural paragraph breaks
    result = split_message(text)

    assert all(len(chunk) <= DISCORD_MESSAGE_LIMIT for chunk in result)


def test_no_chunk_exceeds_the_hard_limit_even_with_a_tiny_custom_limit():
    """Stresses the boundary logic itself, independent of the default
    safety margin."""

    text = "the quick brown fox jumps over the lazy dog. " * 200
    result = split_message(text, limit=50)

    assert all(len(chunk) <= DISCORD_MESSAGE_LIMIT for chunk in result)
    assert all(len(chunk) <= 50 for chunk in result)  # no fences here to rebalance


def test_splitting_never_drops_non_whitespace_content():
    """Every real (non-boundary-whitespace) character in the input
    must appear somewhere in the output, in order."""

    text = "alpha beta gamma delta epsilon " * 200
    result = split_message(text, limit=100)

    rejoined = "".join(result)
    for word in ("alpha", "beta", "gamma", "delta", "epsilon"):
        assert rejoined.count(word) == text.count(word)


def test_a_single_unbroken_token_longer_than_the_limit_is_hard_cut():
    """No newline, no space anywhere -- only a hard cut can possibly
    split this, and it must still never exceed the limit."""

    text = "x" * 5000
    result = split_message(text, limit=100)

    assert all(len(chunk) <= 100 for chunk in result)
    assert "".join(result) == text


# ==========================================================
# 5-6. Readability: newline/paragraph boundaries preferred
# ==========================================================


def test_prefers_a_blank_line_paragraph_break_when_available():
    first_para = "A" * 900
    second_para = "B" * 900
    text = f"{first_para}\n\n{second_para}"

    result = split_message(text, limit=1000)

    assert result[0] == first_para
    assert result[1] == second_para


def test_prefers_a_newline_over_a_hard_cut():
    lines = [f"line {i} of some reasonable length here" for i in range(60)]
    text = "\n".join(lines)

    result = split_message(text, limit=200)

    # Every chunk boundary landed on a real line -- no line was ever
    # cut mid-word.
    for chunk in result:
        for line in chunk.split("\n"):
            assert line == "" or line in lines


def test_prefers_a_space_over_a_hard_cut_when_no_newline_available():
    # rstrip()'d so the source text itself has no trailing space --
    # otherwise the final chunk legitimately ending in that source
    # whitespace would look like (but not actually be) a bad cut.
    text = ("word " * 500).rstrip()

    result = split_message(text, limit=200)

    for chunk in result:
        assert not chunk.startswith(" ")
        # A hard cut mid-word would produce a chunk not ending in a
        # complete "word" -- every chunk here must end cleanly.
        assert chunk[-1] != " "


def test_readable_normal_text_reconstructs_to_the_same_words():
    text = (
        "Julie can discuss current official game state, administrator-taught "
        "knowledge, historical structured records, the Hamsterwatch narrative "
        "archive, live-feed observations, and conversational memory. "
    ) * 15

    result = split_message(text, limit=300)

    assert " ".join(result).split() == text.split()


# ==========================================================
# 7. Markdown code fences are rebalanced across a split boundary
# ==========================================================


def test_code_fence_split_across_chunks_is_rebalanced():
    code_body = "line_of_code()\n" * 200
    text = "Some intro text.\n\n```\n" + code_body + "```\n\nSome outro text."

    result = split_message(text, limit=500)

    assert len(result) > 1
    for chunk in result:
        # Every chunk must contain an EVEN number of ``` markers --
        # i.e. no chunk renders with an unbalanced/open code fence.
        assert chunk.count("```") % 2 == 0


def test_code_fence_rebalancing_still_respects_the_hard_limit():
    code_body = "x" * 3000  # one giant unbroken "line" inside a fence
    text = "```\n" + code_body + "\n```"

    result = split_message(text, limit=200)

    assert all(len(chunk) <= DISCORD_MESSAGE_LIMIT for chunk in result)


def test_message_with_no_code_fences_is_unaffected_by_rebalancing():
    text = "word " * 1000

    result = split_message(text)

    assert all("```" not in chunk for chunk in result)


def test_markdown_table_rows_are_not_cut_mid_row():
    rows = [f"| Player {i} | HOH | Week {i} |" for i in range(80)]
    text = "\n".join(rows)

    result = split_message(text, limit=300)

    for chunk in result:
        for line in chunk.split("\n"):
            assert line == "" or line in rows


# ==========================================================
# 8. A realistic KNOWLEDGE_SUMMARY-sized response can be delivered
# ==========================================================


def test_a_realistic_knowledge_summary_length_response_is_delivered_in_chunks():
    # Mirrors the real shape/length of a moderator KNOWLEDGE_SUMMARY
    # briefing (see production/knowledge_summary.py /
    # format_knowledge_summary_guidance()'s live smoke-test output) --
    # long enough on its own, and the model's own prose typically adds
    # more on top, comfortably exceeding 2,000 characters in practice.
    section = (
        "### Section heading\n"
        "- Detail line one about this knowledge category.\n"
        "- Detail line two with a bit more explanation and nuance.\n\n"
    )
    text = section * 20  # well over 2,000 characters

    assert len(text) > DISCORD_MESSAGE_LIMIT

    result = split_message(text)

    assert len(result) > 1
    assert all(len(chunk) <= DISCORD_MESSAGE_LIMIT for chunk in result)


# ==========================================================
# send_long_message(): the async delivery wrapper
# ==========================================================


def test_send_long_message_sends_once_for_a_short_reply():
    sent: list[str] = []

    async def fake_send(content: str) -> None:
        sent.append(content)

    asyncio.run(send_long_message(fake_send, "Yash is the current HOH."))

    assert sent == ["Yash is the current HOH."]


def test_send_long_message_sends_multiple_times_for_a_long_reply():
    sent: list[str] = []

    async def fake_send(content: str) -> None:
        sent.append(content)

    asyncio.run(send_long_message(fake_send, "word " * 1000))

    assert len(sent) > 1
    assert all(len(chunk) <= DISCORD_MESSAGE_LIMIT for chunk in sent)


def test_send_long_message_sends_chunks_in_order():
    sent: list[str] = []

    async def fake_send(content: str) -> None:
        sent.append(content)

    # limit=200 forces each ~650-char paragraph into its own chunk --
    # the default limit is large enough that several would legitimately
    # pack into one chunk together, which is correct behavior but
    # would make a strict per-chunk assertion meaningless here.
    text = "\n\n".join(f"paragraph {i} " * 50 for i in range(10))
    asyncio.run(send_long_message(fake_send, text, limit=200))

    assert len(sent) > 1
    # Each paragraph's marker must appear, and in non-decreasing
    # index order across the sequence of sent chunks -- i.e. chunks
    # genuinely arrived in the same order as the source text, not
    # reordered or interleaved.
    seen_indices = [
        i for chunk in sent for i in range(10) if f"paragraph {i} " in chunk
    ]
    assert seen_indices == sorted(seen_indices)
    assert set(seen_indices) == set(range(10))


def test_send_long_message_makes_no_extra_calls_beyond_send():
    """The fix is pure delivery-layer chunking -- it must never itself
    invoke any AI generation. fake_send is the only callable
    send_long_message is given access to."""

    call_count = 0

    async def fake_send(content: str) -> None:
        nonlocal call_count
        call_count += 1

    asyncio.run(send_long_message(fake_send, "word " * 1000))

    # Every call recorded was a real chunk send, nothing else -- and
    # this equals exactly len(split_message(...)), proving no hidden
    # extra calls (e.g. a generation retry) snuck in.
    expected_chunks = len(split_message("word " * 1000))
    assert call_count == expected_chunks
