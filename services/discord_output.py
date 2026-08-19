"""
Julie ChenBot Discord Production Output
=======================================

Routes ProductionEvents to the appropriate Discord channels.
"""

from __future__ import annotations

import asyncio
import io
from urllib.request import Request, urlopen

import discord

from config import (
    HOUSE_STATUS_CHANNEL,
    LIVE_UPDATES_CHANNEL,
    PRODUCTION_CHANNEL,
    PRODUCTION_LOG_CHANNEL,
)
from production.events import EventSeverity, EventType, ProductionEvent
from services.logger import ProductionLogger


# Reused by both image sources this router attaches (House Status
# rotation, RSS (IMG) live-feed items): a conservative cap comfortably
# under Discord's smallest guaranteed attachment limit, and a
# lightweight magic-number sniff so an unexpected non-image response
# (an HTML error page mislabeled as an image, for instance) is never
# handed to Discord as an attachment.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB


def _looks_like_image(data: bytes) -> bool:
    if not data:
        return False
    if data.startswith(b"\xff\xd8\xff"):  # JPEG
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):  # PNG
        return True
    if data.startswith((b"GIF87a", b"GIF89a")):  # GIF
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":  # WEBP
        return True
    return False


def _image_extension(data: bytes) -> str:
    """Picks a filename extension matching the actual sniffed format,
    so an attachment's extension doesn't mismatch its real content."""

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def _hamsterwatch_title(event: ProductionEvent) -> str:
    """Builds the Hamsterwatch embed title from event metadata.

    Surfaces the BB day (or the count of recaps, for a multi-day
    catch-up) directly in the title so it's visible without opening
    the embed body.

    metadata["is_new"] (see production/hamsterwatch.py
    HamsterwatchMonitor._build_event()) distinguishes a section's
    first appearance ("HAMSTERWATCH UPDATE") from a later substantial
    edit to a section already archived ("HAMSTERWATCH UPDATED") --
    otherwise identical-looking posts for the same Day N heading are
    what made a legitimate re-announcement read as a duplicate.
    Defaults to True (the "UPDATE"/new wording) for any event that
    predates this field, matching how every other metadata addition
    in this codebase stays backward-compatible with already-persisted
    events (see production/engine.py _rss_event()'s image_url/
    image_urls precedent).
    """

    count = event.metadata.get("count", 1)
    bb_day = event.metadata.get("bb_day")
    is_new = event.metadata.get("is_new", True)
    label = "HAMSTERWATCH UPDATE" if is_new else "HAMSTERWATCH UPDATED"

    if count and count > 1:
        recaps = "new recaps" if is_new else "updated recaps"
        return f"🐹 {label} — {count} {recaps}"
    if bb_day:
        return f"🐹 {label} — Day {bb_day}"
    return f"🐹 {label}"


class DiscordOutputRouter:
    """Publishes ProductionEvents to configured Discord channels."""

    _CHANNEL_NAMES = {
        "live_updates": "live-updates",
        "house_status": "house-status",
        "production": "production",
        "production_log": "production-log",
    }

    def __init__(self, bot) -> None:
        self.bot = bot
        self.logger = ProductionLogger.get("DiscordOutput")

    async def publish(self, event: ProductionEvent) -> None:
        """Routes and publishes one production event.

        Delivery is tracked per destination on the event itself
        (event.delivered_to), not just as an overall success/failure
        for the whole call. This matters because engine.announce()
        retries a failed publish() by requeuing this exact same
        ProductionEvent instance (see production/engine.py) -- so a
        destination that already succeeded must never be sent to
        again on retry, and a destination that failed (whether
        because it couldn't be resolved or because channel.send()
        itself raised) must remain eligible for the next attempt.

        Each destination is fully isolated from the others: one
        destination's resolution failure or send failure never
        prevents another destination in the same call from being
        attempted, and never causes an already-delivered destination
        to be resent.
        """
        destinations = self._destinations(event)
        if not destinations:
            self.logger.warning(
                "No Discord destination configured for event: %s",
                event.event_type.value,
            )
            return

        failed_destinations: list[str] = []
        seen_channel_ids: set[int] = set()

        for channel_id, channel_name in destinations:
            if channel_name in event.delivered_to:
                # Already delivered on a previous attempt -- never resend.
                continue

            channel = await self._resolve_channel(channel_id, channel_name)
            if channel is None:
                self.logger.warning("Discord channel unavailable: #%s", channel_name)
                failed_destinations.append(channel_name)
                continue

            if channel.id in seen_channel_ids:
                # Two logical destinations resolved to the same physical
                # channel within this call; it was already sent to once
                # above, so this one counts as delivered without a
                # second physical send.
                event.delivered_to.add(channel_name)
                continue
            seen_channel_ids.add(channel.id)

            try:
                await self._send(channel, event)
            except Exception as exc:
                # Isolated to this destination: a send failure here
                # must not affect any other destination in this call,
                # and must not cause an already-successful destination
                # to be retried.
                self.logger.warning(
                    "Discord send failed for #%s (%s): %s",
                    channel_name,
                    event.event_type.value,
                    exc,
                )
                failed_destinations.append(channel_name)
                continue

            event.delivered_to.add(channel_name)
            self.logger.info(
                "Published %s to #%s.",
                event.event_type.value,
                getattr(channel, "name", channel_name),
            )

        if failed_destinations:
            raise RuntimeError(
                f"Unable to publish {event.event_type.value} to: "
                f"{', '.join(failed_destinations)}."
            )

    async def _send(self, channel, event: ProductionEvent) -> None:
        """Sends one event, attaching an image where this event type
        has one available.

        Two independent image sources feed into this, deliberately
        kept separate (see production/house_image.py's own module
        docstring for why): the rotating House Status graphic
        (IMAGE_CHANGED, metadata["link"]/["url"]) and an RSS (IMG)
        live-feed item's own image(s) (RSS_UPDATE,
        metadata["image_urls"] -- see production/imgur.py). Both are
        uploaded as real Discord attachments rather than hot-linked
        where possible, so the post stays useful even if the source
        URL later disappears or rotates.

        RSS_UPDATE has its own multi-image path (_send_rss_update())
        since a single Joker's Updates post can embed more than one
        image. IMAGE_CHANGED never has more than one image, and a
        failed download falls back to hot-linking (the embed already
        carries the link via _build_embed()) rather than dropping the
        image entirely -- unchanged from before.
        """

        embed = self._build_embed(event)

        if event.event_type == EventType.RSS_UPDATE:
            await self._send_rss_update(channel, embed, event)
            return

        image_url = self._attachment_image_url(event)

        if not image_url:
            await channel.send(embed=embed)
            return

        payload = await self._download(image_url)

        if payload is None:
            self.logger.warning(
                "Image unavailable for %s; sending text-only.",
                event.event_type.value,
            )
            await channel.send(embed=embed)
            return

        filename = "house_status.png"
        embed.set_image(url=f"attachment://{filename}")

        await channel.send(
            embed=embed,
            file=discord.File(io.BytesIO(payload), filename=filename),
        )

    async def _send_rss_update(self, channel, embed, event: ProductionEvent) -> None:
        """Sends one RSS_UPDATE event, attaching every image resolved
        for it (see production/imgur.py) as separate Discord
        attachments.

        Each URL is downloaded and validated independently through
        the same _download()/_looks_like_image() machinery
        IMAGE_CHANGED uses above. One URL failing to download is
        skipped and logged, not treated as fatal for the whole event
        -- the images that did resolve are still posted. A failed
        image download never falls back to hot-linking a third-party,
        unverified URL, same as before this supported more than one
        image. If none resolve, the update posts as plain text,
        exactly like an item that never had an image at all; the text
        update is never lost either way.
        """

        urls = self._attachment_image_urls(event)

        if not urls:
            await channel.send(embed=embed)
            return

        files: list[discord.File] = []
        for index, url in enumerate(urls):
            payload = await self._download(url)
            if payload is None:
                self.logger.warning(
                    "Image unavailable for RSS_UPDATE (%s); skipping this image.",
                    url,
                )
                continue

            filename = (
                f"live_feed_image{_image_extension(payload)}"
                if len(urls) == 1
                else f"live_feed_image_{index + 1}{_image_extension(payload)}"
            )
            files.append(discord.File(io.BytesIO(payload), filename=filename))

        if not files:
            self.logger.warning(
                "Image unavailable for %s; sending text-only.",
                event.event_type.value,
            )
            await channel.send(embed=embed)
            return

        embed.set_image(url=f"attachment://{files[0].filename}")

        if len(files) == 1:
            await channel.send(embed=embed, file=files[0])
        else:
            await channel.send(embed=embed, files=files)

    @staticmethod
    def _attachment_image_url(event: ProductionEvent) -> str | None:
        """Returns the House Status image URL to attempt downloading
        as a Discord attachment for this event, or None if this event
        type has no such image. RSS_UPDATE has its own multi-image
        resolution -- see _attachment_image_urls()/_send_rss_update()."""

        if event.event_type == EventType.IMAGE_CHANGED:
            return event.metadata.get("link") or event.metadata.get("url") or None

        return None

    @staticmethod
    def _attachment_image_urls(event: ProductionEvent) -> list[str]:
        """Returns every image URL to attempt attaching for one
        RSS_UPDATE event, preserving order.

        Prefers metadata["image_urls"] (see production/engine.py
        _rss_event(), production/imgur.py) -- the full list this
        pipeline resolves today. Falls back to the single
        metadata["image_url"] so an event recovered from storage by
        A3's durability from before this list existed still attaches
        its one image exactly as before.
        """

        urls = event.metadata.get("image_urls")
        if isinstance(urls, list) and urls:
            return [url for url in urls if url]

        single = event.metadata.get("image_url")
        return [single] if single else []

    async def _download(
        self, url: str, *, max_bytes: int = _MAX_IMAGE_BYTES
    ) -> bytes | None:
        """Downloads image bytes, returning None on any failure.

        Rejects (returns None for) a response larger than max_bytes --
        read is capped at max_bytes + 1 so a server that lies about or
        omits Content-Length can't still exhaust memory -- and a
        response whose leading bytes don't match a known image
        format's magic number, so an HTML error page or similar
        accidental non-image response is never handed to Discord as
        an attachment.
        """

        def _get() -> bytes | None:
            request = Request(
                url,
                headers={"User-Agent": "JulieChenBot/1.0"},
            )
            with urlopen(request, timeout=20) as response:
                payload = response.read(max_bytes + 1)

            if len(payload) > max_bytes:
                self.logger.warning(
                    "Image at %s exceeds %d byte(s); rejecting.", url, max_bytes
                )
                return None

            if not _looks_like_image(payload):
                self.logger.warning(
                    "Response from %s does not look like a supported "
                    "image format; rejecting.",
                    url,
                )
                return None

            return payload

        try:
            return await asyncio.to_thread(_get)
        except Exception:
            self.logger.warning(
                "Could not download image for attachment: %s", url
            )
            return None

    def _destinations(self, event: ProductionEvent) -> list[tuple[int, str]]:
        destinations: list[tuple[int, str]] = []

        if event.event_type == EventType.IMAGE_CHANGED:
            # #house-status exists for exactly one thing: the actual
            # Joker's Updates House Status image (production/
            # house_image.py IMAGE_CHANGED, emitted only when the
            # image content itself changes -- an unchanged/rotated-
            # filename image never reaches here at all). No other
            # event type belongs in this destination set -- see the
            # branch below, and the routing-regression incident it
            # fixes (structured game-state events were being posted
            # to #house-status alongside the image).
            destinations.extend([
                (HOUSE_STATUS_CHANNEL, self._CHANNEL_NAMES["house_status"]),
                (LIVE_UPDATES_CHANNEL, self._CHANNEL_NAMES["live_updates"]),
            ])
        elif event.event_type in {EventType.RSS_UPDATE, EventType.TIMELINE}:
            destinations.append(
                (LIVE_UPDATES_CHANNEL, self._CHANNEL_NAMES["live_updates"])
            )
        elif event.event_type in {
            EventType.HOUSE_STATUS_CHANGED,
            EventType.HOH_CHANGED,
            EventType.NOMINATIONS_CHANGED,
            EventType.POV_CHANGED,
            EventType.HAVE_NOTS_CHANGED,
            EventType.FEEDS_UP,
            EventType.FEEDS_DOWN,
            EventType.COMPETITION_STARTED,
            EventType.COMPETITION_FINISHED,
            EventType.COMPETITION_CHANGED,
            EventType.COMPETITION_WINNER,
        }:
            # Structured game-state change events (HouseStatusMonitor,
            # CompetitionMonitor) -- these update Julie's internal
            # tracked state and are announced to #live-updates, but
            # they are NOT House Status image events and must never be
            # routed to #house-status (that channel previously also
            # received these, which is exactly the routing regression
            # this branch fixes: e.g. "Head of Household Changed" and
            # "Competition Winner" showing up in #house-status next to
            # unrelated House Status image posts).
            #
            # PRODUCTION_CHANNEL still has no configured/deployed real
            # channel (there has never been a #production channel in
            # the Discord server -- see config.py, which gives
            # LIVE_UPDATES_CHANNEL and HOUSE_STATUS_CHANNEL real
            # hardcoded default IDs but leaves PRODUCTION_CHANNEL
            # unset). Routing these there would make them permanently
            # undeliverable, and because engine.announce() requeues a
            # failed event at the front of the queue and stops for
            # that tick (see production/engine.py), that becomes a
            # permanent head-of-line block on every later event once
            # A3 started persisting that queue across restarts (the
            # original incident this destination avoided) --
            # live-updates is the one real, always-deliverable
            # destination for these, so this can never reintroduce
            # that failure mode.
            destinations.append(
                (LIVE_UPDATES_CHANNEL, self._CHANNEL_NAMES["live_updates"])
            )
        else:
            destinations.append(
                (PRODUCTION_CHANNEL, self._CHANNEL_NAMES["production"])
            )

        if event.severity in {
            EventSeverity.WARNING,
            EventSeverity.IMPORTANT,
            EventSeverity.CRITICAL,
        } and PRODUCTION_LOG_CHANNEL:
            destinations.append(
                (PRODUCTION_LOG_CHANNEL, self._CHANNEL_NAMES["production_log"])
            )

        return destinations

    async def _resolve_channel(self, channel_id: int, channel_name: str):
        if channel_id:
            channel = self.bot.get_channel(channel_id)
            if channel is not None:
                return channel
            try:
                return await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                self.logger.exception("Failed to fetch Discord channel %s.", channel_id)

        return discord.utils.find(
            lambda channel: getattr(channel, "name", "") == channel_name,
            self.bot.get_all_channels(),
        )

    @staticmethod
    def _build_embed(event: ProductionEvent) -> discord.Embed:
        colors = {
            EventSeverity.DEBUG: 0x95A5A6,
            EventSeverity.INFO: 0x3498DB,
            EventSeverity.NOTICE: 0x2ECC71,
            EventSeverity.WARNING: 0xF1C40F,
            EventSeverity.IMPORTANT: 0xE67E22,
            EventSeverity.CRITICAL: 0xE74C3C,
        }

        if event.event_type == EventType.RSS_UPDATE:
            title = "🟦 LIVE FEED UPDATE"
        elif event.event_type == EventType.TIMELINE and event.source == "Hamsterwatch":
            title = _hamsterwatch_title(event)
        elif event.event_type == EventType.IMAGE_CHANGED:
            title = "🏠 HOUSE STATUS UPDATED"
        else:
            title = event.title

        if event.metadata.get("test"):
            title = f"🧪 [TEST] {title}"

        embed = discord.Embed(
            title=title,
            description=event.detail,
            color=colors.get(event.severity, 0x3498DB),
            timestamp=event.created_at,
        )

        link = event.metadata.get("link") or event.metadata.get("url")
        if link:
            label = "Hamsterwatch" if event.source == "Hamsterwatch" else "Joker's Updates"
            embed.add_field(
                name="🔗 Source",
                value=f"[{label}]({link})",
                inline=False,
            )

        if event.event_type == EventType.IMAGE_CHANGED and link:
            embed.set_image(url=link)

        published = event.metadata.get("published")
        if published:
            embed.add_field(name="🕒 Published", value=str(published), inline=False)

        if event.source == "Hamsterwatch" and event.metadata.get("bb_day"):
            embed.add_field(
                name="📅 BB Day",
                value=str(event.metadata["bb_day"]),
                inline=True,
            )

        embed.set_footer(text=f"Julie ChenBot • Source: {event.source}")
        return embed
