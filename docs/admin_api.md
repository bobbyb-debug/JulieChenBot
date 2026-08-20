# Admin API

A narrow, authenticated HTTP surface for the separate
[Julie ChenBot Admin Dashboard](https://github.com/bobbyb-debug/julie-chenbot-admin-dashboard).
Nothing else talks to it — not end users, not Discord.

Off by default. Enabling it does not change any bot behavior other
than opening this one HTTP surface.

## Enabling it

Set these on the bot's host (Railway, or your local `.env`):

| Variable | Required | Purpose |
|---|---|---|
| `ENABLE_ADMIN_API` | to turn it on | `true`/`false`, default `false` |
| `ADMIN_API_KEY` | yes, once enabled | long random shared secret; the server refuses to start without it |
| `ADMIN_API_PORT` | no | default `8080` |

The dashboard's *backend* sends `Authorization: Bearer <ADMIN_API_KEY>`
on every request. The key must never reach a browser — the dashboard's
frontend should never call this API directly.

If you deploy this on Railway, you'll additionally need to expose the
chosen port (Railway's `PORT`/networking settings) — that's a
deployment decision for whoever operates the bot's host, not something
this code does automatically.

## Architecture

Runs as a background `asyncio` task inside the same process as the
Discord bot (started from `services/discord.py` `on_ready()`, next to
the existing production scheduler). It reads and writes the *same*
`ProductionEngine` instance the bot itself uses — there is no second
copy of game state, knowledge, or anything else. If this server is
unreachable or crashes, the bot's own Discord/production loop is
unaffected: it owns no state and isn't on the `tick()`/`announce()`
path.

Every write route mirrors an operation a trusted Discord moderator
could already perform via `/teach`:

- `production/knowledge.py` `KnowledgeStore.teach()` / `.forget()`
- `production/batch_teach.py` `parse_batch()` / `parse_state_updates()`
  / `build_plan()` / `apply_plan()` — the exact plan-then-apply flow
  behind `/teach batch` and `/teach update`

No route invents new business logic; `admin_api/routes.py` only
parses the HTTP request, calls the existing function, and serializes
the result.

One exception: `POST /knowledge/{id}/reactivate` has no `/teach`
equivalent on Discord today — it reverses a previous `/forget`,
in place, for the dashboard's Knowledge Center. See
`production/knowledge.py` `KnowledgeStore.reactivate()`, built the
same soft/idempotent/in-place way `.forget()` already is.

Since the official-facts architecture change (see
`docs/official-facts-architecture.md`), `/teach update` and
`POST /state/apply` write only to `KnowledgeStore` — neither touches
the automated, RSS-driven `HouseStatus` object `/hoh`, `/nominees`,
and `/veto` no longer read.

## Endpoints

All under `/api/v1`, all requiring the bearer token.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | engine health + build info |
| GET | `/game-state` | current HOH/nominees/veto/have-nots + competition |
| GET | `/conflicts` | taught STATE vs. live HouseStatus disagreements |
| GET | `/knowledge` | list, filters: `type`, `active`, `topic`, `q` |
| GET | `/knowledge/{id}` | one item |
| POST | `/knowledge` | teach FACT/RULE/CORRECTION/STATE |
| POST | `/knowledge/{id}/forget` | deactivate (soft delete) |
| POST | `/knowledge/{id}/reactivate` | reverse a previous forget, in place (no `/teach` equivalent) |
| GET | `/state/{topic}/why` | provenance: current value, taught history, related facts |
| POST | `/batch/plan` | preview a `FACT:`/`RULE:`/`STATE:` batch — zero writes |
| POST | `/batch/apply` | write selected lines from a previewed batch |
| POST | `/state/plan` | preview a `TOPIC: value` state update — zero writes |
| POST | `/state/apply` | write selected STATE lines as official facts (KnowledgeStore only — never touches live HouseStatus) |
| GET | `/sources` | RSS/House Image/Competition/Hamsterwatch status |
| GET | `/events` | recent *delivered* events (`?limit=`) |
| GET | `/events/pending` | events queued but not yet delivered |
| GET | `/diagnostics` | health + watcher snapshot + pending events + recent warnings |
| GET | `/discord/routing` | live routing table (derived from the real router, not duplicated) |

`POST /batch/apply` and `POST /state/apply` both accept an optional
`line_numbers: [int, ...]` to apply only a moderator-selected subset
of a previewed plan — nothing is written until this call is made.

## What this deliberately does not do

- No arbitrary database/SQL access, no shell/Python execution.
- No new AI-driven parsing of free-text into knowledge — batch
  teaching uses the same strict `FACT:`/`RULE:`/`STATE:` line syntax
  `/teach batch` already requires, so there is exactly one parser for
  "text becomes structured knowledge," not two that could disagree.
- `KnowledgeStore.forget()` does not record *who* forgot an item, only
  `updated_at` — that's a pre-existing limitation of the underlying
  store, not something this API layer works around. The dashboard's
  own audit log (in the dashboard repo) is the place that should
  record which dashboard user triggered a given API call.
- Does not touch Discord routing logic, RSS parsing, House Status
  detection, Competition Monitor, or recap generation.

## Tests

`tests/test_admin_api_auth.py`, `tests/test_admin_api_routes.py`,
`tests/test_engine_event_log.py`.
