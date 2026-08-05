"""
Julie ChenBot Discord Service
=============================

Responsible for connecting Julie ChenBot to Discord,
loading slash commands, and managing presence.
"""

from __future__ import annotations

import importlib
import pkgutil

import discord
from discord.ext import commands

from config import BOT_NAME, DISCORD_TOKEN
from services.logger import ProductionLogger


class DiscordService:
    """
    Main Discord service.
    """

    def __init__(self) -> None:

        self.logger = ProductionLogger.get("Discord")

        intents = discord.Intents.default()

        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True

        self.bot = commands.Bot(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

        self.register_events()

    # ==========================================================
    # Events
    # ==========================================================

    def register_events(self) -> None:

        @self.bot.event
        async def on_ready():

            self.logger.info(
                f"Logged in as {self.bot.user}"
            )

            print()
            print("=" * 60)
            print("CONNECTED SERVERS")
            print("=" * 60)

            if not self.bot.guilds:
                print("Julie is not connected to any servers.")

            for guild in self.bot.guilds:
                print(f"• {guild.name}")
                print(f"  ID: {guild.id}")
                print(f"  Members: {guild.member_count}")
                print()

            print("=" * 60)
            print()

            await self.bot.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name="the Live Feeds 👀",
                ),
            )

            self.load_commands()

            try:

                synced = await self.bot.tree.sync()

                self.logger.info(
                    "Synced %s slash command(s).",
                    len(synced),
                )

            except Exception:
                self.logger.exception(
                    "Failed to sync slash commands."
                )

            print()
            print("=" * 60)
            print(f"{BOT_NAME} is ONLINE")
            print("=" * 60)
            print()

    # ==========================================================
    # Slash Command Helper
    # ==========================================================

    def command(self, *args, **kwargs):
        return self.bot.tree.command(*args, **kwargs)

    # ==========================================================
    # Command Loader
    # ==========================================================

    def load_commands(self) -> None:

        import commands

        loaded = 0

        for _, module_name, _ in pkgutil.iter_modules(commands.__path__):

            if module_name.startswith("_"):
                continue

            try:

                module = importlib.import_module(
                    f"commands.{module_name}"
                )

                if hasattr(module, "register"):

                    module.register(self)

                    loaded += 1

                    self.logger.info(
                        "Loaded command: %s",
                        module_name,
                    )

            except Exception:

                self.logger.exception(
                    "Failed loading command: %s",
                    module_name,
                )

        self.logger.info(
            "Loaded %s command module(s).",
            loaded,
        )

    # ==========================================================
    # Run
    # ==========================================================

    def run(self) -> None:

        if not DISCORD_TOKEN:
            raise RuntimeError(
                "DISCORD_TOKEN is missing from .env"
            )

        self.logger.info(
            "Connecting to Discord..."
        )

        self.bot.run(DISCORD_TOKEN)