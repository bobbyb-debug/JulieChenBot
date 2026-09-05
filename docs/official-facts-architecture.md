# Official Facts vs. Live Feed vs. Conversational Memory

This document describes the architecture fixed in this change, and the
storage migration it required. See the PR description for the full
background (the reported production symptom: automated parsing showed
HOH = Taylor while the admin dashboard's manually maintained record
said Yash -- Yash was correct).

## The three systems

1. **Official facts** -- `KnowledgeStore` `STATE` items
   (`production/knowledge.py`), set only via `/teach update` or the
   admin dashboard's Update State. This is the sole source of truth
   for `/hoh`, `/nominees`, `/veto`, and the AI chat context's
   "OFFICIAL GAME FACTS" block. Every item carries `author_id`,
   `created_at`, and an automatic supersede-on-write chain per topic.

2. **Live feed observation** -- `HouseStatus`
   (`production/house_status.py`), updated automatically every
   production cycle by RSS/live-feed parsing
   (`production/engine.py` `tick()`). Never authoritative, never
   written to by any manual/admin path, never read by `/hoh`,
   `/nominees`, or `/veto`. Still useful for conflict detection
   (`admin_api/conflicts.py`) and diagnostics.

3. **Conversational memory** -- the rolling `chat_messages` table
   (`services/ai_service.py`, per-channel, identity-aware, bounded
   recent window) and explicit long-term memory
   (`production/memory.py` `MemoryStore`, via `/remember`). Neither is
   ever treated as an official fact.

The one rule that used to be violated: nothing but the RSS parser may
write to `HouseStatus`. A manual update writes only to `KnowledgeStore`.

## Storage migration (backward compatible, non-destructive)

- **`database/chat_history.db`**: the `chat_messages` table gains two
  nullable columns, `author_id` and `author_name`, via an idempotent
  `ALTER TABLE` run at connection time
  (`services/ai_service.py` `_ensure_author_columns()`). Existing rows
  are never modified or deleted -- they simply have `NULL` author
  fields, same as before this feature existed. No data loss.
- **`data/storage.json`**: gains one new top-level key,
  `"long_term_memory"` (default `[]`), used exclusively by
  `MemoryStore`. Every existing key (`"knowledge"`, `"game_state"`,
  `"pending_events"`, etc.) is untouched.
- Nothing is deleted, reset, or destructively migrated anywhere in
  this change.

## What changed for existing behavior

- `/teach update` and the dashboard's `POST /api/v1/state/apply` no
  longer touch `HouseStatus` -- they write official facts only.
- `/hoh`, `/nominees`, `/noms`, and `/veto` now read `KnowledgeStore`
  official facts instead of `HouseStatus`.
- `GET /api/v1/game-state` gained an additive `official_state` field
  (every active STATE item, keyed by topic) alongside the existing,
  unchanged `house_status`/`competition` fields.
- The AI chat system prompt no longer instructs the model to prefer
  automated live-feed data over administrator-taught facts -- that
  instruction was the literal root cause of the reported bug.

## Weekly state boundary (HISTORY IS NOT CURRENT STATE)

A second, later production incident: Julie presented a PREVIOUS
week's HOH/nominees/veto/Have-Nots as current "OFFICIAL GAME FACTS"
because a STATE topic, once taught, stayed the active/current value
forever -- there was no notion of "this belongs to week N," so an
admin who forgot to re-teach a topic for the new week left the old
one answering current-state questions indefinitely. A related finding:
free-text topic entry (`/teach update`'s "TOPIC: value" lines) could
produce two differently-spelled STATE topics for the same real-world
fact (`"VETO WINNER"` vs `"VETO_WINNER"`), both independently "active."

Fixed in `production/knowledge.py`:

- `KnowledgeStore` topics are canonicalized at write time
  (`_canonicalize_topic()`, used by `teach()`) -- a space/underscore
  spelling difference can no longer create two independently-active
  values for one topic. `KnowledgeStore.dedupe_topics()` (called once
  at every `ProductionEngine` startup, like
  `reconcile_game_state_from_knowledge()`) repairs data persisted
  before this existed.
- `KnowledgeStore.current_state(topic)` wraps `active_state(topic)`
  with a week boundary for `WEEK_SCOPED_TOPICS` (`HOH`, `NOMINEES`,
  `VETO_WINNER`, `VETO_USED`, `BB_BLOCKBUSTER`, `HAVE_NOTS`): a value
  taught before the current week started reads as unconfirmed, not as
  stale-but-current. `format_official_state()` and `/hoh`/`/nominees`/
  `/veto` all use this (or its safe superset, `current_state_items()`)
  instead of the raw `active_state()`/`active_items()` they used
  before. Every OTHER STATE topic (`REMAINING_HOUSEGUESTS`,
  `LAST_EVICTED`, `VOTE`, `EVICTED`, ...) is unaffected -- those are
  running/one-time facts, not per-week values.
- `KnowledgeStore.start_new_week()`/`close_week()`/`archived_week()`/
  `set_archived_week()` are the weekly boundary and archive
  themselves -- see `admin_api/routes.py`'s `/api/v1/week*` routes for
  the dashboard's CURRENT WEEK / CLOSE WEEK / START NEW WEEK controls,
  and `services/ai_service.py` `format_weekly_archive()` for how a
  question naming a specific past week ("who won the Week 8 veto?")
  is answered from the archive instead of falling back to semantic
  knowledge retrieval, which has no notion of "this belongs to a
  closed week." Starting or closing a week never mutates a single
  `KnowledgeItem` -- the boundary is a time-based read
  (`item.created_at` vs. the recorded week-start), not a rewrite.
