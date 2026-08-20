"""
Julie ChenBot Logging Service
=============================

Centralized logging for Julie ChenBot.

Owns:
    • ProductionLogger  — configured loggers (rotating file + console)
    • Session IDs        — short identifiers for a single bot run
    • Startup/shutdown banners — console text printed around a run

Keeping the banners here (rather than in separate core/startup.py
and core/shutdown.py modules) means every piece of console/log
output formatting has one home. The banners take plain data as
arguments — they know nothing about ProductionEngine, Scheduler,
or Discord; the caller is responsible for supplying real values.
"""

from __future__ import annotations

import logging
import platform
import uuid
from datetime import datetime, timedelta, timezone
from logging import Logger
from logging.handlers import RotatingFileHandler
from typing import Optional

from config import BOT_NAME, BUILD, LOG_FILE, PHASE, VERSION

# ==========================================================
# Rotation Settings
# ==========================================================

MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB
BACKUP_COUNT = 5


class ProductionLogger:
    """
    Provides configured loggers throughout Julie ChenBot.
    """

    _formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-15s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    @classmethod
    def get(cls, name: str) -> Logger:

        logger = logging.getLogger(name)

        if logger.handlers:
            return logger

        logger.setLevel(logging.INFO)
        logger.propagate = False

        # Defensive: ensure logs/ exists even if config.py's own
        # directory creation was ever skipped or changed.
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

        console = logging.StreamHandler()
        console.setFormatter(cls._formatter)

        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=MAX_LOG_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )

        file_handler.setFormatter(cls._formatter)

        logger.addHandler(console)
        logger.addHandler(file_handler)

        return logger


# ==========================================================
# Session
# ==========================================================


def generate_session_id() -> str:
    """
    Generates a short session identifier for this run.
    """

    return uuid.uuid4().hex[:8]


# ==========================================================
# Banners
# ==========================================================

BORDER = "=" * 60


def startup_banner(session_id: str) -> str:
    """
    Builds the startup banner text.
    """

    started = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    python_version = platform.python_version()

    lines = [
        BORDER,
        f"🤖 {BOT_NAME}",
        "Production Engine",
        BORDER,
        f"Version : {VERSION}",
        f"Phase   : {PHASE}",
        f"Build   : {BUILD}",
        f"Python  : {python_version}",
        f"Started : {started}",
        f"Session : {session_id}",
        "",
        "Expect the unexpected.",
        BORDER,
    ]

    return "\n".join(lines)


def _format_uptime(uptime: Optional[timedelta]) -> str:

    if uptime is None:
        return "N/A"

    return str(timedelta(seconds=int(uptime.total_seconds())))


def _format_count(value: Optional[int]) -> str:

    return "N/A" if value is None else str(value)


def shutdown_banner(
    session_id: str,
    uptime: Optional[timedelta] = None,
    tick_count: Optional[int] = None,
    error_count: Optional[int] = None,
) -> str:
    """
    Builds the shutdown banner text.

    The caller supplies uptime/tick_count/error_count directly.
    This function has no knowledge of ProductionEngine, Scheduler,
    or how to obtain these values — any of them may be None, and
    are then reported as "N/A".
    """

    lines = [
        BORDER,
        "Julie ChenBot — Production has ended for the evening.",
        BORDER,
        f"Uptime      : {_format_uptime(uptime)}",
        f"Tick Count  : {_format_count(tick_count)}",
        f"Error Count : {_format_count(error_count)}",
        f"Session     : {session_id}",
        "",
        "Goodbye. And remember to love one another.",
        BORDER,
    ]

    return "\n".join(lines)
