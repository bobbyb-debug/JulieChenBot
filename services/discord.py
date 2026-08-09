"""
Julie ChenBot Discord Service
=============================

Responsible for:

• Connecting to Discord
• Loading slash commands
• Starting Julie's Production Scheduler
• Managing Presence
• Binding Discord as a production output
"""

from __future__ import annotations

import asyncio
import importlib
import pkgutil

import discord
from discord.ext import commands

from config import BOT_NAME, DISCORD_TOKEN, LIVE_UPDATES_CHANNEL
from services.logger import ProductionLogger
from services.scheduler import Scheduler


class DiscordService:

    def __init__(self) -> None:

        self.logger = ProductionLogger.get("Discord")

        self.scheduler = Scheduler()

        intents = discord.Intents.default()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True

        self.bot = commands.Bot(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

        # Bind the existing Discord client to the production announcer.
        # The engine remains Discord-agnostic; the announcer owns outputs.
        self.scheduler.engine.announcer.bind_discord(self.bot)

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

            # Global commands can take time to propagate. Sync directly to
            # the configured live-feed channel's guild during development so
            # new commands such as /posttest appear immediately. The guild
            # command copy is local to that server and does not affect the
            # eventual global command deployment.
            try:
                live_channel = self.bot.get_channel(LIVE_UPDATES_CHANNEL)
                guild = live_channel.guild if live_channel is not None else None

                if guild is not None:
                    guild_synced = await self.bot.tree.sync(guild=guild)
                    self.logger.info(
                        "Synced %s slash command(s) to guild %s.",
                        len(guild_synced),
                        guild.id,
                    )
                else:
                    self.logger.warning(
                        "Could not resolve LIVE_UPDATES_CHANNEL=%s for guild command sync.",
                        LIVE_UPDATES_CHANNEL,
                    )

            except Exception:
                self.logger.exception(
                    "Failed syncing guild slash commands."
                )

            try:

                synced = await self.bot.tree.sync()

                self.logger.info(
                    "Synced %s global slash command(s).",
                    len(synced),
                )

            except Exception:

                self.logger.exception(
                    "Failed syncing global slash commands."
                )

            asyncio.create_task(
                self.scheduler.start()
            )

            self.logger.info(
                "Production Scheduler started."
            )

            print()
            print("=" * 60)
            print(f"{BOT_NAME} is ONLINE")
            print("=" * 60)
            print()

        @self.bot.event
        async def on_disconnect():

            self.logger.warning(
                "Julie disconnected from Discord."
            )

        @self.bot.event
        async def on_resumed():

            self.logger.info(
                "Discord connection resumed."
            )

    # ==========================================================
    # Slash Commands
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
                "DISCORD_TOKEN missing from .env"
            )

        self.logger.info(
            "Connecting to Discord..."
        )

        self.bot.run(DISCORD_TOKEN)
