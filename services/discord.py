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

from admin_api.server import run_admin_api
from config import (
    BOT_NAME,
    DISCORD_TOKEN,
    ENABLE_ADMIN_API,
    ENABLE_SCHEDULER,
    LIVE_UPDATES_CHANNEL,
)
from production.authorization import is_trusted_moderator
from production.hamsterwatch_context import (
    HistoricalContextResult,
    retrieve_historical_context,
)
from production.historical_retrieval import retrieve_hoh
from production.knowledge_summary import collect_summary_metadata, is_broad_knowledge_query
from services.logger import ProductionLogger
from services.scheduler import Scheduler
from services.ai_service import (
    format_game_state,
    format_historical_context,
    format_historical_events,
    format_knowledge_summary_guidance,
    format_learned_knowledge,
    format_long_term_memory,
    format_official_state,
    generate_julie_response,
)

AI_COOLDOWN_SECONDS = 8


class DiscordService:

    def __init__(self) -> None:

        self.logger = ProductionLogger.get("Discord")

        self.scheduler = Scheduler()

        # Owns the admin API's background task for this instance's
        # entire lifetime (see _start_admin_api()/_stop_admin_api()
        # below) -- deliberately an instance attribute, not a
        # fire-and-forget asyncio.create_task() call with the return
        # value discarded, which risks the task being garbage
        # collected mid-run since nothing else would hold a
        # reference to it. None until/unless _start_admin_api()
        # actually starts it (ENABLE_ADMIN_API=false leaves this
        # None forever).
        self.admin_api_task: asyncio.Task | None = None

        self._ai_cooldowns: dict[int, float] = {}

        # on_ready() is not guaranteed to fire only once per process
        # (Discord's own documented behavior: a full reconnect can
        # trigger it again, not just on_resumed()). load_commands()
        # registers every command onto self.bot.tree, and re-running
        # it would hit CommandAlreadyRegistered for every single
        # command on a second pass (verified directly against
        # discord.py: re-registering an existing name/Group raises,
        # it does not silently duplicate) -- harmless but noisy. This
        # flag makes load_commands() a no-op after the first
        # successful pass, whichever on_ready() call that ends up
        # being.
        self._commands_loaded = False

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
        author_name: str | None = None,
        is_moderator: bool = False,
    ) -> str:
        """Generates Julie's AI reply, applying cooldown and real game
        state context.

        Shared by the mention/DM handler and the /chat command, so both
        entry points behave identically rather than drifting apart.

        `is_moderator` is decided by the caller (see production/
        authorization.py's is_trusted_moderator(), applied to the real
        discord.Member/discord.User this method never sees itself) --
        it only ever changes how much architectural framing the
        KNOWLEDGE_SUMMARY guidance uses (see
        production/knowledge_summary.py and
        format_knowledge_summary_guidance()'s own docstring); every
        other code path in this method ignores it completely.

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

        engine = self.scheduler.engine
        house_status = engine.watcher.house_status.current
        competition = engine.watcher.competition.current

        official_state = format_official_state(engine.knowledge)
        game_state = format_game_state(house_status, competition)
        knowledge = format_learned_knowledge(engine.knowledge.active_items())
        memory = format_long_term_memory(
            engine.memory.recall(channel_id, user_text)
        )

        # Best-effort: never mutates the archive, KnowledgeStore,
        # HouseStatus, or CompetitionState. Never raises -- this is
        # additive background context, not a required dependency, so
        # any failure here (the Hamsterwatch monitor never
        # constructed, see
        # ProductionWatcher._register_builtin_monitors(); a test
        # double with no `hamsterwatch` attribute; an unexpected
        # SQLite error) degrades to "no historical context" rather
        # than breaking /chat or a mention/DM reply. Kept inline
        # (not its own method) so it's part of generate_ai_reply()'s
        # existing single call path rather than a second method every
        # caller/test double needs to know about separately.
        hamsterwatch = getattr(engine.watcher, "hamsterwatch", None)
        historical_result = HistoricalContextResult()
        if hamsterwatch is not None:
            try:
                historical_result = retrieve_historical_context(
                    user_text, hamsterwatch.archive
                )
            except Exception:
                self.logger.exception(
                    "Hamsterwatch historical-context retrieval failed; "
                    "continuing without it."
                )
        historical_context = format_historical_context(historical_result)

        # Same best-effort posture as the Hamsterwatch block above:
        # never mutates anything (see database/historical_events.py --
        # every read path used here returns ADMIN_VERIFIED, active
        # records only), never raises. engine.historical_events is
        # constructed unconditionally in ProductionEngine.__init__()
        # (unlike hamsterwatch, which can fail to construct), but the
        # try/except stays anyway -- a store-level error must degrade
        # to "no historical events" rather than break /chat.
        try:
            historical_hoh_result = retrieve_hoh(user_text, engine.historical_events)
        except Exception:
            self.logger.exception(
                "Historical event retrieval failed; continuing without it."
            )
            historical_hoh_result = None
        historical_events = (
            format_historical_events(historical_hoh_result)
            if historical_hoh_result is not None
            else ""
        )

        # Deterministic, no-AI-call check (see production/
        # knowledge_summary.py). The real metadata collection only
        # runs for a genuine broad-summary question -- an ordinary
        # message pays no extra store reads at all.
        knowledge_summary_guidance = ""
        if is_broad_knowledge_query(user_text):
            summary_metadata = collect_summary_metadata(
                knowledge_items=engine.knowledge.active_items(),
                historical_events=engine.historical_events,
                hamsterwatch_archive=(
                    hamsterwatch.archive if hamsterwatch is not None else None
                ),
                house_status=house_status,
                competition=competition,
                memory_store=engine.memory,
                channel_id=channel_id,
            )
            knowledge_summary_guidance = format_knowledge_summary_guidance(
                summary_metadata, is_moderator=is_moderator
            )

        return await generate_julie_response(
            channel_id,
            user_text,
            author_id=user_id,
            author_name=author_name,
            official_state=official_state,
            game_state=game_state,
            knowledge=knowledge,
            memory=memory,
            historical_events=historical_events,
            historical_context=historical_context,
            knowledge_summary_guidance=knowledge_summary_guidance,
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

            await self.sync_commands()

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

            self._start_admin_api()

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
                            author_name=getattr(
                                message.author, "display_name", None
                            )
                            or str(message.author),
                            is_moderator=is_trusted_moderator(message.author),
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

        if self._commands_loaded:
            self.logger.info(
                "Commands already loaded this process; skipping "
                "reload (on_ready() fired again)."
            )
            return

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

        self._commands_loaded = True

    # ==========================================================
    # Command Sync
    # ==========================================================

    async def sync_commands(self) -> None:
        """Pushes the loaded command tree to Discord: guild-scoped
        only, and actively clears any stale global registrations.

        Julie serves exactly one Discord server (see config.py:
        GUILD_ID, LIVE_UPDATES_CHANNEL, and HOUSE_STATUS_CHANNEL are
        all fixed to that one guild) -- global commands provide no
        benefit here, and previously caused every command to appear
        TWICE in that guild's slash-command picker: this method used
        to sync the command set to the guild AND separately sync it
        globally, and Discord shows both a guild-scoped and a
        global-scoped registration side by side in any guild where the
        bot has both. Guild-scoped sync alone is both correct (nothing
        left to duplicate against) and strictly better for a
        single-guild bot: it propagates instantly, instead of up to an
        hour for global commands.

        The global clear runs second, deliberately: it needs the
        tree's global command set still populated so copy_global_to()
        (above) has something to copy from, so global commands can
        only be cleared after the guild copy is already done. Clearing
        the tree's global scope and syncing that empty set is the
        correct, idiomatic discord.py way to un-register whatever
        Discord still has cached globally from every earlier deploy
        (all of which also synced globally) -- not a manual Discord-
        side deletion, and safe/cheap to repeat on every startup (a
        no-op once Discord's global set is already empty).
        """

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
            self.bot.tree.clear_commands(guild=None)
            cleared = await self.bot.tree.sync()
            self.logger.info(
                "Cleared global slash commands (%s remaining).",
                len(cleared),
            )

        except Exception:
            self.logger.exception(
                "Failed clearing global slash commands."
            )

    # ==========================================================
    # Admin API lifecycle
    # ==========================================================

    def _start_admin_api(self) -> None:
        """Starts the admin API as a task owned by self.admin_api_task
        for this instance's entire lifetime -- see shutdown() for the
        matching cancellation. A no-op, leaving admin_api_task None,
        when ENABLE_ADMIN_API is false: the admin API stays completely
        disabled, exactly as before.

        Idempotent: on_ready() is not guaranteed to fire only once
        per process (Discord's own documented behavior -- see the
        note on _commands_loaded above), so a reconnect calling this
        again must never spin up a second task/server on top of one
        that's still alive, which would just fail to bind the
        already-taken port. Three states, handled explicitly:

          - never started (admin_api_task is None): start it.
          - already running (task exists and not done()): leave it
            alone -- do not create a second task or server.
          - previously started but no longer running (task exists
            and done() -- cancelled, crashed, or returned early e.g.
            a missing ADMIN_API_KEY): safe, and desirable, to start a
            fresh one, so a task that died unexpectedly can recover
            on the next reconnect rather than leaving the admin API
            dark for the rest of the process's life. Any exception
            from the finished task was already retrieved and logged
            by _on_admin_api_task_done() when it completed -- nothing
            is hidden by restarting here.
        """

        if not ENABLE_ADMIN_API:
            self.logger.info(
                "Admin API disabled (ENABLE_ADMIN_API=false)."
            )
            return

        if self.admin_api_task is not None:
            if not self.admin_api_task.done():
                self.logger.info(
                    "Admin API already running; not starting a "
                    "second instance."
                )
                return

            self.logger.info(
                "Previous admin API task is no longer running (%s); "
                "starting a new one.",
                self._describe_admin_api_task_outcome(self.admin_api_task),
            )

        self.admin_api_task = asyncio.create_task(
            run_admin_api(self.scheduler.engine)
        )
        self.admin_api_task.add_done_callback(
            self._on_admin_api_task_done
        )
        self.logger.info(
            "Admin API starting (ENABLE_ADMIN_API=true)."
        )

    @staticmethod
    def _describe_admin_api_task_outcome(task: asyncio.Task) -> str:
        """One-line description of a finished task's outcome, purely
        for the restart log message in _start_admin_api() -- never
        raises, and never the sole place an exception is retrieved
        (see _on_admin_api_task_done(), which already called
        task.exception() once; calling it again here is a safe,
        repeatable read of already-stored state, not a second
        retrieval with different effects).
        """

        if task.cancelled():
            return "cancelled"

        exc = task.exception()
        if exc is not None:
            return f"raised {exc!r}"

        return "returned"

    def _on_admin_api_task_done(self, task: asyncio.Task) -> None:
        """Surfaces an unexpected admin API crash instead of losing it
        silently. Without this, an exception raised inside
        run_admin_api() (anything other than a deliberate cancel from
        _stop_admin_api()) would only ever be visible via Python's
        default "Task exception was never retrieved" warning at
        garbage-collection time, if at all -- easy to miss, and gives
        no indication the dashboard's admin API has gone dark while
        the rest of the bot keeps running normally.
        """

        if task.cancelled():
            return

        exc = task.exception()
        if exc is not None:
            self.logger.error(
                "Admin API task terminated unexpectedly.",
                exc_info=exc,
            )

    async def _stop_admin_api(self) -> None:
        """Cancels the admin API task and waits for its own cleanup
        (admin_api/server.py's run_admin_api() closes the listening
        socket in a finally block) to finish -- a no-op if the admin
        API was never started (ENABLE_ADMIN_API=false).
        """

        if self.admin_api_task is None:
            return

        self.admin_api_task.cancel()

        try:
            await self.admin_api_task
        except asyncio.CancelledError:
            pass
        except Exception:
            # Already surfaced by _on_admin_api_task_done(); avoid a
            # second, redundant traceback during shutdown.
            pass

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
        """Gracefully stop the scheduler, admin API, and Discord connection."""

        try:
            # Stop the scheduler loop
            self.scheduler.stop()

            # Stop the admin API, if it was started.
            await self._stop_admin_api()

            # Close the Discord connection
            await self.bot.close()

            self.logger.info("DiscordService shutdown complete.")

        except Exception:
            self.logger.exception("Error during DiscordService.shutdown()")
