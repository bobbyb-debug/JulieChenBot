"""
Julie ChenBot Personality Engine
================================

Defines Julie ChenBot's voice and personality.
"""

from __future__ import annotations

import random


class Julie:

    def __init__(self) -> None:

        self.greetings = [
            "Good evening, Houseguests...",
            "Houseguests...",
            "Attention, Houseguests...",
        ]

        self.transitions = [
            "But first...",
            "Let's take a look...",
            "As always...",
            "Before we continue...",
        ]

        self.closings = [
            "Expect the unexpected.",
            "The game never sleeps.",
            "Stay tuned, Houseguests.",
        ]

    def greeting(self) -> str:
        return random.choice(self.greetings)

    def transition(self) -> str:
        return random.choice(self.transitions)

    def closing(self) -> str:
        return random.choice(self.closings)

    def startup(self) -> str:
        return (
            f"{self.greeting()}\n\n"
            "Julie ChenBot is now on duty."
        )

    def shutdown(self) -> str:
        return (
            "Production has ended for the evening.\n\n"
            "Julie ChenBot has gone offline."
        )

    def production(self, message: str) -> str:
        return (
            "🎥 **PRODUCTION ANNOUNCEMENT**\n\n"
            "Good evening, Houseguests...\n\n"
            f"{message}"
        )

    def reply(self, message: str) -> str:
        return (
            f"{self.transition()}\n\n"
            f"{message}"
        )

    def error(self) -> str:
        return (
            "Production is temporarily unable to retrieve the latest information."
        )