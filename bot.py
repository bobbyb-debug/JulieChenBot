"""
Julie ChenBot
=============

Main entry point for Julie ChenBot.
"""

from __future__ import annotations

import sys

from core.application import JulieApplication
from services.logger import (
    ProductionLogger,
    generate_session_id,
    shutdown_banner,
    startup_banner,
)


def main() -> None:

    logger = ProductionLogger.get("Bot")

    session_id = generate_session_id()

    print(startup_banner(session_id))
    print()

    logger.info("Production systems are coming online.")
    logger.info("Session ID: %s", session_id)

    app = None

    try:

        logger.info("Loading application...")

        app = JulieApplication()

        logger.info("Application loaded successfully.")

        logger.info("Connecting to Discord...")

        app.run()

    except KeyboardInterrupt:

        # Read whatever engine stats are available; any missing
        # link in the chain (app never built, engine never
        # started) falls back to None, and shutdown_banner()
        # reports those as "N/A".
        engine = getattr(
            getattr(getattr(app, "discord", None), "scheduler", None),
            "engine",
            None,
        )

        print()
        print(
            shutdown_banner(
                session_id,
                uptime=getattr(engine, "uptime", None),
                tick_count=getattr(engine, "tick_count", None),
                error_count=getattr(engine, "error_count", None),
            )
        )

        logger.info("Julie ChenBot stopped by user.")

    except Exception:

        logger.exception(
            "Julie ChenBot encountered a fatal error."
        )

        sys.exit(1)


if __name__ == "__main__":
    main()
