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
# Set STATE_DIR to a persistent disk mount when deploying Julie. Railway
# volumes are available at the configured absolute mount path (for example,
# /data), while local development keeps state inside the project folder.
STATE_DIR = Path(os.getenv("STATE_DIR", ROOT)).expanduser()
DATABASE = STATE_DIR / "database"
DATA = STATE_DIR / "data"
LOGS = STATE_DIR / "logs"

# Ensure required folders exist
for directory in (ASSETS, DATA, DATABASE, LOGS):
    directory.mkdir(parents=True, exist_ok=True)

# ==========================================================
# Application
# ==========================================================

BOT_NAME = "Julie ChenBot"
VERSION = "1.0.0"

CHECK_INTERVAL = 60  # Seconds between update checks

DEBUG = False


def env_flag(name: str, default: bool = False) -> bool:
    """Read a conventional true/false value from the environment."""

    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


# Keep monitoring off until it is explicitly enabled in a production host.
ENABLE_SCHEDULER = env_flag("ENABLE_SCHEDULER", default=False)

# ==========================================================
# Build Information
# ==========================================================

PHASE = "Development" if DEBUG else "Production"

BUILD = os.getenv("BUILD", VERSION)

# ==========================================================
# Discord
# ==========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")


def env_int(name: str, default: int = 0) -> int:
    """
    Safely read an integer from the environment.

    Returns the default value if the variable is
    missing, empty, or not a valid integer.
    """

    value = os.getenv(name)

    if value is None or value.strip() == "":
        return default

    try:
        return int(value)
    except ValueError:
        return default


GUILD_ID = env_int("GUILD_ID")

# Live-updates and house-status were both explicitly provisioned for
# Julie's production feed, so both get real defaults the same way —
# routing keeps working even without the env vars set.
LIVE_UPDATES_CHANNEL = env_int("LIVE_UPDATES_CHANNEL", 1534581029871026347)
PRODUCTION_CHANNEL = env_int("PRODUCTION_CHANNEL")
HOUSE_STATUS_CHANNEL = env_int("HOUSE_STATUS_CHANNEL", 1534566047611617371)
PRODUCTION_LOG_CHANNEL = env_int("PRODUCTION_LOG_CHANNEL")

# A Discord role ID trusted to run /teach batch and /teach update
# (see commands/teach.py _is_trusted_moderator()) without needing full
# server administrator. Unset means those two commands are effectively
# admin-only in practice (a full administrator always qualifies
# regardless of this setting) -- there is no unsafe default here, only
# a missing opt-in. /teach fact, /teach rule, /teach correction, and
# /teach forget are unaffected by this setting: they stay restricted
# to full administrators via Discord's own default_permissions gate,
# exactly as before.
TRUSTED_MODERATOR_ROLE_ID = env_int("TRUSTED_MODERATOR_ROLE_ID")

# ==========================================================
# Admin API (dashboard integration)
# ==========================================================

# Narrow, authenticated HTTP surface consumed only by the separate
# Julie ChenBot Admin Dashboard (bobbyb-debug/julie-chenbot-admin-
# dashboard) -- never by end users, never by Discord itself. Off by
# default, same posture as ENABLE_SCHEDULER: nothing about this
# process's behavior changes unless a deployer opts in explicitly.
# See admin_api/ for the implementation.
ENABLE_ADMIN_API = env_flag("ENABLE_ADMIN_API", default=False)

ADMIN_API_PORT = env_int("ADMIN_API_PORT", 8080)

# Bearer token every admin API request must present (Authorization:
# Bearer <token>). Required whenever ENABLE_ADMIN_API is on -- see
# admin_api/auth.py, which refuses to start the server with this
# unset. Never logged, never echoed back in any response.
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

# ==========================================================
# JokersUpdates
# ==========================================================

RSS_FEED = (
    "http://rss.jokersupdates.com/ubbthreads/rss/bbusaupdates/rss.php"
)

JOKERS_HOME = (
    "https://www.jokersupdates.com/"
)

HOUSE_STATUS_IMAGE = (
    "http://www.jokersupdates.com/ubbthreads/images/headers/bigbrother/hg/"
    "bbupdatesblock1786231774.png"
)

# Page scraped to discover the CURRENT bbupdatesblock*.png filename.
# The image filename embeds a Unix timestamp and rotates whenever the
# house status changes, so HOUSE_STATUS_IMAGE above is only a fallback
# seed — discovery is the source of truth at runtime.
# Maximum number of missed RSS items announced in one catch-up pass.
# Prevents a long outage from flooding the channel; anything older is
# marked as seen without announcing.
RSS_MAX_BACKFILL = env_int("RSS_MAX_BACKFILL", 15)

HOUSE_STATUS_PAGE = os.getenv(
    "HOUSE_STATUS_PAGE",
    JOKERS_HOME,
)

# ==========================================================
# Imgur
# ==========================================================

# Joker's Updates embeds (IMG)-tagged post images via an Imgur
# client-side widget that only exposes an opaque image ID server-side
# (see production/imgur.py). Resolving that ID into a real URL uses
# Imgur's official public read API (https://apidocs.imgur.com/#image),
# which requires a free, registered app's Client-ID sent as
# `Authorization: Client-ID <id>` -- no OAuth, no user login. Left
# unset, production/imgur.py.ImgurResolver logs one warning and
# (IMG) posts fall back to text-only; nothing else in Julie is
# affected.
IMGUR_CLIENT_ID = os.getenv("IMGUR_CLIENT_ID", "")

# ==========================================================
# Logging
# ==========================================================

LOG_FILE = LOGS / "julie.log"
