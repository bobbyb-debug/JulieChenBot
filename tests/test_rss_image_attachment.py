"""Tests for RSS (IMG) live-feed image detection and how it reaches
the production event pipeline.

Investigation confirmed (against the live Joker's Updates RSS feed,
the individual forum thread page an item's <link> points to, and Quick
View -- see production/rss.py's _extract_image_url() docstring for the
full account) that Joker's Updates does not expose a direct,
downloadable image URL anywhere for an (IMG)-tagged item: it embeds
the picture via an Imgur client-side widget (a <blockquote
class="imgur-embed-pub" data-id="...">), which never appears as an
<img> tag, RSS enclosure, or media element in the feed or the linked
page's server-rendered HTML. These tests therefore cover two things
deliberately: (a) that a genuine direct image URL -- were the feed
ever to provide one via a standard mechanism -- is correctly detected
and carried through, and (b) that today's actual Imgur-embed-only
reality, and the presence of "(IMG)" in the text alone, never causes
Julie to invent or guess a URL.
"""

from __future__ import annotations

from production.engine import ProductionEngine
from production.rss import FeedUpdate, _extract_image_url


def _entry(**overrides) -> dict:
    """A plain dict is sufficient here -- _extract_image_url() only
    ever calls entry.get(key), never attribute access, exactly
    matching the subset of feedparser's FeedParserDict interface it
    depends on."""

    base = {"description": ""}
    base.update(overrides)
    return base


# ==========================================================
# 1. A genuine, directly-provided image URL is detected
# ==========================================================


def test_extracts_media_content_url() -> None:
    entry = _entry(media_content=[{"url": "https://example.test/photo.jpg"}])
    assert _extract_image_url(entry, "") == "https://example.test/photo.jpg"


def test_extracts_media_thumbnail_url() -> None:
    entry = _entry(media_thumbnail=[{"url": "https://example.test/thumb.jpg"}])
    assert _extract_image_url(entry, "") == "https://example.test/thumb.jpg"


def test_extracts_image_enclosure() -> None:
    entry = _entry(
        enclosures=[{"href": "https://example.test/attached.png", "type": "image/png"}]
    )
    assert _extract_image_url(entry, "") == "https://example.test/attached.png"


def test_ignores_non_image_enclosure() -> None:
    entry = _entry(
        enclosures=[{"href": "https://example.test/clip.mp3", "type": "audio/mpeg"}]
    )
    assert _extract_image_url(entry, "") == ""


def test_extracts_literal_img_src_from_description() -> None:
    entry = _entry()
    description = 'Some text <img src="https://example.test/inline.jpg"> more text'
    assert _extract_image_url(entry, description) == "https://example.test/inline.jpg"


# ==========================================================
# 2 & 3. No image, or (IMG) with only an Imgur embed reference,
# must never cause a URL to be invented
# ==========================================================


def test_plain_text_item_has_no_image_url() -> None:
    entry = _entry()
    description = "Lala out of bed, wakes Melody. (NT)"
    assert _extract_image_url(entry, description) == ""


def test_img_marker_alone_does_not_produce_an_image_url() -> None:
    """The literal string '(IMG)' appearing in the text must never be
    treated as proof an image URL exists."""

    entry = _entry()
    description = (
        "7:02 AM Lala out of her Pod BR bed, wakes Melody, & out of Pod "
        "BR off cam. (NT) (IMG)"
    )
    assert _extract_image_url(entry, description) == ""


def test_imgur_embed_widget_is_not_resolved_into_a_url() -> None:
    """The actual current real-world shape: Joker's Updates embeds the
    image via an Imgur blockquote widget, not an <img> tag or media
    element. This must be recognized as "no usable image URL" -- not
    guessed at or resolved by contacting Imgur."""

    entry = _entry()
    description = (
        '<blockquote class="imgur-embed-pub" lang="en" data-id="cyitq5t">'
        '<a href="//imgur.com/cyitq5t"></a></blockquote>'
        '<script async src="//s.imgur.com/min/embed.js"></script>'
    )
    assert _extract_image_url(entry, description) == ""


def test_missing_get_method_is_handled_gracefully() -> None:
    """Defensive: an entry-like object without .get() (shouldn't occur
    with real feedparser entries, but must not crash extraction)."""

    class NoGet:
        pass

    assert _extract_image_url(NoGet(), "no image here") == ""


# ==========================================================
# FeedUpdate default and full entries()/latest() pipeline
# ==========================================================


def test_feed_update_image_url_defaults_to_empty_string() -> None:
    update = FeedUpdate(
        guid="g", title="t", description="d", link="l", published="p"
    )
    assert update.image_url == ""


_SAMPLE_RSS_WITH_MEDIA = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel>
  <item>
    <guid>guid-img-1</guid>
    <title>07:14 AM PST - Melody in her costume. (NT) (IMG)</title>
    <description>07:14 AM PST - Melody in her costume. (NT) (IMG)</description>
    <link>http://x/img-1</link>
    <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
    <media:content url="https://example.test/melody.jpg" />
  </item>
</channel></rss>"""

_SAMPLE_RSS_WITHOUT_IMAGE = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <guid>guid-plain-1</guid>
    <title>Test Update</title>
    <description>desc</description>
    <link>http://x/1</link>
    <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


class FakeStorage:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    @property
    def last_guid(self):
        return self.data.get("last_guid", "")

    @last_guid.setter
    def last_guid(self, value):
        self.data["last_guid"] = value

    @property
    def last_title(self):
        return self.data.get("last_title", "")

    @last_title.setter
    def last_title(self, value):
        self.data["last_title"] = value

    @property
    def last_published(self):
        return self.data.get("last_published", "")

    @last_published.setter
    def last_published(self, value):
        self.data["last_published"] = value


def test_entries_carries_media_content_url_through_the_real_pipeline(monkeypatch):
    import production.rss as rss_module

    class FakeResponse:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        rss_module,
        "urlopen",
        lambda request, timeout=None: FakeResponse(_SAMPLE_RSS_WITH_MEDIA),
    )

    entries = rss_module.JokersRSS(storage=FakeStorage()).entries()

    assert len(entries) == 1
    assert entries[0].image_url == "https://example.test/melody.jpg"
    # The existing text fields must be completely unaffected.
    assert "(IMG)" in entries[0].title


def test_entries_without_image_data_leaves_image_url_empty(monkeypatch):
    import production.rss as rss_module

    class FakeResponse:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        rss_module,
        "urlopen",
        lambda request, timeout=None: FakeResponse(_SAMPLE_RSS_WITHOUT_IMAGE),
    )

    entries = rss_module.JokersRSS(storage=FakeStorage()).entries()

    assert len(entries) == 1
    assert entries[0].image_url == ""


# ==========================================================
# 4. image_url is carried into ProductionEvent metadata
# ==========================================================


def test_rss_event_carries_image_url_into_metadata():
    update = FeedUpdate(
        guid="g",
        title="Melody in costume (IMG)",
        description="Melody in costume (IMG)",
        link="https://forums.jokersupdates.com/x",
        published="Mon, 01 Jan 2024 00:00:00 GMT",
        image_url="https://example.test/melody.jpg",
    )

    event = ProductionEngine._rss_event(update)

    assert event.metadata["image_url"] == "https://example.test/melody.jpg"
    # Existing metadata/text fields must be completely unaffected.
    assert event.metadata["link"] == "https://forums.jokersupdates.com/x"
    assert event.detail == "Melody in costume (IMG)"


def test_rss_event_without_image_carries_empty_image_url():
    update = FeedUpdate(
        guid="g",
        title="Plain update",
        description="Plain update",
        link="https://forums.jokersupdates.com/x",
        published="Mon, 01 Jan 2024 00:00:00 GMT",
    )

    event = ProductionEngine._rss_event(update)

    assert event.metadata["image_url"] == ""
