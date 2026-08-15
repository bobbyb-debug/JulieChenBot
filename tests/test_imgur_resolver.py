"""Tests for Imgur ID extraction and resolution (production/imgur.py).

Covers turning a Joker's Updates post's linked HTML into real image
URLs via Imgur's official public read API -- see production/imgur.py's
module docstring for the full investigation this is built on. No test
here makes a real network call: urlopen is monkeypatched throughout.
"""

from __future__ import annotations

import json

import production.imgur as imgur_module
from production.imgur import ImgurResolver, extract_imgur_ids
from production.rss import FeedUpdate


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _update(**overrides) -> FeedUpdate:
    base = dict(
        guid="g1",
        title="7:55 AM HGs up and about. (NT) (IMG)",
        description="7:55 AM HGs up and about. (NT) (IMG)",
        link="https://forums.jokersupdates.com/x",
        published="Sat, 15 Aug 2026 10:59:57 -0700",
        image_url="",
    )
    base.update(overrides)
    return FeedUpdate(**base)


# ==========================================================
# 2 & 3. Imgur ID extraction: one embed, multiple embeds
# ==========================================================


def test_extract_single_imgur_embed() -> None:
    html = (
        '<div class="postimage"><blockquote class="imgur-embed-pub" '
        'lang="en" data-id="Q1n6TNW"><a href="//imgur.com/Q1n6TNW">'
        "</a></blockquote><script async src=\"//s.imgur.com/min/embed.js\">"
        "</script></div>"
    )
    assert extract_imgur_ids(html) == ["Q1n6TNW"]


def test_extract_multiple_imgur_embeds() -> None:
    html = (
        '<blockquote class="imgur-embed-pub" lang="en" data-id="0Pn0kFs">'
        '<a href="//imgur.com/0Pn0kFs"></a></blockquote>'
        '<blockquote class="imgur-embed-pub" lang="en" data-id="GGT1ttT">'
        '<a href="//imgur.com/GGT1ttT"></a></blockquote>'
    )
    assert extract_imgur_ids(html) == ["0Pn0kFs", "GGT1ttT"]


# ==========================================================
# 4. Order preservation
# ==========================================================


def test_extract_preserves_source_order() -> None:
    html = "".join(
        f'<blockquote class="imgur-embed-pub" data-id="{image_id}"></blockquote>'
        for image_id in ("ccccc", "aaaaa", "bbbbb")
    )
    assert extract_imgur_ids(html) == ["ccccc", "aaaaa", "bbbbb"]


# ==========================================================
# 5. Duplicate IDs are deduplicated
# ==========================================================


def test_extract_deduplicates_repeated_ids() -> None:
    html = (
        '<blockquote class="imgur-embed-pub" data-id="Q1n6TNW"></blockquote>'
        '<blockquote class="imgur-embed-pub" data-id="Q1n6TNW"></blockquote>'
    )
    assert extract_imgur_ids(html) == ["Q1n6TNW"]


def test_extract_ignores_unrelated_html() -> None:
    html = (
        "<div>Some unrelated content</div>"
        '<blockquote class="other-widget" data-id="notimgur"></blockquote>'
        "<p>More text</p>"
    )
    assert extract_imgur_ids(html) == []


def test_extract_returns_empty_list_when_no_embed_present() -> None:
    assert extract_imgur_ids("<html><body>No images here.</body></html>") == []


# ==========================================================
# 1. (IMG) RSS item with no media URL -- gating before any network call
# ==========================================================


def test_resolver_skips_network_when_update_already_has_image_url(monkeypatch) -> None:
    resolver = ImgurResolver(client_id="test-client-id")
    update = _update(image_url="https://example.test/already-have-one.jpg")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("must not make a network request")

    monkeypatch.setattr(imgur_module, "urlopen", fail_if_called)

    assert resolver.resolve_images_for_update(update) == []


def test_resolver_skips_network_when_no_img_marker(monkeypatch) -> None:
    resolver = ImgurResolver(client_id="test-client-id")
    update = _update(
        title="7:55 AM HGs up and about. (NT)",
        description="7:55 AM HGs up and about. (NT)",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("must not make a network request")

    monkeypatch.setattr(imgur_module, "urlopen", fail_if_called)

    assert resolver.resolve_images_for_update(update) == []


# ==========================================================
# 6, 7, 8. Imgur API: success, error, malformed/missing data
# ==========================================================


def _imgur_api_success(link: str) -> bytes:
    return json.dumps(
        {"data": {"id": "Q1n6TNW", "link": link}, "success": True, "status": 200}
    ).encode("utf-8")


def test_resolve_images_returns_url_on_successful_api_response(monkeypatch) -> None:
    resolver = ImgurResolver(client_id="test-client-id")
    update = _update()

    page_html = (
        '<blockquote class="imgur-embed-pub" data-id="Q1n6TNW"></blockquote>'
    )

    def fake_urlopen(request, timeout=None):
        if "api.imgur.com" in request.full_url:
            assert request.headers.get("Authorization") == "Client-ID test-client-id"
            return FakeResponse(_imgur_api_success("https://i.imgur.com/Q1n6TNW.jpg"))
        return FakeResponse(page_html.encode("utf-8"))

    monkeypatch.setattr(imgur_module, "urlopen", fake_urlopen)

    assert resolver.resolve_images_for_update(update) == [
        "https://i.imgur.com/Q1n6TNW.jpg"
    ]


def test_resolve_images_handles_api_error_response(monkeypatch) -> None:
    resolver = ImgurResolver(client_id="test-client-id")
    update = _update()

    page_html = '<blockquote class="imgur-embed-pub" data-id="deleted1"></blockquote>'

    def fake_urlopen(request, timeout=None):
        if "api.imgur.com" in request.full_url:
            return FakeResponse(
                json.dumps(
                    {"data": {"error": "Image not found"}, "success": False, "status": 404}
                ).encode("utf-8")
            )
        return FakeResponse(page_html.encode("utf-8"))

    monkeypatch.setattr(imgur_module, "urlopen", fake_urlopen)

    assert resolver.resolve_images_for_update(update) == []


def test_resolve_images_handles_malformed_json(monkeypatch) -> None:
    resolver = ImgurResolver(client_id="test-client-id")
    update = _update()

    page_html = '<blockquote class="imgur-embed-pub" data-id="Q1n6TNW"></blockquote>'

    def fake_urlopen(request, timeout=None):
        if "api.imgur.com" in request.full_url:
            return FakeResponse(b"not valid json{{{")
        return FakeResponse(page_html.encode("utf-8"))

    monkeypatch.setattr(imgur_module, "urlopen", fake_urlopen)

    assert resolver.resolve_images_for_update(update) == []


def test_resolve_images_handles_missing_link_field(monkeypatch) -> None:
    resolver = ImgurResolver(client_id="test-client-id")
    update = _update()

    page_html = '<blockquote class="imgur-embed-pub" data-id="Q1n6TNW"></blockquote>'

    def fake_urlopen(request, timeout=None):
        if "api.imgur.com" in request.full_url:
            return FakeResponse(
                json.dumps({"data": {"id": "Q1n6TNW"}, "success": True}).encode("utf-8")
            )
        return FakeResponse(page_html.encode("utf-8"))

    monkeypatch.setattr(imgur_module, "urlopen", fake_urlopen)

    assert resolver.resolve_images_for_update(update) == []


def test_resolve_images_handles_page_fetch_timeout(monkeypatch) -> None:
    resolver = ImgurResolver(client_id="test-client-id")
    update = _update()

    def fake_urlopen(request, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(imgur_module, "urlopen", fake_urlopen)

    assert resolver.resolve_images_for_update(update) == []


# ==========================================================
# 9. IMGUR_CLIENT_ID missing
# ==========================================================


def test_missing_client_id_returns_empty_and_warns_once(monkeypatch) -> None:
    resolver = ImgurResolver(client_id="")
    update = _update()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("must not make a network request without a Client-ID")

    monkeypatch.setattr(imgur_module, "urlopen", fail_if_called)

    warnings = []
    monkeypatch.setattr(
        imgur_module.logger, "warning", lambda *a, **k: warnings.append(a)
    )

    assert resolver.resolve_images_for_update(update) == []
    assert resolver.resolve_images_for_update(_update(guid="g2")) == []
    assert resolver.resolve_images_for_update(_update(guid="g3")) == []

    assert len(warnings) == 1  # logged once, not once per (IMG) post


# ==========================================================
# 10 & 11. Partial/total resolution failure
# ==========================================================


def test_one_image_fails_another_succeeds_returns_only_successful(monkeypatch) -> None:
    resolver = ImgurResolver(client_id="test-client-id")
    update = _update()

    page_html = (
        '<blockquote class="imgur-embed-pub" data-id="goodid1"></blockquote>'
        '<blockquote class="imgur-embed-pub" data-id="badid99"></blockquote>'
    )

    def fake_urlopen(request, timeout=None):
        if "api.imgur.com/3/image/goodid1" in request.full_url:
            return FakeResponse(_imgur_api_success("https://i.imgur.com/goodid1.jpg"))
        if "api.imgur.com/3/image/badid99" in request.full_url:
            return FakeResponse(
                json.dumps({"data": {}, "success": False, "status": 404}).encode("utf-8")
            )
        return FakeResponse(page_html.encode("utf-8"))

    monkeypatch.setattr(imgur_module, "urlopen", fake_urlopen)

    assert resolver.resolve_images_for_update(update) == [
        "https://i.imgur.com/goodid1.jpg"
    ]


def test_all_images_fail_returns_empty_list(monkeypatch) -> None:
    resolver = ImgurResolver(client_id="test-client-id")
    update = _update()

    page_html = '<blockquote class="imgur-embed-pub" data-id="badid99"></blockquote>'

    def fake_urlopen(request, timeout=None):
        if "api.imgur.com" in request.full_url:
            return FakeResponse(
                json.dumps({"data": {}, "success": False, "status": 404}).encode("utf-8")
            )
        return FakeResponse(page_html.encode("utf-8"))

    monkeypatch.setattr(imgur_module, "urlopen", fake_urlopen)

    assert resolver.resolve_images_for_update(update) == []


def test_multiple_images_all_resolve_preserving_order(monkeypatch) -> None:
    resolver = ImgurResolver(client_id="test-client-id")
    update = _update()

    page_html = (
        '<blockquote class="imgur-embed-pub" data-id="0Pn0kFs"></blockquote>'
        '<blockquote class="imgur-embed-pub" data-id="GGT1ttT"></blockquote>'
    )

    def fake_urlopen(request, timeout=None):
        if "api.imgur.com/3/image/0Pn0kFs" in request.full_url:
            return FakeResponse(_imgur_api_success("https://i.imgur.com/0Pn0kFs.jpg"))
        if "api.imgur.com/3/image/GGT1ttT" in request.full_url:
            return FakeResponse(_imgur_api_success("https://i.imgur.com/GGT1ttT.jpg"))
        return FakeResponse(page_html.encode("utf-8"))

    monkeypatch.setattr(imgur_module, "urlopen", fake_urlopen)

    assert resolver.resolve_images_for_update(update) == [
        "https://i.imgur.com/0Pn0kFs.jpg",
        "https://i.imgur.com/GGT1ttT.jpg",
    ]


def test_page_with_no_imgur_embed_returns_empty_list(monkeypatch) -> None:
    """An (IMG)-tagged item whose linked page has no recognizable embed
    (e.g. Joker's changes its markup) must degrade to no images, not
    raise."""

    resolver = ImgurResolver(client_id="test-client-id")
    update = _update()

    def fake_urlopen(request, timeout=None):
        return FakeResponse(b"<html><body>no embed here</body></html>")

    monkeypatch.setattr(imgur_module, "urlopen", fake_urlopen)

    assert resolver.resolve_images_for_update(update) == []
