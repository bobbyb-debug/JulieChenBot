"""
Julie ChenBot Imgur Resolution
===============================

Resolves the Imgur images Joker's Updates embeds into (IMG)-tagged
posts, so DiscordOutputRouter has a real, hotlinkable URL to attach.

Background (see production/rss.py _extract_image_url() for the full
investigation): Joker's Updates never exposes a direct image URL in
its RSS feed, an enclosure, a media element, or an <img> tag anywhere
in a post's server-rendered HTML. It embeds each picture via Imgur's
client-side embed widget instead:

    <blockquote class="imgur-embed-pub" lang="en" data-id="Q1n6TNW">
      <a href="//imgur.com/Q1n6TNW"></a>
    </blockquote>
    <script async src="//s.imgur.com/min/embed.js"></script>

`data-id` is the only thing available anywhere -- an opaque Imgur
image ID that a browser's JavaScript resolves into a real picture at
view time. This module extracts that ID from the linked post's HTML
(the RSS item's own <link>, already available -- no separate page
discovery needed) and resolves it into a real URL through Imgur's
official public read API (https://apidocs.imgur.com/#image), which
Imgur documents as usable anonymously with just a registered app's
Client-ID -- no OAuth, no scraping, no guessed https://i.imgur.com/...
URL shape.

This module does NOT talk to Discord or the Production Engine -- it
only turns "a Joker's Updates post URL" into "zero or more real image
URLs."
"""

from __future__ import annotations

import json
import re
from urllib.request import Request, urlopen

from config import IMGUR_CLIENT_ID
from production.rss import FeedUpdate
from services.logger import ProductionLogger

logger = ProductionLogger.get("Imgur")

_USER_AGENT = "JulieChenBot/1.0"

# Same bound used by every other Julie network call (see
# production/rss.py _FETCH_TIMEOUT) -- both the Joker's page fetch and
# the Imgur API call are bounded so a stalled connection can never
# hang a production cycle indefinitely.
_FETCH_TIMEOUT = 20

# Discord's own hard limit on attachments in a single message. Capping
# here means a pathological post never triggers more Imgur API calls
# than Discord could ever actually display anyway.
_MAX_IMAGES_PER_POST = 10

_IMGUR_API_URL = "https://api.imgur.com/3/image/{image_id}"

# Deliberately narrow: recognizes exactly the embed widget Joker's
# Updates is verified to emit (see module docstring above), not a
# general HTML or Imgur-URL scraper. Matched in two steps -- find each
# <blockquote ...> tag, then check its attributes -- rather than one
# combined regex, so attribute order (class before data-id, or the
# reverse) never matters.
_BLOCKQUOTE_PATTERN = re.compile(r"<blockquote[^>]*>", re.IGNORECASE)
_CLASS_ATTR_PATTERN = re.compile(r"""class=["']([^"']*)["']""", re.IGNORECASE)
_DATA_ID_ATTR_PATTERN = re.compile(r"""data-id=["']([^"']+)["']""", re.IGNORECASE)

# Real Imgur image IDs are short alphanumeric strings. This rejects
# obvious junk without pretending to be a full Imgur ID validator.
_VALID_IMGUR_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{5,10}$")


def extract_imgur_ids(html: str) -> list[str]:
    """Extracts Imgur image IDs embedded in one Joker's Updates post
    page, in the order they appear, with duplicates removed.

    Returns [] if the page has no recognizable Imgur embed at all.
    """

    ids: list[str] = []
    seen: set[str] = set()

    for tag in _BLOCKQUOTE_PATTERN.findall(html or ""):
        class_match = _CLASS_ATTR_PATTERN.search(tag)
        if not class_match or "imgur-embed-pub" not in class_match.group(1).split():
            continue

        data_id_match = _DATA_ID_ATTR_PATTERN.search(tag)
        if not data_id_match:
            continue

        image_id = data_id_match.group(1)
        if not _VALID_IMGUR_ID_PATTERN.match(image_id):
            continue

        if image_id not in seen:
            seen.add(image_id)
            ids.append(image_id)

    return ids


class ImgurResolver:
    """Resolves the Imgur images referenced by Joker's Updates posts.

    Every failure mode (missing Client-ID, unreachable page, HTTP
    error, malformed JSON, an Imgur API error response, a deleted
    image) degrades to returning fewer images -- never raises, and
    never invents a URL. Callers get back exactly the images that
    were actually confirmed to exist.
    """

    def __init__(self, client_id: str | None = None) -> None:
        self.client_id = IMGUR_CLIENT_ID if client_id is None else client_id
        # Set once the missing-Client-ID warning has fired, so a
        # misconfigured deployment logs it once rather than once per
        # (IMG) post for as long as it runs.
        self._warned_missing_client_id = False

    def resolve_images_for_update(self, update: FeedUpdate) -> list[str]:
        """Returns the real image URL(s) for one RSS update, or [] if
        this update has none (or none could be resolved).

        Only does any work at all when the RSS item's own image_url
        is empty (see production/rss.py _extract_image_url()) and its
        text carries Joker's Updates' "(IMG)" marker -- exactly the
        case the RSS feed itself cannot resolve. Every other update
        returns [] immediately, without a network request.
        """

        if update.image_url:
            return []

        text = f"{update.title} {update.description}".upper()
        if "(IMG)" not in text:
            return []

        if not self.client_id:
            if not self._warned_missing_client_id:
                logger.warning(
                    "IMGUR_CLIENT_ID is not configured; (IMG) posts "
                    "will post as text-only until it is set."
                )
                self._warned_missing_client_id = True
            return []

        if not update.link:
            return []

        html = self._fetch_page(update.link)
        if not html:
            return []

        image_ids = extract_imgur_ids(html)[:_MAX_IMAGES_PER_POST]
        if not image_ids:
            return []

        resolved: list[str] = []
        for image_id in image_ids:
            url = self._resolve_image(image_id)
            if url:
                resolved.append(url)
            else:
                logger.warning(
                    "Could not resolve Imgur image %s (post %s).",
                    image_id,
                    update.link,
                )

        return resolved

    def _fetch_page(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urlopen(request, timeout=_FETCH_TIMEOUT) as response:
                raw = response.read()
        except Exception as exc:
            logger.warning("Failed to fetch Joker's Updates post %s: %s", url, exc)
            return ""

        return raw.decode("utf-8", errors="replace")

    def _resolve_image(self, image_id: str) -> str:
        """Calls Imgur's official public image endpoint for one ID.

        https://apidocs.imgur.com/#image documents this endpoint as
        readable anonymously via `Authorization: Client-ID <id>` --
        no OAuth required for a public read. Returns "" on any
        failure; the actual direct URL is read from the API's own
        JSON response (data.link), never constructed here.
        """

        request = Request(
            _IMGUR_API_URL.format(image_id=image_id),
            headers={
                "Authorization": f"Client-ID {self.client_id}",
                "User-Agent": _USER_AGENT,
            },
        )

        try:
            with urlopen(request, timeout=_FETCH_TIMEOUT) as response:
                raw = response.read()
        except Exception as exc:
            logger.warning("Imgur API request failed for %s: %s", image_id, exc)
            return ""

        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "Imgur API returned malformed JSON for %s: %s", image_id, exc
            )
            return ""

        if not isinstance(payload, dict) or not payload.get("success"):
            logger.warning("Imgur API reported failure for %s: %r", image_id, payload)
            return ""

        data = payload.get("data")
        if not isinstance(data, dict):
            return ""

        link = data.get("link")
        if not isinstance(link, str) or not link:
            return ""

        return link
