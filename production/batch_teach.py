"""
Julie ChenBot Batch Teaching
=============================

Parses a moderator's pasted multi-line training batch (one FACT:/RULE:/
STATE: instruction per line) into a deterministic, previewable plan,
and applies that plan to a KnowledgeStore.

This module owns parsing and planning only -- it never talks to
Discord (see commands/teach.py for the /teach batch command and its
Confirm/Cancel UI) and never calls an AI provider. Conflict/duplicate
detection here is purely deterministic string comparison, by design:
no fuzzy/AI similarity matching in v1 (see build_plan()).

Expected line shapes::

    FACT: Yash has won several competitions.
    RULE: Never invent live-feed information.
    STATE: HOH = Yash
    STATE: NOMINEES = Angela, Dee

A STATE line's topic (the text before "=") is normalized upper-case,
matching KnowledgeStore.teach()'s own normalization -- see production/
knowledge.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from production.knowledge import KnowledgeItem, KnowledgeStore, KnowledgeType

_LINE_PATTERN = re.compile(r"^(FACT|RULE|STATE)\s*:\s*(.*)$", re.IGNORECASE)


# ==========================================================
# Parsed line
# ==========================================================


@dataclass(slots=True)
class BatchLine:
    """One line from a pasted training batch, parsed independently of
    every other line -- a malformed line never affects any other."""

    line_number: int
    raw: str
    type: KnowledgeType | None = None
    content: str = ""
    # Only set for a valid STATE line.
    topic: str | None = None
    # Optional free-text source/reason -- see KnowledgeItem.note in
    # production/knowledge.py. Not set by batch parsing itself (batch
    # lines carry no per-line reason syntax); commands/teach.py's
    # /teach update sets this uniformly across every line it parses,
    # from that command's single `reason` parameter.
    note: str | None = None
    # None means this line parsed successfully; any other value is a
    # human-readable reason it was rejected.
    error: str | None = None

    @property
    def valid(self) -> bool:
        return self.error is None


def parse_batch(text: str) -> list[BatchLine]:
    """Parses every non-blank line of a pasted batch independently.

    Blank lines are silently skipped (not treated as malformed --
    pasting with paragraph spacing is normal). Every other line
    produces exactly one BatchLine, valid or not; nothing is ever
    dropped outright, so the caller can always show the moderator
    what happened to every line they pasted.
    """

    lines: list[BatchLine] = []

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        lines.append(_parse_line(line_number, stripped))

    return lines


def _parse_line(line_number: int, raw: str) -> BatchLine:
    match = _LINE_PATTERN.match(raw)

    if not match:
        return BatchLine(
            line_number=line_number,
            raw=raw,
            error='No recognized "FACT:", "RULE:", or "STATE:" prefix.',
        )

    prefix = match.group(1).upper()
    rest = match.group(2).strip()

    if not rest:
        return BatchLine(
            line_number=line_number,
            raw=raw,
            error=f"{prefix}: line has no content after the prefix.",
        )

    if prefix == "FACT":
        return BatchLine(
            line_number=line_number, raw=raw, type=KnowledgeType.FACT, content=rest
        )

    if prefix == "RULE":
        return BatchLine(
            line_number=line_number, raw=raw, type=KnowledgeType.RULE, content=rest
        )

    # STATE: TOPIC = value
    if "=" not in rest:
        return BatchLine(
            line_number=line_number,
            raw=raw,
            error='STATE line must be "STATE: TOPIC = value" (missing "=").',
        )

    topic_part, _, value_part = rest.partition("=")
    topic = topic_part.strip().upper()
    value = value_part.strip()

    if not topic:
        return BatchLine(
            line_number=line_number,
            raw=raw,
            error='STATE line is missing a topic before "=".',
        )

    if not value:
        return BatchLine(
            line_number=line_number,
            raw=raw,
            error='STATE line is missing a value after "=".',
        )

    return BatchLine(
        line_number=line_number,
        raw=raw,
        type=KnowledgeType.STATE,
        content=value,
        topic=topic,
    )


_STATE_UPDATE_LINE_PATTERN = re.compile(r"^([A-Za-z_ ]+?)\s*:\s*(.+)$")


def parse_state_updates(text: str, *, note: str | None = None) -> list[BatchLine]:
    """Parses /teach update's simpler "TOPIC: value" syntax (see
    commands/teach.py) -- no FACT:/RULE:/STATE: prefix, since /teach
    update is already scoped to current-state only. Every resulting
    line is a STATE BatchLine (or an invalid one with an error), so it
    can be fed straight into build_plan()/apply_plan() exactly like a
    STATE: line from /teach batch.

    note, when given, is attached to every parsed line uniformly (see
    BatchLine.note) -- /teach update takes one optional `reason` for
    the whole command, not a per-line reason.
    """

    lines: list[BatchLine] = []

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue

        match = _STATE_UPDATE_LINE_PATTERN.match(stripped)
        if not match:
            lines.append(
                BatchLine(
                    line_number=line_number,
                    raw=stripped,
                    error='Expected "TOPIC: value", e.g. "HOH: Yash".',
                )
            )
            continue

        topic = match.group(1).strip().upper()
        value = match.group(2).strip()

        if not value:
            lines.append(
                BatchLine(
                    line_number=line_number,
                    raw=stripped,
                    error=f"{topic}: missing a value after the colon.",
                )
            )
            continue

        lines.append(
            BatchLine(
                line_number=line_number,
                raw=stripped,
                type=KnowledgeType.STATE,
                content=value,
                topic=topic,
                note=note,
            )
        )

    return lines


# ==========================================================
# Plan
# ==========================================================


@dataclass(slots=True)
class StateConflict:
    """A STATE line whose topic already has a different active value
    -- either in the store already, or earlier in this same batch.
    Informational, not blocking: the moderator sees exactly what will
    change before confirming."""

    topic: str
    current_value: str | None
    new_value: str


@dataclass(slots=True)
class BatchPlan:
    """The deterministic result of checking a parsed batch against
    the current KnowledgeStore state -- what WOULD happen, before
    anything is written."""

    valid: list[BatchLine] = field(default_factory=list)
    invalid: list[BatchLine] = field(default_factory=list)
    conflicts: list[StateConflict] = field(default_factory=list)

    @property
    def fact_count(self) -> int:
        return sum(1 for line in self.valid if line.type == KnowledgeType.FACT)

    @property
    def rule_count(self) -> int:
        return sum(1 for line in self.valid if line.type == KnowledgeType.RULE)

    @property
    def state_count(self) -> int:
        return sum(1 for line in self.valid if line.type == KnowledgeType.STATE)


def build_plan(lines: list[BatchLine], knowledge: KnowledgeStore) -> BatchPlan:
    """Checks parsed lines against the current store, deterministically.

    STATE conflict detection compares each STATE line's new value
    against the topic's current active_state() -- or, for a topic
    that appears more than once in the same batch, against the
    immediately preceding line's value for that topic, so a batch
    that changes the same topic twice reports the conflict against
    what the moderator actually just typed, not stale pre-batch state.
    No fuzzy/AI similarity matching is used anywhere here, for FACT/
    RULE or STATE -- v1 is deterministic string comparison only.
    """

    valid = [line for line in lines if line.valid]
    invalid = [line for line in lines if not line.valid]

    conflicts: list[StateConflict] = []
    seen_in_batch: dict[str, str] = {}

    for line in valid:
        if line.type != KnowledgeType.STATE:
            continue

        if line.topic in seen_in_batch:
            current_value = seen_in_batch[line.topic]
        else:
            existing = knowledge.active_state(line.topic)
            current_value = existing.content if existing is not None else None

        if current_value is not None and current_value != line.content:
            conflicts.append(
                StateConflict(
                    topic=line.topic,
                    current_value=current_value,
                    new_value=line.content,
                )
            )

        seen_in_batch[line.topic] = line.content

    return BatchPlan(valid=valid, invalid=invalid, conflicts=conflicts)


def apply_plan(
    plan: BatchPlan, knowledge: KnowledgeStore, author_id: int
) -> list[KnowledgeItem]:
    """Writes every valid line in the plan, in order, returning the
    created items.

    STATE lines rely on KnowledgeStore.teach()'s own per-topic
    auto-supersede lookup (see production/knowledge.py) -- called once
    per line here, so a batch that changes the same topic twice
    resolves correctly in sequence, exactly as two separate manual
    updates would. Invalid lines are never passed in (see
    build_plan()) and therefore never written.
    """

    written: list[KnowledgeItem] = []

    for line in plan.valid:
        if line.type == KnowledgeType.STATE:
            item = knowledge.teach(
                KnowledgeType.STATE,
                line.content,
                author_id,
                topic=line.topic,
                note=line.note,
            )
        else:
            item = knowledge.teach(
                line.type, line.content, author_id, note=line.note
            )

        written.append(item)

    return written
