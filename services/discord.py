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
import time

import discord
from discord.ext import commands

from config import (
    BOT_NAME,
    DISCORD_TOKEN,
    ENABLE_SCHEDULER,
    LIVE_UPDATES_CHANNEL,
)
from services.logger import ProductionLogger
from services.scheduler import Scheduler
from services.ai_service import format_game_state, generate_julie_response

AI_COOLDOWN_SECONDS = 8


class DiscordService:

    def __init__(self) -> None:

        self.logger = ProductionLogger.get("Discord")

        self.scheduler = Scheduler()

        self._ai_cooldowns: dict[int, float] = {}

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
    # AI Chat
    # ==========================================================

    def _ai_cooldown_remaining(self, user_id: int) -> float:
        """Returns seconds left on a user's cooldown, or 0 if clear."""

        last_used = self._ai_cooldowns.get(user_id, 0.0)
        elapsed = time.monotonic() - last_used
        remaining = AI_COOLDOWN_SECONDS - elapsed

        return max(0.0, remaining)

    async def generate_ai_reply(
        self,
        user_id: int,
        channel_id: int,
        user_text: str,
    ) -> str:
        """Generates Julie's AI reply, applying cooldown and real game
        state context.

        Shared by the mention/DM handler and the /chat command, so both
        entry points behave identically rather than drifting apart.

        Returns a cooldown message if the user is rate-limited, rather
        than raising, since callers just send whatever string comes
        back.
        """

        remaining = self._ai_cooldown_remaining(user_id)

        if remaining > 0:
            return (
                f"⏳ Slow down, Houseguest — give me {remaining:.0f} "
                "more second(s) before your next question."
            )

        self._ai_cooldowns[user_id] = time.monotonic()

        house_status = self.scheduler.engine.watcher.house_status.current
        competition = self.scheduler.engine.watcher.competition.current

        game_state = format_game_state(house_status, competition)

        return await generate_julie_response(
            channel_id,
            user_text,
            game_state=game_state,
        )

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
                guild = (
                    live_channel.guild
                    if live_channel is not None
                    else None
                )

                if guild is not None:
                    self.bot.tree.copy_global_to(guild=guild)
                    guild_synced = await self.bot.tree.sync(guild=guild)
                    self.logger.info(
                        "Synced %s slash command(s) to guild %s.",
                        len(guild_synced),
                        guild.id,
                    )
                else:
                    self.logger.warning(
                        (
                            "Could not resolve LIVE_UPDATES_CHANNEL=%s "
                            "for guild command sync."
                        ),
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

            if ENABLE_SCHEDULER:
                asyncio.create_task(
                    self.scheduler.start()
                )
                self.logger.info(
                    "Production Scheduler started."
                )
            else:
                self.logger.info(
                    "Production Scheduler disabled (ENABLE_SCHEDULER=false)."
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

        @self.bot.event
        async def on_message(message):
            # Never respond to other bots
            if message.author.bot:
                return

            # Trigger Julie's AI when mentioned or in DMs
            is_mentioned = self.bot.user in message.mentions
            is_dm = isinstance(message.channel, discord.DMChannel)

            if is_mentioned or is_dm:
                # Remove mention token from the message text
                clean_text = (
                    message.content
                    .replace(
                        f"<@{self.bot.user.id}>",
                        "",
                    )
                    .strip()
                )

                if not clean_text:
                    await message.channel.send(
                        "Good evening, Houseguest. Did you need "
                        "the Executive Producer?"
                    )
                    return

                async with message.channel.typing():
                    try:
                        ai_reply = await self.generate_ai_reply(
                            message.author.id,
                            message.channel.id,
                            clean_text,
                        )
                        await message.channel.send(ai_reply)
                    except Exception:
                        self.logger.exception(
                            "Failed while generating Julie response."
                        )

            # Allow other command processors to run
            await self.bot.process_commands(message)

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

    async def shutdown(self) -> None:
        """Gracefully stop scheduler and close the bot connection."""

        try:
            # Stop the scheduler loop
            self.scheduler.stop()

            # Close the Discord connection
            await self.bot.close()

            self.logger.info("DiscordService shutdown complete.")

        except Exception:
            self.logger.exception("Error during DiscordService.shutdown()")
