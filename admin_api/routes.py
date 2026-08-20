"""
Julie ChenBot Admin API Routes
================================

Every route here mirrors an existing, already-tested operation --
KnowledgeStore.teach()/forget()/reactivate() (production/knowledge.py),
and the same batch/state-update plan-then-apply flow (production/
batch_teach.py, production/state_sync.py) that already backs /teach
batch and /teach update (commands/teach.py) -- rather than inventing
new business logic. This module's job is strictly: parse the HTTP
request, call the existing function, serialize the result.

The one exception: POST /knowledge/{id}/reactivate has no Discord
slash-command equivalent today (there is no /teach reactivate) -- it
exists to support the dashboard's Knowledge Center reactivation
control, added because reversing a /forget was previously impossible
through any surface. It calls KnowledgeStore.reactivate(), which is
built the same way forget() already is (soft, idempotent, in-place --
see that method's docstring), so this is a small, symmetric addition,
not new business logic.

Auth is handled entirely by admin_api/auth.py's middleware, applied to
the whole application in admin_api/server.py -- no route here checks
authorization itself. The one exception is GET /health just below,
which the middleware explicitly exempts (see admin_api/auth.py) --
everything else on this router requires a valid bearer token.
"""

from __future__ import annotations

from aiohttp import web

from admin_api.conflicts import detect_conflicts, house_status_value
from admin_api.routing import build_routing_table, channel_configuration
from production.batch_teach import (
    BatchPlan,
    apply_plan,
    build_plan,
    parse_batch,
    parse_state_updates,
)
from production.knowledge import KnowledgeType
from production.state_sync import is_recognized_topic

routes = web.RouteTableDef()


def _engine(request: web.Request):
    return request.app["engine"]


async def _json_body(request: web.Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _plan_to_dict(plan: BatchPlan) -> dict:
    return {
        "valid": [
            {
                "line_number": line.line_number,
                "raw": line.raw,
                "type": line.type.value if line.type else None,
                "content": line.content,
                "topic": line.topic,
                "note": line.note,
            }
            for line in plan.valid
        ],
        "invalid": [
            {"line_number": line.line_number, "raw": line.raw, "error": line.error}
            for line in plan.invalid
        ],
        "conflicts": [
            {
                "topic": conflict.topic,
                "current_value": conflict.current_value,
                "new_value": conflict.new_value,
            }
            for conflict in plan.conflicts
        ],
    }


def _filter_plan(plan: BatchPlan, line_numbers: set[int] | None) -> BatchPlan:
    """Restricts a plan's valid lines to a moderator's selection before
    apply_plan() writes anything -- the dashboard's "select/deselect
    changes" step (see the Batch Teaching UI). None means "everything
    valid," matching /teach batch/update's own Confirm behavior."""

    if line_numbers is None:
        return plan
    return BatchPlan(
        valid=[line for line in plan.valid if line.line_number in line_numbers],
        invalid=plan.invalid,
        conflicts=plan.conflicts,
    )


def _validate_line_numbers(body: dict) -> tuple[set[int] | None, str | None]:
    """Returns (line_numbers or None, error message or None)."""

    line_numbers = body.get("line_numbers")
    if line_numbers is None:
        return None, None
    if not isinstance(line_numbers, list) or not all(
        isinstance(n, int) for n in line_numbers
    ):
        return None, "'line_numbers' must be a list of integers"
    return set(line_numbers), None


# ==========================================================
# Public liveness endpoint (Railway health check)
# ==========================================================


@routes.get("/health")
async def liveness(request: web.Request) -> web.Response:
    """Unauthenticated liveness check: "is the admin API process up
    and serving requests." Deliberately returns nothing beyond a
    static status -- no engine state, no game data, no knowledge, no
    diagnostics, no secrets -- so it's safe to leave unauthenticated
    (see the explicit, exact-path exemption in admin_api/auth.py).

    The detailed, authenticated engine health lives at the existing
    GET /api/v1/health below; this route is not a replacement for it.
    """

    return web.json_response({"status": "ok"})


# ==========================================================
# Health / Game State / Conflicts
# ==========================================================


@routes.get("/api/v1/health")
async def health(request: web.Request) -> web.Response:
    engine = _engine(request)
    return web.json_response({"engine": engine.health(), "info": engine.info()})


@routes.get("/api/v1/game-state")
async def game_state(request: web.Request) -> web.Response:
    """house_status/competition are the automated, live-feed-driven
    observation layer (production/house_status.py, production/
    competition.py) -- never authoritative. official_state is every
    active STATE knowledge item (production/knowledge.py
    KnowledgeStore), keyed by topic -- the actual source of truth
    /hoh, /noms, /nominees, and /veto read. The dashboard should
    present official_state as current game state and house_status as
    a secondary, clearly-labeled live-feed signal -- see docs/
    ARCHITECTURE.md in the admin dashboard repo."""

    engine = _engine(request)
    official_state = {
        item.topic: item.to_dict()
        for item in engine.knowledge.active_items()
        if item.type == KnowledgeType.STATE and item.topic
    }
    return web.json_response(
        {
            "house_status": engine.watcher.house_status.snapshot(),
            "competition": engine.watcher.competition.snapshot(),
            "official_state": official_state,
        }
    )


@routes.get("/api/v1/conflicts")
async def conflicts(request: web.Request) -> web.Response:
    engine = _engine(request)
    return web.json_response({"conflicts": detect_conflicts(engine)})


# ==========================================================
# Knowledge
# ==========================================================


@routes.get("/api/v1/knowledge")
async def list_knowledge(request: web.Request) -> web.Response:
    engine = _engine(request)
    items = engine.knowledge.all_items()

    type_filter = request.query.get("type")
    if type_filter:
        try:
            wanted_type = KnowledgeType(type_filter)
        except ValueError:
            return web.json_response(
                {"error": f"unknown type: {type_filter}"}, status=400
            )
        items = [item for item in items if item.type == wanted_type]

    active_filter = request.query.get("active")
    if active_filter is not None:
        wanted_active = active_filter.strip().lower() in {"1", "true", "yes"}
        items = [item for item in items if item.active == wanted_active]

    topic_filter = request.query.get("topic")
    if topic_filter:
        normalized_topic = topic_filter.strip().upper()
        items = [item for item in items if item.topic == normalized_topic]

    search = request.query.get("q")
    if search:
        needle = search.strip().lower()
        items = [item for item in items if needle in item.content.lower()]

    items = sorted(items, key=lambda item: item.id)
    return web.json_response([item.to_dict() for item in items])


@routes.get("/api/v1/knowledge/{item_id}")
async def get_knowledge(request: web.Request) -> web.Response:
    engine = _engine(request)
    try:
        item_id = int(request.match_info["item_id"])
    except ValueError:
        return web.json_response({"error": "invalid knowledge id"}, status=400)

    item = engine.knowledge.get(item_id)
    if item is None:
        return web.json_response({"error": "not found"}, status=404)

    return web.json_response(item.to_dict())


@routes.post("/api/v1/knowledge")
async def create_knowledge(request: web.Request) -> web.Response:
    engine = _engine(request)
    body = await _json_body(request)
    if body is None:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    raw_type = body.get("type")
    content = (body.get("content") or "").strip()
    author_id = body.get("author_id")
    supersedes = body.get("supersedes")
    topic = body.get("topic")
    note = body.get("note")

    if raw_type not in {t.value for t in KnowledgeType}:
        return web.json_response({"error": "invalid or missing 'type'"}, status=400)
    if not content:
        return web.json_response({"error": "'content' is required"}, status=400)
    if not isinstance(author_id, int):
        return web.json_response(
            {"error": "'author_id' must be an integer"}, status=400
        )
    if supersedes is not None and not isinstance(supersedes, int):
        return web.json_response(
            {"error": "'supersedes' must be an integer"}, status=400
        )

    try:
        item = engine.knowledge.teach(
            KnowledgeType(raw_type),
            content,
            author_id,
            supersedes=supersedes,
            topic=topic,
            note=note,
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    return web.json_response(item.to_dict(), status=201)


@routes.post("/api/v1/knowledge/{item_id}/forget")
async def forget_knowledge(request: web.Request) -> web.Response:
    engine = _engine(request)
    try:
        item_id = int(request.match_info["item_id"])
    except ValueError:
        return web.json_response({"error": "invalid knowledge id"}, status=400)

    forgotten = engine.knowledge.forget(item_id)
    return web.json_response({"id": item_id, "forgotten": forgotten})


@routes.post("/api/v1/knowledge/{item_id}/reactivate")
async def reactivate_knowledge(request: web.Request) -> web.Response:
    """Reverses a previous /forget for the SAME item -- see
    KnowledgeStore.reactivate() (production/knowledge.py). Never
    creates a new item; id/type/content/author_id/created_at/topic
    are all preserved exactly as they were."""

    engine = _engine(request)
    try:
        item_id = int(request.match_info["item_id"])
    except ValueError:
        return web.json_response({"error": "invalid knowledge id"}, status=400)

    reactivated = engine.knowledge.reactivate(item_id)
    return web.json_response({"id": item_id, "reactivated": reactivated})


@routes.get("/api/v1/state/{topic}/why")
async def state_why(request: web.Request) -> web.Response:
    """Provenance for one game-state topic: the "Why does Julie think
    X?" flagship dashboard feature. Every field here is read directly
    from KnowledgeStore/HouseStatus -- nothing is synthesized."""

    engine = _engine(request)
    topic = request.match_info["topic"].strip().upper()

    current = engine.knowledge.active_state(topic)
    history = sorted(
        (item for item in engine.knowledge.all_items() if item.topic == topic),
        key=lambda item: item.created_at,
    )

    # Best-effort, deliberately simple keyword match -- not a claim of
    # semantic understanding. Surfaces plausibly-related FACTs for a
    # moderator to judge, never invents a connection.
    keyword = topic.replace("_", " ").lower()
    related_facts = [
        item
        for item in engine.knowledge.active_items()
        if item.type == KnowledgeType.FACT and keyword in item.content.lower()
    ]

    house_status = engine.watcher.house_status.current

    return web.json_response(
        {
            "topic": topic,
            "current_state": current.to_dict() if current else None,
            "house_status_value": house_status_value(house_status, topic),
            "history": [item.to_dict() for item in history],
            "related_facts": [item.to_dict() for item in related_facts],
        }
    )


# ==========================================================
# Batch teaching / manual state updates
# ==========================================================


@routes.post("/api/v1/batch/plan")
async def batch_plan(request: web.Request) -> web.Response:
    engine = _engine(request)
    body = await _json_body(request)
    if body is None:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    lines = parse_batch(body.get("text", ""))
    plan = build_plan(lines, engine.knowledge)
    return web.json_response(_plan_to_dict(plan))


@routes.post("/api/v1/batch/apply")
async def batch_apply(request: web.Request) -> web.Response:
    engine = _engine(request)
    body = await _json_body(request)
    if body is None:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    author_id = body.get("author_id")
    if not isinstance(author_id, int):
        return web.json_response(
            {"error": "'author_id' must be an integer"}, status=400
        )

    line_numbers, error = _validate_line_numbers(body)
    if error:
        return web.json_response({"error": error}, status=400)

    lines = parse_batch(body.get("text", ""))
    plan = _filter_plan(build_plan(lines, engine.knowledge), line_numbers)

    written = apply_plan(plan, engine.knowledge, author_id)
    return web.json_response({"written": [item.to_dict() for item in written]})


@routes.post("/api/v1/state/plan")
async def state_plan(request: web.Request) -> web.Response:
    engine = _engine(request)
    body = await _json_body(request)
    if body is None:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    lines = parse_state_updates(body.get("text", ""), note=body.get("reason"))
    plan = build_plan(lines, engine.knowledge)
    return web.json_response(_plan_to_dict(plan))


@routes.post("/api/v1/state/apply")
async def state_apply(request: web.Request) -> web.Response:
    """Mirrors /teach update's confirm handler exactly: writes the
    selected lines as official-facts STATE knowledge -- the sole
    source of truth /hoh, /noms, /nominees, and /veto read (see
    commands/teach.py _StateUpdateConfirmView.handle_confirm).

    Deliberately does NOT touch HouseStatus (production/
    house_status.py): that is the automated, live-feed-driven
    observation layer, written only by production/engine.py's RSS
    pipeline. This is what stops an automated parse from silently
    overwriting a dashboard-confirmed fact, and vice versa.
    """

    engine = _engine(request)
    body = await _json_body(request)
    if body is None:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    author_id = body.get("author_id")
    if not isinstance(author_id, int):
        return web.json_response(
            {"error": "'author_id' must be an integer"}, status=400
        )

    line_numbers, error = _validate_line_numbers(body)
    if error:
        return web.json_response({"error": error}, status=400)

    lines = parse_state_updates(body.get("text", ""), note=body.get("reason"))
    plan = _filter_plan(build_plan(lines, engine.knowledge), line_numbers)

    written = apply_plan(plan, engine.knowledge, author_id)

    applied_topics = [
        item.topic
        for item in written
        if item.topic and is_recognized_topic(item.topic)
    ]

    return web.json_response(
        {
            "written": [item.to_dict() for item in written],
            "applied_topics": applied_topics,
        }
    )


# ==========================================================
# Sources / Events / Diagnostics / Discord
# ==========================================================


@routes.get("/api/v1/sources")
async def sources(request: web.Request) -> web.Response:
    engine = _engine(request)
    watcher = engine.watcher
    storage = engine.storage

    hamsterwatch = getattr(watcher, "hamsterwatch", None)

    return web.json_response(
        {
            "rss": {
                "last_guid": storage.get("last_guid", ""),
                "last_title": storage.get("last_title", ""),
                "last_published": storage.get("last_published", ""),
                "feed_state": storage.get("feed_state", "UNKNOWN"),
            },
            "house_image": {
                "url": watcher.house_image.image_url,
                "last_hash": storage.get("house_image_last_hash", ""),
            },
            "competition": watcher.competition.snapshot(),
            "hamsterwatch": (
                {
                    "archive_size": hamsterwatch.archive.count(),
                    "bootstrapping": hamsterwatch._bootstrapping,
                }
                if hamsterwatch is not None
                else None
            ),
            "monitors": watcher.snapshot(),
        }
    )


@routes.get("/api/v1/events")
async def events(request: web.Request) -> web.Response:
    engine = _engine(request)
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 200))

    return web.json_response(engine.recent_events(limit))


@routes.get("/api/v1/events/pending")
async def pending_events(request: web.Request) -> web.Response:
    engine = _engine(request)
    return web.json_response([event.to_dict() for event in engine.pending_events])


@routes.get("/api/v1/diagnostics")
async def diagnostics(request: web.Request) -> web.Response:
    engine = _engine(request)
    recent = engine.recent_events(100)
    warnings = [
        entry
        for entry in recent
        if entry.get("severity") in {"warning", "important", "critical"}
    ]

    return web.json_response(
        {
            "health": engine.health(),
            "watcher": engine.watcher.snapshot(),
            "pending_events": [event.to_dict() for event in engine.pending_events],
            "recent_warnings": warnings[:20],
        }
    )


@routes.get("/api/v1/discord/routing")
async def discord_routing(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "channels": channel_configuration(),
            "routing": build_routing_table(),
        }
    )
