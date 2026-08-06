"""
Julie ChenBot Configuration
===========================

Central configuration for Julie ChenBot.

This module is the single source of truth for:
- Environment variables
- Project paths
- Discord configuration
- JokersUpdates configuration
- Application settings
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ==========================================================
# Load Environment Variables
# ==========================================================

load_dotenv()

# ==========================================================
# Project Paths
# ==========================================================

ROOT = Path(__file__).resolve().parent

ASSETS = ROOT / "assets"
DATABASE = ROOT / "database"
LOGS = ROOT / "logs"

for directory in (ASSETS, DATABASE, LOGS):
    directory.mkdir(parents=True, exist_ok=True)

# ==========================================================
# Application
# ==========================================================

BOT_NAME = "Julie ChenBot"
VERSION = "1.0.0"

DEBUG = False

PHASE = "Development" if DEBUG else "Production"

BUILD = os.getenv("BUILD", VERSION)

CHECK_INTERVAL = 60

# ==========================================================
# Discord
# ==========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")


def env_int(name: str, default: int = 0) -> int:
    """
    Safely read an integer from an environment variable.
    """

    value = os.getenv(name)

    if value is None or value.strip() == "":
        return default

    try:
        return int(value)
    except ValueError:
        return default


GUILD_ID = env_int("GUILD_ID")

LIVE_UPDATES_CHANNEL = env_int("LIVE_UPDATES_CHANNEL")
PRODUCTION_CHANNEL = env_int("PRODUCTION_CHANNEL")
HOUSE_STATUS_CHANNEL = env_int("HOUSE_STATUS_CHANNEL")
PRODUCTION_LOG_CHANNEL = env_int("PRODUCTION_LOG_CHANNEL")

# ==========================================================
# JokersUpdates
# ==========================================================

JOKERS_RSS_FEED = (
    "http://rss.jokersupdates.com/ubbthreads/rss/bbusaupdates/rss.php"
)

# Backwards compatibility
RSS_FEED = JOKERS_RSS_FEED

JOKERS_HOME = (
    "https://www.jokersupdates.com/"
)

HOUSE_STATUS_IMAGE = (
    "https://www.jokersupdates.com/ubbthreads/images/headers/bigbrother/hg/"
)

# ==========================================================
# HTTP
# ==========================================================

REQUEST_TIMEOUT = 15

USER_AGENT = (
    f"{BOT_NAME}/{VERSION} ({PHASE})"
)

# ==========================================================
# Logging
# ==========================================================

LOG_FILE = LOGS / "julie.log"