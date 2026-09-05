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


def test_game_state_includes_official_state_keyed_by_topic(
    tmp_path: Path, monkeypatch
) -> None:
    """official_state is the actual source of truth (KnowledgeStore
    STATE items) -- separate from and never influenced by
    house_status, the automated live-feed observation above."""

    engine, app = _build(monkeypatch, tmp_path)
    # Automated observation disagrees on purpose -- official_state
    # must reflect only what's been taught, regardless.
    engine.watcher.house_status.current = HouseStatus(hoh="Taylor")
    engine.knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/game-state", headers=AUTH)
            body = await resp.json()
            assert body["official_state"]["HOH"]["content"] == "Yash"
            assert body["official_state"]["HOH"]["author_id"] == 1
            assert body["house_status"]["hoh"] == "Taylor"

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


def test_reactivate_knowledge_restores_it_in_place(
    tmp_path: Path, monkeypatch
) -> None:
    engine, app = _build(monkeypatch, tmp_path)
    item = engine.knowledge.teach(KnowledgeType.FACT, "Some fact.", 1)
    engine.knowledge.forget(item.id)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                f"/api/v1/knowledge/{item.id}/reactivate", headers=AUTH
            )
            assert resp.status == 200
            body = await resp.json()
            assert body == {"id": item.id, "reactivated": True}

            second = await client.post(
                f"/api/v1/knowledge/{item.id}/reactivate", headers=AUTH
            )
            body2 = await second.json()
            assert body2["reactivated"] is False  # already active

    _run(scenario())
    restored = engine.knowledge.get(item.id)
    assert restored.active is True
    assert restored.content == "Some fact."
    assert len(engine.knowledge.all_items()) == 1  # no duplicate created


def test_reactivate_knowledge_rejects_a_non_integer_id(
    tmp_path: Path, monkeypatch
) -> None:
    _, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/knowledge/not-a-number/reactivate", headers=AUTH
            )
            assert resp.status == 400

    _run(scenario())


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


def test_state_apply_writes_official_knowledge_never_house_status(
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

    assert engine.knowledge.active_state("HOH").content == "Yash"
    # /state/apply must never touch the automated, RSS-driven
    # HouseStatus object -- only KnowledgeStore official facts.
    assert engine.watcher.house_status.current.hoh == ""


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

    assert engine.knowledge.active_state("HOH").content == "Yash"
    assert engine.knowledge.active_state("NOMINEES") is None


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


def test_conflicts_endpoint_reason_does_not_imply_equal_authority(
    tmp_path: Path, monkeypatch
) -> None:
    """The dashboard must not present house_status and taught STATE
    as two equally-authoritative sources -- the reason text says the
    live feed is not authoritative, explicitly."""

    engine, app = _build(monkeypatch, tmp_path)
    engine.knowledge.teach(KnowledgeType.STATE, "Yash", 1, topic="HOH")
    engine.watcher.house_status.current = HouseStatus(hoh="Taylor")

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/conflicts", headers=AUTH)
            body = await resp.json()
            hoh_conflict = next(c for c in body["conflicts"] if c["topic"] == "HOH")
            assert "not authoritative" in hoh_conflict["reason"].lower()
            assert hoh_conflict["taught_value"] == "Yash"
            assert hoh_conflict["house_status_value"] == "Taylor"

    _run(scenario())


# ==========================================================
# Weekly game state -- the dashboard's CURRENT WEEK / WEEKLY ARCHIVE /
# CLOSE WEEK / START NEW WEEK control plane (see production/
# knowledge.py KnowledgeStore.start_new_week()/close_week()).
# ==========================================================


def test_week_status_reports_current_week_and_archive(
    tmp_path: Path, monkeypatch
) -> None:
    engine, app = _build(monkeypatch, tmp_path)
    engine.knowledge.start_new_week(8)
    engine.knowledge.close_week()
    engine.knowledge.start_new_week(9)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/week", headers=AUTH)
            body = await resp.json()
            assert body["current_week"] == 9
            assert body["archived_weeks"] == [8]

    _run(scenario())


def test_week_start_moves_the_current_week_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    engine, app = _build(monkeypatch, tmp_path)
    engine.knowledge.teach(KnowledgeType.STATE, "Dee", 1, topic="HOH")

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/week/start", json={"week": 9}, headers=AUTH
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["current_week"] == 9

    _run(scenario())

    # The old HOH value must now read as unconfirmed for the new week.
    assert engine.knowledge.current_state("HOH") is None
    assert engine.knowledge.active_state("HOH").content == "Dee"  # history preserved


def test_week_start_requires_an_integer_week(tmp_path: Path, monkeypatch) -> None:
    _, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/week/start", json={"week": "nine"}, headers=AUTH
            )
            assert resp.status == 400

    _run(scenario())


def test_week_close_freezes_the_current_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    engine, app = _build(monkeypatch, tmp_path)
    engine.knowledge.start_new_week(8)
    engine.knowledge.teach(KnowledgeType.STATE, "Yash", 1, topic="VETO_WINNER")

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/v1/week/close", headers=AUTH)
            assert resp.status == 200
            body = await resp.json()
            assert body["week"] == 8
            assert body["snapshot"]["VETO_WINNER"] == "Yash"

    _run(scenario())


def test_week_close_without_a_current_week_is_a_400(
    tmp_path: Path, monkeypatch
) -> None:
    _, app = _build(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/v1/week/close", headers=AUTH)
            assert resp.status == 400

    _run(scenario())


def test_week_detail_returns_current_snapshot_for_the_live_week(
    tmp_path: Path, monkeypatch
) -> None:
    engine, app = _build(monkeypatch, tmp_path)
    engine.knowledge.start_new_week(9)
    engine.knowledge.teach(KnowledgeType.STATE, "Barrett", 1, topic="HOH")

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/week/9", headers=AUTH)
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "current"
            assert body["snapshot"]["HOH"] == "Barrett"

    _run(scenario())


def test_week_detail_returns_404_for_a_week_never_recorded(
    tmp_path: Path, monkeypatch
) -> None:
    engine, app = _build(monkeypatch, tmp_path)
    engine.knowledge.start_new_week(9)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/week/3", headers=AUTH)
            assert resp.status == 404

    _run(scenario())


def test_week_backfill_records_a_historical_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    """A human-friendly way to record Week 7/8's final state after
    this feature first ships, with no manual JSON editing -- see
    KnowledgeStore.set_archived_week()."""

    engine, app = _build(monkeypatch, tmp_path)
    engine.knowledge.start_new_week(9)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/week/8/archive",
                json={"snapshot": {"VETO_WINNER": "Yash", "BB_BLOCKBUSTER": "Devens"}},
                headers=AUTH,
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["snapshot"]["BB_BLOCKBUSTER"] == "Devens"

            detail = await client.get("/api/v1/week/8", headers=AUTH)
            detail_body = await detail.json()
            assert detail_body["status"] == "archived"
            assert detail_body["snapshot"]["VETO_WINNER"] == "Yash"

    _run(scenario())


def test_game_state_official_state_excludes_a_stale_week_scoped_value(
    tmp_path: Path, monkeypatch
) -> None:
    """The dashboard's own /api/v1/game-state must not show a
    previous week's HOH as current either -- same fix as Julie's
    OFFICIAL GAME FACTS block (services/ai_service.py
    format_official_state())."""

    engine, app = _build(monkeypatch, tmp_path)
    engine.knowledge.teach(KnowledgeType.STATE, "Dee", 1, topic="HOH")
    engine.knowledge.start_new_week(9)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/game-state", headers=AUTH)
            body = await resp.json()
            assert "HOH" not in body["official_state"]
            assert body["current_week"] == 9

    _run(scenario())
