"""Tests for production/batch_teach.py -- parsing, planning, and
applying a moderator's pasted multi-line training batch.

Pure logic only: no Discord, no AI. commands/teach.py's /teach batch
command (and its Confirm/Cancel UI) is covered separately in
tests/test_teach_command.py.
"""

from __future__ import annotations

from pathlib import Path

from database.storage import Storage
from production.batch_teach import apply_plan, build_plan, parse_batch, parse_state_updates
from production.knowledge import KnowledgeStore, KnowledgeType


def _store(tmp_path: Path, monkeypatch) -> KnowledgeStore:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    return KnowledgeStore(storage=Storage())


# ==========================================================
# Parsing
# ==========================================================


def test_parses_fact_rule_state_lines() -> None:
    text = (
        "FACT: Yash has won several competitions.\n"
        "RULE: Never invent live-feed information.\n"
        "STATE: HOH = Yash"
    )

    lines = parse_batch(text)

    assert len(lines) == 3
    assert lines[0].type == KnowledgeType.FACT
    assert lines[0].content == "Yash has won several competitions."
    assert lines[1].type == KnowledgeType.RULE
    assert lines[1].content == "Never invent live-feed information."
    assert lines[2].type == KnowledgeType.STATE
    assert lines[2].topic == "HOH"
    assert lines[2].content == "Yash"
    assert all(line.valid for line in lines)


def test_prefix_matching_is_case_insensitive() -> None:
    lines = parse_batch("fact: lowercase works\nFaCt: mixed case works too")

    assert all(line.type == KnowledgeType.FACT for line in lines)
    assert all(line.valid for line in lines)


def test_state_topic_is_normalized_upper_case() -> None:
    lines = parse_batch("STATE: hoh = Yash")

    assert lines[0].topic == "HOH"


def test_state_value_supports_commas() -> None:
    lines = parse_batch("STATE: NOMINEES = Angela, Dee")

    assert lines[0].topic == "NOMINEES"
    assert lines[0].content == "Angela, Dee"


def test_blank_lines_are_silently_skipped() -> None:
    lines = parse_batch("FACT: one\n\n\nFACT: two\n   \n")

    assert len(lines) == 2
    assert [line.content for line in lines] == ["one", "two"]


def test_line_with_no_recognized_prefix_is_invalid() -> None:
    lines = parse_batch("Just some text with no prefix at all")

    assert len(lines) == 1
    assert not lines[0].valid
    assert lines[0].error is not None


def test_fact_or_rule_with_no_content_after_prefix_is_invalid() -> None:
    lines = parse_batch("FACT:\nRULE:   ")

    assert len(lines) == 2
    assert all(not line.valid for line in lines)


def test_state_line_missing_equals_is_invalid() -> None:
    lines = parse_batch("STATE: HOH Yash")

    assert not lines[0].valid
    assert "=" in lines[0].error


def test_state_line_missing_topic_is_invalid() -> None:
    lines = parse_batch("STATE: = Yash")

    assert not lines[0].valid


def test_state_line_missing_value_is_invalid() -> None:
    lines = parse_batch("STATE: HOH =")

    assert not lines[0].valid


def test_one_malformed_line_does_not_affect_other_lines() -> None:
    text = (
        "FACT: good fact one\n"
        "this line has no prefix\n"
        "RULE: good rule\n"
        "STATE: HOH Yash\n"  # missing "="
        "STATE: NOMINEES = Angela, Dee\n"
    )

    lines = parse_batch(text)

    assert len(lines) == 5
    valid = [line for line in lines if line.valid]
    invalid = [line for line in lines if not line.valid]
    assert len(valid) == 3
    assert len(invalid) == 2
    assert [line.content for line in valid] == [
        "good fact one",
        "good rule",
        "Angela, Dee",
    ]
    # Line numbers are preserved for the moderator to locate the issue.
    assert [line.line_number for line in invalid] == [2, 4]


# ==========================================================
# Planning: counts and conflict detection
# ==========================================================


def test_plan_counts_reflect_only_valid_lines(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    lines = parse_batch(
        "FACT: a\nFACT: b\nRULE: c\nSTATE: HOH = Yash\nnot a valid line"
    )

    plan = build_plan(lines, store)

    assert plan.fact_count == 2
    assert plan.rule_count == 1
    assert plan.state_count == 1
    assert len(plan.invalid) == 1


def test_state_conflict_detected_against_existing_active_state(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")

    lines = parse_batch("STATE: HOH = Barrett")
    plan = build_plan(lines, store)

    assert len(plan.conflicts) == 1
    conflict = plan.conflicts[0]
    assert conflict.topic == "HOH"
    assert conflict.current_value == "Yash"
    assert conflict.new_value == "Barrett"


def test_no_conflict_when_new_state_value_matches_current(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")

    lines = parse_batch("STATE: HOH = Yash")
    plan = build_plan(lines, store)

    assert plan.conflicts == []


def test_no_conflict_when_topic_has_no_existing_state(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)

    lines = parse_batch("STATE: HOH = Yash")
    plan = build_plan(lines, store)

    assert plan.conflicts == []


def test_conflict_detected_against_earlier_line_in_same_batch(
    tmp_path: Path, monkeypatch
) -> None:
    """Two STATE lines for the same topic in one paste -- the second
    line's conflict must compare against the first line's value, not
    stale pre-batch state."""

    store = _store(tmp_path, monkeypatch)

    lines = parse_batch("STATE: HOH = Yash\nSTATE: HOH = Barrett")
    plan = build_plan(lines, store)

    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].current_value == "Yash"
    assert plan.conflicts[0].new_value == "Barrett"


def test_fact_and_rule_lines_never_produce_conflicts(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    store.teach(KnowledgeType.FACT, "Yash has won comps.", author_id=1)

    lines = parse_batch("FACT: Yash has won comps.\nRULE: some rule")
    plan = build_plan(lines, store)

    assert plan.conflicts == []


# ==========================================================
# Applying: zero writes on cancel is enforced by the caller
# (commands/teach.py) never calling apply_plan -- these tests cover
# what apply_plan itself writes when it IS called.
# ==========================================================


def test_apply_plan_writes_only_valid_lines(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    lines = parse_batch("FACT: good\nnot valid\nRULE: also good")
    plan = build_plan(lines, store)

    written = apply_plan(plan, store, author_id=42)

    assert len(written) == 2
    assert [item.content for item in written] == ["good", "also good"]
    assert len(store.all_items()) == 2


def test_apply_plan_state_lines_supersede_correctly(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    first = store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")

    lines = parse_batch("STATE: HOH = Barrett")
    plan = build_plan(lines, store)
    written = apply_plan(plan, store, author_id=42)

    assert written[0].topic == "HOH"
    assert written[0].content == "Barrett"
    assert written[0].supersedes == first.id
    assert store.get(first.id).active is False
    assert store.active_state("HOH") == written[0]


def test_apply_plan_same_topic_twice_in_one_batch_resolves_in_sequence(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    lines = parse_batch("STATE: HOH = Yash\nSTATE: HOH = Barrett")
    plan = build_plan(lines, store)

    written = apply_plan(plan, store, author_id=42)

    assert len(written) == 2
    first_written, second_written = written
    assert second_written.supersedes == first_written.id
    assert store.get(first_written.id).active is False
    assert store.active_state("HOH") == second_written


def test_apply_plan_facts_accumulate_normally(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    lines = parse_batch("FACT: one\nFACT: two")
    plan = build_plan(lines, store)

    written = apply_plan(plan, store, author_id=42)

    assert len(written) == 2
    assert all(item in store.active_items() for item in written)


def test_apply_plan_records_author_id_as_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    lines = parse_batch("FACT: something")
    plan = build_plan(lines, store)

    written = apply_plan(plan, store, author_id=987654321)

    assert written[0].author_id == 987654321


def test_mixed_batch_end_to_end(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    text = (
        "FACT: Yash has won several competitions.\n"
        "FACT: Angela is known for strategic gameplay.\n"
        "RULE: Never invent live-feed information.\n"
        "RULE: Joker's Updates is the primary live-feed source.\n"
        "STATE: HOH = Yash\n"
        "STATE: NOMINEES = Angela, Dee\n"
        "STATE: VETO_WINNER = Barrett\n"
    )

    lines = parse_batch(text)
    plan = build_plan(lines, store)

    assert plan.fact_count == 2
    assert plan.rule_count == 2
    assert plan.state_count == 3
    assert plan.invalid == []

    written = apply_plan(plan, store, author_id=1)

    assert len(written) == 7
    assert store.active_state("HOH").content == "Yash"
    assert store.active_state("NOMINEES").content == "Angela, Dee"
    assert store.active_state("VETO_WINNER").content == "Barrett"


# ==========================================================
# parse_state_updates (used by /teach update -- simpler "TOPIC: value"
# syntax, no FACT:/RULE:/STATE: prefix needed)
# ==========================================================


def test_parse_state_updates_basic_lines() -> None:
    lines = parse_state_updates("HOH: Yash\nNominees: Angela, Dee")

    assert len(lines) == 2
    assert all(line.valid for line in lines)
    assert all(line.type == KnowledgeType.STATE for line in lines)
    assert lines[0].topic == "HOH"
    assert lines[0].content == "Yash"
    assert lines[1].topic == "NOMINEES"
    assert lines[1].content == "Angela, Dee"


def test_parse_state_updates_normalizes_topic_case() -> None:
    lines = parse_state_updates("hoh: Yash")

    assert lines[0].topic == "HOH"


def test_parse_state_updates_rejects_line_with_no_colon() -> None:
    lines = parse_state_updates("just some text")

    assert not lines[0].valid


def test_parse_state_updates_rejects_missing_value() -> None:
    lines = parse_state_updates("HOH:")

    assert not lines[0].valid


def test_parse_state_updates_attaches_uniform_note() -> None:
    lines = parse_state_updates(
        "HOH: Yash\nNominees: Angela, Dee", note="confirmed via live feed replay"
    )

    assert all(line.note == "confirmed via live feed replay" for line in lines)


def test_parse_state_updates_note_defaults_to_none() -> None:
    lines = parse_state_updates("HOH: Yash")

    assert lines[0].note is None


def test_parse_state_updates_skips_blank_lines() -> None:
    lines = parse_state_updates("HOH: Yash\n\n\nNominees: Angela, Dee\n  \n")

    assert len(lines) == 2


def test_parse_state_updates_feeds_directly_into_build_and_apply_plan(
    tmp_path: Path, monkeypatch
) -> None:
    """Proof of reuse: parse_state_updates() output works with the
    exact same build_plan()/apply_plan() used by /teach batch."""

    store = _store(tmp_path, monkeypatch)
    store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")

    lines = parse_state_updates("HOH: Barrett")
    plan = build_plan(lines, store)

    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].current_value == "Yash"

    written = apply_plan(plan, store, author_id=42)

    assert written[0].content == "Barrett"
    assert store.active_state("HOH").content == "Barrett"
