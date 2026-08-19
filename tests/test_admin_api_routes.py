"""Tests for admin_api/routes.py -- the HTTP surface the dashboard
calls. Each route mirrors an already-tested underlying operation (see
production/knowledge.py, production/batch_teach.py, production/
state_sync.py); these tests focus on request parsing/validation and
that the right underlying call happens, not re-testing that
underlying logic itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

import admin_api.auth as auth_module
from admin_api.server import build_app
from database.storage import Storage
from production.engine import ProductionEngine
from production.house_status import HouseStatus
from production.knowledge import KnowledgeType

API_KEY = "test-secret"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


def _build(monkeypatch, tmp_path: Path) -> tuple[ProductionEngine, object]:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    monkeypatch.setattr(auth_module, "ADMIN_API_KEY", API_KEY)

    engine = ProductionEngine(storage=Storage())
    app = build_app(engine)
    return engine, app


def _run(coro) -> None:
    asyncio.run(coro)


# ==========================================================
# Health / game state
# ==========================================================


def test_health_reports_engine_status(tmp_path: Path, monkeypatch) -> None:
    _, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/health", headers=AUTH)
            assert resp.status == 200
            body = await resp.json()
            assert body["engine"]["status"] in {"healthy", "degraded"}
            assert "name" in body["info"]

    _run(scenario())


def test_game_state_reflects_watcher_snapshots(tmp_path: Path, monkeypatch) -> None:
    engine, app = _build(monkeypatch, tmp_path)
    engine.watcher.house_status.current = HouseStatus(hoh="Yash")

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/game-state", headers=AUTH)
            body = await resp.json()
            assert body["house_status"]["hoh"] == "Yash"

    _run(scenario())


# ==========================================================
# Knowledge CRUD
# ==========================================================


def test_create_list_and_get_knowledge(tmp_path: Path, monkeypatch) -> None:
    _, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            create = await client.post(
                "/api/v1/knowledge",
                json={"type": "fact", "content": "Yash is strong.", "author_id": 1},
                headers=AUTH,
            )
            assert create.status == 201
            created = await create.json()
            assert created["type"] == "fact"
            assert created["content"] == "Yash is strong."

            listing = await client.get("/api/v1/knowledge", headers=AUTH)
            items = await listing.json()
            assert len(items) == 1
            assert items[0]["id"] == created["id"]

            single = await client.get(
                f"/api/v1/knowledge/{created['id']}", headers=AUTH
            )
            assert single.status == 200

            missing = await client.get("/api/v1/knowledge/9999", headers=AUTH)
            assert missing.status == 404

    _run(scenario())


def test_create_knowledge_rejects_invalid_type(tmp_path: Path, monkeypatch) -> None:
    _, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/knowledge",
                json={"type": "opinion", "content": "x", "author_id": 1},
                headers=AUTH,
            )
            assert resp.status == 400

    _run(scenario())


def test_create_knowledge_rejects_missing_author_id(
    tmp_path: Path, monkeypatch
) -> None:
    _, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/knowledge",
                json={"type": "fact", "content": "x"},
                headers=AUTH,
            )
            assert resp.status == 400

    _run(scenario())


def test_state_type_requires_topic(tmp_path: Path, monkeypatch) -> None:
    _, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/knowledge",
                json={"type": "state", "content": "Yash", "author_id": 1},
                headers=AUTH,
            )
            # KnowledgeStore.teach() raises ValueError -> routed to 400
            assert resp.status == 400

    _run(scenario())


def test_forget_knowledge_deactivates_it(tmp_path: Path, monkeypatch) -> None:
    engine, app = _build(monkeypatch, tmp_path)
    item = engine.knowledge.teach(KnowledgeType.FACT, "Some fact.", 1)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                f"/api/v1/knowledge/{item.id}/forget", headers=AUTH
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["forgotten"] is True

            second = await client.post(
                f"/api/v1/knowledge/{item.id}/forget", headers=AUTH
            )
            body2 = await second.json()
            assert body2["forgotten"] is False  # already forgotten -- idempotent

    _run(scenario())
    assert engine.knowledge.get(item.id).active is False


def test_knowledge_list_filters_by_type_and_active(
    tmp_path: Path, monkeypatch
) -> None:
    engine, app = _build(monkeypatch, tmp_path)
    fact = engine.knowledge.teach(KnowledgeType.FACT, "A fact.", 1)
    engine.knowledge.teach(KnowledgeType.RULE, "A rule.", 1)
    engine.knowledge.forget(fact.id)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            only_rules = await client.get(
                "/api/v1/knowledge?type=rule", headers=AUTH
            )
            rules = await only_rules.json()
            assert len(rules) == 1
            assert rules[0]["type"] == "rule"

            only_active = await client.get(
                "/api/v1/knowledge?active=true", headers=AUTH
            )
            active = await only_active.json()
            assert all(item["active"] for item in active)
            assert len(active) == 1

    _run(scenario())


# ==========================================================
# State: why / plan / apply
# ==========================================================


def test_state_why_returns_provenance(tmp_path: Path, monkeypatch) -> None:
    engine, app = _build(monkeypatch, tmp_path)
    engine.knowledge.teach(KnowledgeType.STATE, "Yash", 1, topic="HOH")

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/state/HOH/why", headers=AUTH)
            body = await resp.json()
            assert body["current_state"]["content"] == "Yash"
            assert len(body["history"]) == 1

    _run(scenario())


def test_state_apply_writes_knowledge_and_updates_live_house_status(
    tmp_path: Path, monkeypatch
) -> None:
    engine, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            plan_resp = await client.post(
                "/api/v1/state/plan", json={"text": "HOH: Yash"}, headers=AUTH
            )
            plan = await plan_resp.json()
            assert len(plan["valid"]) == 1

            apply_resp = await client.post(
                "/api/v1/state/apply",
                json={"text": "HOH: Yash", "author_id": 1},
                headers=AUTH,
            )
            assert apply_resp.status == 200
            body = await apply_resp.json()
            assert body["applied_topics"] == ["HOH"]

    _run(scenario())

    assert engine.watcher.house_status.current.hoh == "Yash"
    assert engine.knowledge.active_state("HOH").content == "Yash"


def test_state_apply_respects_line_number_selection(
    tmp_path: Path, monkeypatch
) -> None:
    engine, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/v1/state/apply",
                json={
                    "text": "HOH: Yash\nNOMINEES: Angela, Dee",
                    "author_id": 1,
                    "line_numbers": [1],
                },
                headers=AUTH,
            )

    _run(scenario())

    assert engine.watcher.house_status.current.hoh == "Yash"
    assert engine.watcher.house_status.current.nominees == ()


def test_batch_apply_writes_facts_and_rules(tmp_path: Path, monkeypatch) -> None:
    engine, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/batch/apply",
                json={"text": "FACT: Yash won HOH.\nRULE: Never lie.", "author_id": 7},
                headers=AUTH,
            )
            body = await resp.json()
            assert len(body["written"]) == 2

    _run(scenario())

    assert len(engine.knowledge.active_items()) == 2


def test_batch_apply_requires_integer_author_id(tmp_path: Path, monkeypatch) -> None:
    _, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/batch/apply",
                json={"text": "FACT: x.", "author_id": "not-an-int"},
                headers=AUTH,
            )
            assert resp.status == 400

    _run(scenario())


# ==========================================================
# Sources / events / diagnostics / routing / conflicts
# ==========================================================


def test_sources_endpoint_returns_expected_shape(tmp_path: Path, monkeypatch) -> None:
    _, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/sources", headers=AUTH)
            assert resp.status == 200
            body = await resp.json()
            assert "rss" in body
            assert "house_image" in body
            assert "monitors" in body

    _run(scenario())


def test_events_endpoint_returns_recent_log(tmp_path: Path, monkeypatch) -> None:
    _, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/events?limit=10", headers=AUTH)
            assert resp.status == 200
            body = await resp.json()
            assert body == []

    _run(scenario())


def test_diagnostics_endpoint_includes_health_and_watcher(
    tmp_path: Path, monkeypatch
) -> None:
    _, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/diagnostics", headers=AUTH)
            body = await resp.json()
            assert "health" in body
            assert "watcher" in body
            assert "pending_events" in body

    _run(scenario())


def test_discord_routing_endpoint_covers_every_event_type(
    tmp_path: Path, monkeypatch
) -> None:
    from production.events import EventType

    _, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/discord/routing", headers=AUTH)
            body = await resp.json()
            assert set(body["routing"].keys()) == {e.value for e in EventType}

    _run(scenario())


def test_discord_routing_image_changed_targets_house_status_and_live_updates(
    tmp_path: Path, monkeypatch
) -> None:
    # LIVE_UPDATES_CHANNEL and HOUSE_STATUS_CHANNEL both have real
    # hardcoded defaults in config.py, so this holds without patching
    # any channel IDs -- see services/discord_output.py _destinations().
    _, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/discord/routing", headers=AUTH)
            body = await resp.json()
            channels = {
                d["channel"] for d in body["routing"]["image_changed"]["destinations"]
            }
            assert channels == {"#house-status", "#live-updates"}

    _run(scenario())


def test_conflicts_endpoint_flags_disagreement(tmp_path: Path, monkeypatch) -> None:
    engine, app = _build(monkeypatch, tmp_path)
    engine.knowledge.teach(KnowledgeType.STATE, "Yash", 1, topic="HOH")
    # Simulate an automated source moving HouseStatus without a
    # corresponding taught STATE update.
    engine.watcher.house_status.current = HouseStatus(hoh="Barrett")

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/conflicts", headers=AUTH)
            body = await resp.json()
            topics = {c["topic"] for c in body["conflicts"]}
            assert "HOH" in topics

    _run(scenario())
