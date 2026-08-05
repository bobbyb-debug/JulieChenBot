"""
Julie ChenBot
=============

Main application entry point.
"""

from __future__ import annotations

import sys

from config import BOT_NAME, VERSION
from personality.julie import Julie
from services.logger import ProductionLogger


class JulieChenBot:
    """
    Main Julie ChenBot application.
    """

    def __init__(self) -> None:
        self.logger = ProductionLogger.get("Bot")
        self.julie = Julie()

    def banner(self) -> None:
        print()
        print("═" * 55)
        print(f"🤖 {BOT_NAME} v{VERSION}")
        print("═" * 55)
        print()

    def startup(self) -> None:
        self.banner()

        print(self.julie.startup())
        print()

        self.logger.info("Production systems are coming online.")

        systems = [
            "Configuration",
            "Logger",
            "Personality",
            "Scheduler",
        ]

        for system in systems:
            print(f"✓ {system}")
            self.logger.info("%s loaded successfully.", system)

        print()
        print("Julie ChenBot is standing by.")
        print()
        print("═" * 55)

    def shutdown(self) -> None:
        print()
        print(self.julie.shutdown())

        self.logger.info("Production has ended for the evening.")

    def run(self) -> None:
        try:
            self.startup()

        except KeyboardInterrupt:
            self.shutdown()
            sys.exit(0)

        except Exception:
            self.logger.exception("Julie ChenBot encountered an unexpected error.")
            raise


def main() -> None:
    bot = JulieChenBot()
    bot.run()


if __name__ == "__main__":
    main()