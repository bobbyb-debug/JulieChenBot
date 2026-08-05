"""
Julie ChenBot Application
=========================

Main application controller.
"""

from services.discord import DiscordService


class JulieApplication:

    def __init__(self) -> None:
        self.discord = DiscordService()

    def run(self) -> None:
        self.discord.run()