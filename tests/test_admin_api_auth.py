"""Tests for admin_api/auth.py's bearer-token middleware."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

import admin_api.auth as auth_module
from admin_api.server import build_app
from database.storage import Storage
from production.engine import ProductionEngine


def _build_app(monkeypatch, tmp_path: Path, api_key: str | None):
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    monkeypatch.setattr(auth_module, "ADMIN_API_KEY", api_key)

    engine = ProductionEngine(storage=Storage())
    return build_app(engine)


def test_missing_authorization_header_is_rejected(tmp_path: Path, monkeypatch) -> None:
    app = _build_app(monkeypatch, tmp_path, api_key="test-secret")

    async def run() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/health")
            assert resp.status == 401
            body = await resp.json()
            assert body == {"error": "unauthorized"}

    asyncio.run(run())


def test_wrong_token_is_rejected(tmp_path: Path, monkeypatch) -> None:
    app = _build_app(monkeypatch, tmp_path, api_key="test-secret")

    async def run() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/health", headers={"Authorization": "Bearer wrong-token"}
            )
            assert resp.status == 401

    asyncio.run(run())


def test_correct_token_is_accepted(tmp_path: Path, monkeypatch) -> None:
    app = _build_app(monkeypatch, tmp_path, api_key="test-secret")

    async def run() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/health", headers={"Authorization": "Bearer test-secret"}
            )
            assert resp.status == 200

    asyncio.run(run())


def test_malformed_authorization_header_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    app = _build_app(monkeypatch, tmp_path, api_key="test-secret")

    async def run() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/health", headers={"Authorization": "test-secret"}
            )
            assert resp.status == 401

    asyncio.run(run())


def test_unset_admin_api_key_rejects_every_request(
    tmp_path: Path, monkeypatch
) -> None:
    """Even a request that happens to send an empty token must never
    be treated as authorized when no key is configured -- there must
    be no way to accidentally expose an unauthenticated admin surface."""

    app = _build_app(monkeypatch, tmp_path, api_key="")

    async def run() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/health", headers={"Authorization": "Bearer "}
            )
            assert resp.status == 401

    asyncio.run(run())


# ==========================================================
# D.4/D.5/D.6/D.7 -- GET /health: the one deliberate, narrow exception
# ==========================================================


def test_health_returns_200_with_no_authorization_header_at_all(
    tmp_path: Path, monkeypatch
) -> None:
    app = _build_app(monkeypatch, tmp_path, api_key="test-secret")

    async def run() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            body = await resp.json()
            assert body == {"status": "ok"}

    asyncio.run(run())


def test_health_returns_200_even_when_admin_api_key_is_unset(
    tmp_path: Path, monkeypatch
) -> None:
    """Railway's liveness check must see the process as up even in a
    misconfigured deploy (ADMIN_API_KEY forgotten) -- only the
    authenticated surface should be locked out in that case, not the
    liveness endpoint itself."""

    app = _build_app(monkeypatch, tmp_path, api_key="")

    async def run() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200

    asyncio.run(run())


def test_health_bypass_is_get_only_post_health_still_requires_auth(
    tmp_path: Path, monkeypatch
) -> None:
    app = _build_app(monkeypatch, tmp_path, api_key="test-secret")

    async def run() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/health")
            assert resp.status == 401

    asyncio.run(run())


def test_authenticated_health_endpoint_still_requires_a_token(
    tmp_path: Path, monkeypatch
) -> None:
    """The /health bypass must not leak onto /api/v1/health -- exact
    path match only, never a prefix match."""

    app = _build_app(monkeypatch, tmp_path, api_key="test-secret")

    async def run() -> None:
        async with TestClient(TestServer(app)) as client:
            unauthenticated = await client.get("/api/v1/health")
            assert unauthenticated.status == 401

            authenticated = await client.get(
                "/api/v1/health", headers={"Authorization": "Bearer test-secret"}
            )
            assert authenticated.status == 200
            body = await authenticated.json()
            assert "engine" in body and "info" in body

    asyncio.run(run())


def test_a_different_protected_endpoint_remains_protected(
    tmp_path: Path, monkeypatch
) -> None:
    """The /health exemption is scoped to that one path -- spot-check
    an unrelated route to prove nothing else was accidentally
    widened."""

    app = _build_app(monkeypatch, tmp_path, api_key="test-secret")

    async def run() -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/knowledge")
            assert resp.status == 401

    asyncio.run(run())
