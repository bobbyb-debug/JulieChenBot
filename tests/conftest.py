"""Shared pytest fixtures for Julie ChenBot's test suite."""

from __future__ import annotations

import pytest

from database import hamsterwatch_archive as _hamsterwatch_archive_module


@pytest.fixture(autouse=True)
def _isolate_hamsterwatch_archive_file(tmp_path, monkeypatch):
    """Redirects HamsterwatchArchive's default database path to a
    per-test temp file.

    Any test that constructs a ProductionWatcher or HamsterwatchMonitor
    without explicitly injecting an archive (e.g. tests focused on an
    unrelated monitor) would otherwise create/write
    database/hamsterwatch_archive.db in the real repository — mirrors
    the same isolation ai_service tests already give
    services.ai_service.CHAT_HISTORY_FILE.
    """

    monkeypatch.setattr(
        _hamsterwatch_archive_module,
        "ARCHIVE_FILE",
        tmp_path / "hamsterwatch_archive.db",
    )
