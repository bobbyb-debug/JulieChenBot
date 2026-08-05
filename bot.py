"""
Julie ChenBot
=============

Main entry point for Julie ChenBot.
"""

from __future__ import annotations

import sys

from config import BOT_NAME, VERSION
from core.application import JulieApplication
from personality.julie import Julie
from services.logger import ProductionLogger


def banner() -> None:

    print()
    print("═" * 55)
    print(f"🤖 {BOT_NAME} v{VERSION}")
    print("═" * 55)
    print()


def main() -> None:

    logger = ProductionLogger.get("Bot")
    julie = Julie()

    banner()

    print(julie.startup())
    print()

    logger.info("Production systems are coming online.")

    try:

        logger.info("Loading application...")

        app = JulieApplication()

        logger.info("Application loaded successfully.")

        logger.info("Connecting to Discord...")

        app.run()

    except KeyboardInterrupt:

        print()
        print(julie.shutdown())
        logger.info("Julie ChenBot stopped by user.")

    except Exception:

        logger.exception(
            "Julie ChenBot encountered a fatal error."
        )

        sys.exit(1)


if __name__ == "__main__":
    main()