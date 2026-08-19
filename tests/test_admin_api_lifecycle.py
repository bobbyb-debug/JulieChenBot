"""Tests for the admin API's task lifecycle (services/discord.py
_start_admin_api()/_on_admin_api_task_done()/_stop_admin_api()).

Root cause fixed here: on_ready() used to call
`asyncio.create_task(run_admin_api(...))` and discard the return
value -- a documented asyncio pitfall (the event loop only keeps a
*weak* reference to tasks; an unreferenced task may be garbage
collected before it's done). DiscordService.admin_api_task now holds
the one strong reference for the instance's entire lifetime, and
shutdown() cancels it explicitly rather than relying on process exit.

No real Discord connection is made anywhere in this file -- same
bare-construction pattern as tests/test_command_registration.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import discord.ext.commands as dc

from services.discord import DiscordService


class FakeEngine:
    pass


class _FakeLogger:
    """Records error/info calls instead of writing to the real logger,
    so tests can assert on exactly what got logged."""

    def __init__(self) -> None:
        self.errors: list[tuple] = []
        self.infos: list[tuple] = []

    def info(self, *args, **kwargs) -> None:
        self.infos.append((args, kwargs))

    def error(self, *args, **kwargs) -> None:
        self.errors.append((args, kwargs))

    def exception(self, *args, **kwargs) -> None:
        self.errors.append((args, kwargs))

    def warning(self, *args, **kwargs) -> None:
        pass


class _FakeAdminApi:
    """Stands in for admin_api.server.run_admin_api: blocks until
    cancelled (matching the real function's `await
    asyncio.Event().wait()`), records whether it started and whether
    its cleanup ran, and can optionally raise to simulate a crash."""

    def __init__(self, *, raise_after_start: Exception | None = None) -> None:
        self.started = False
        self.cleaned_up = False
        self.raise_after_start = raise_after_start

    async def run(self, engine, *, port: int | None = None) -> None:
        self.started = True
        try:
            if self.raise_after_start is not None:
                raise self.raise_after_start
            await asyncio.Event().wait()
        finally:
            self.cleaned_up = True


def _bare_discord_service() -> DiscordService:
    """A DiscordService with __init__ skipped (no real Discord
    connection) -- matches the pattern in
    tests/test_command_registration.py."""

    ds = DiscordService.__new__(DiscordService)
    ds.bot = dc.Bot(command_prefix="!", intents=discord.Intents.default())
    ds.scheduler = SimpleNamespace(engine=FakeEngine())
    ds.admin_api_task = None
    ds.logger = _FakeLogger()
    return ds


async def _let_task_run() -> None:
    """Yields control back to the loop so a just-created task gets a
    chance to run up to its first await point."""

    for _ in range(10):
        await asyncio.sleep(0)


# ==========================================================
# D.1 / D.8 -- task reference retained, and only when enabled
# ==========================================================


def test_start_admin_api_retains_a_real_task_reference(monkeypatch) -> None:
    fake = _FakeAdminApi()
    monkeypatch.setattr("services.discord.ENABLE_ADMIN_API", True)
    monkeypatch.setattr("services.discord.run_admin_api", fake.run)
    ds = _bare_discord_service()

    async def scenario() -> None:
        ds._start_admin_api()

        assert ds.admin_api_task is not None
        assert isinstance(ds.admin_api_task, asyncio.Task)
        assert not ds.admin_api_task.done()

        await ds._stop_admin_api()

    asyncio.run(scenario())


def test_admin_api_disabled_creates_no_task(monkeypatch) -> None:
    monkeypatch.setattr("services.discord.ENABLE_ADMIN_API", False)
    ds = _bare_discord_service()

    ds._start_admin_api()

    assert ds.admin_api_task is None


# ==========================================================
# D.2 -- graceful cancellation/cleanup
# ==========================================================


def test_stop_admin_api_cancels_the_task_and_runs_its_cleanup(monkeypatch) -> None:
    fake = _FakeAdminApi()
    monkeypatch.setattr("services.discord.ENABLE_ADMIN_API", True)
    monkeypatch.setattr("services.discord.run_admin_api", fake.run)
    ds = _bare_discord_service()

    async def scenario() -> None:
        ds._start_admin_api()
        await _let_task_run()
        assert fake.started is True
        assert not ds.admin_api_task.done()

        await ds._stop_admin_api()

        assert ds.admin_api_task.cancelled()
        assert fake.cleaned_up is True

    asyncio.run(scenario())


def test_stop_admin_api_is_a_safe_noop_when_never_started() -> None:
    ds = _bare_discord_service()

    asyncio.run(ds._stop_admin_api())

    assert ds.admin_api_task is None


# ==========================================================
# D.3 -- an unexpected crash is surfaced, not lost
# ==========================================================


def test_unexpected_admin_api_crash_is_logged_as_an_error(monkeypatch) -> None:
    fake = _FakeAdminApi(raise_after_start=RuntimeError("boom"))
    monkeypatch.setattr("services.discord.ENABLE_ADMIN_API", True)
    monkeypatch.setattr("services.discord.run_admin_api", fake.run)
    ds = _bare_discord_service()

    async def scenario() -> None:
        ds._start_admin_api()
        for _ in range(10):
            await asyncio.sleep(0)
            if ds.admin_api_task.done():
                break

    asyncio.run(scenario())

    assert ds.admin_api_task.done()
    assert not ds.admin_api_task.cancelled()
    assert len(ds.logger.errors) == 1
    logged_message = ds.logger.errors[0][0][0]
    assert "unexpectedly" in logged_message


def test_a_deliberate_cancellation_is_not_logged_as_an_error(monkeypatch) -> None:
    """_stop_admin_api()'s own cancel() must not trip the same
    "crash" logging _on_admin_api_task_done() does for a real
    failure -- a normal shutdown is not an error."""

    fake = _FakeAdminApi()
    monkeypatch.setattr("services.discord.ENABLE_ADMIN_API", True)
    monkeypatch.setattr("services.discord.run_admin_api", fake.run)
    ds = _bare_discord_service()

    async def scenario() -> None:
        ds._start_admin_api()
        await _let_task_run()
        await ds._stop_admin_api()

    asyncio.run(scenario())

    assert ds.logger.errors == []
