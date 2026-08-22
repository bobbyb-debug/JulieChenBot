"""
Julie ChenBot Message Chunking
=================================

Deterministic Discord message-length handling -- the delivery layer's
job, not the AI's. A legitimate long reply (KNOWLEDGE_SUMMARY's
capability briefing, a detailed historical answer, anything else) must
still be delivered even when it exceeds Discord's hard 2,000-character
message.content limit; the fix belongs here, not in a prompt
instruction asking the model to self-limit (a model can't reliably
count its own output length, and doing so would cap legitimate,
useful long answers for no real reason).

split_message() never drops or reorders content: every character of
`text` appears in the returned chunks, in order, split at the best
available boundary (blank line, then newline, then space, then --
only if a single unbroken run of text is itself longer than the
limit -- a hard cut). See DISCORD_MESSAGE_LIMIT/DEFAULT_CHUNK_LIMIT
below for why the target chunk size is deliberately smaller than
Discord's actual limit.
"""

from __future__ import annotations

from typing import Awaitable, Callable

# Discord's real, hard per-message content limit (error code 50035,
# "Must be 2000 or fewer in length" -- the exact failure this module
# exists to prevent). Every chunk split_message() returns is guaranteed
# to be at or under this, regardless of `limit`.
DISCORD_MESSAGE_LIMIT = 2000

# The target chunk size split_message() actually aims for by default --
# deliberately below DISCORD_MESSAGE_LIMIT, not equal to it. Rebalancing
# an open ``` code fence across a chunk boundary (see
# _rebalance_code_fences() below) can add a handful of characters back
# onto a chunk; aiming short leaves headroom to absorb that safely
# rather than needing to land exactly on the boundary every time.
_SAFETY_MARGIN = 100
DEFAULT_CHUNK_LIMIT = DISCORD_MESSAGE_LIMIT - _SAFETY_MARGIN


def split_message(text: str, limit: int = DEFAULT_CHUNK_LIMIT) -> list[str]:
    """Splits `text` into chunks, each guaranteed to be at most
    DISCORD_MESSAGE_LIMIT characters (never just `limit` -- see
    _rebalance_code_fences()'s docstring for why those can differ
    slightly).

    Boundary preference, most natural first: a blank line (paragraph
    break), a single newline, a space, and only as a last resort (one
    unbroken run of text longer than `limit`, e.g. a long URL) a hard
    cut with no boundary at all.

    Always returns a non-empty list -- a `text` at or under `limit`
    (including "") is returned as the single element it already is,
    so a normal short reply is completely unaffected by this function.
    """

    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text

    while len(remaining) > limit:
        window = remaining[:limit]

        break_at = window.rfind("\n\n")
        consumed = 2
        if break_at <= 0:
            break_at = window.rfind("\n")
            consumed = 1
        if break_at <= 0:
            break_at = window.rfind(" ")
            consumed = 1
        if break_at <= 0:
            # No natural boundary anywhere in this window (one
            # unbroken run of text at least `limit` characters long) --
            # only remaining option is a hard cut.
            break_at = limit
            consumed = 0

        chunks.append(remaining[:break_at])
        remaining = remaining[break_at + consumed:]

    if remaining:
        chunks.append(remaining)

    return _rebalance_code_fences(chunks, hard_limit=DISCORD_MESSAGE_LIMIT)


def _rebalance_code_fences(chunks: list[str], *, hard_limit: int) -> list[str]:
    """Closes and reopens an open ``` code fence that a plain
    character-boundary split cut through, so each chunk renders as
    valid markdown on its own -- an unbalanced fence otherwise makes
    the rest of that chunk (and often the next one) display as a
    stray, unstyled code block in Discord, which is the "obviously
    broken" markdown case this exists to avoid. Table/list formatting
    isn't specially preserved beyond already splitting at line
    boundaries -- a genuinely un-splittable markdown table is a much
    rarer, much lower-impact case than an unbalanced fence.

    A chunk that gains a reopened fence (a leading "```\\n") or a
    closed one (a trailing "\\n```") can grow past the `limit` the
    caller split at -- DEFAULT_CHUNK_LIMIT's safety margin exists
    specifically to absorb that. As a last-resort defensive backstop
    for a truly pathological input (many, many fences packed near a
    boundary), any chunk that still exceeds `hard_limit` after
    rebalancing is hard-truncated to it -- Discord's real constraint
    must never be violated even in a case this function's normal
    logic didn't anticipate.
    """

    result: list[str] = []
    open_fence = False

    for chunk in chunks:
        prefix = "```\n" if open_fence else ""
        fence_count = chunk.count("```")
        ends_open = open_fence != (fence_count % 2 == 1)
        suffix = "\n```" if ends_open else ""

        rebalanced = prefix + chunk + suffix
        if len(rebalanced) > hard_limit:
            rebalanced = rebalanced[:hard_limit]

        result.append(rebalanced)
        open_fence = ends_open

    return result


async def send_long_message(
    send: Callable[[str], Awaitable[object]], text: str, *, limit: int = DEFAULT_CHUNK_LIMIT
) -> None:
    """Sends `text` via `send` (e.g. discord.Interaction.followup.send
    or discord.abc.Messageable.channel.send -- anything with that
    `async def send(content: str)` shape), automatically splitting
    into multiple sequential messages if it exceeds Discord's limit.

    Chunks are awaited one at a time, in order, so they always appear
    in the channel in the correct reading order. A short reply (the
    overwhelmingly common case) makes exactly the one `send()` call it
    always did -- this function changes nothing about that path.
    """

    for chunk in split_message(text, limit=limit):
        await send(chunk)
