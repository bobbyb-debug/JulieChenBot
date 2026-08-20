"""Tests for RSS catch-up so nothing is missed while Julie is offline."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from production.rss import FeedUpdate, JokersRSS


class FakeStorage:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    @property
    def last_guid(self):
        return self.data.get("last_guid")

    @last_guid.setter
    def last_guid(self, value):
        self.data["last_guid"] = value

    @property
    def last_title(self):
        return self.data.get("last_title")

    @last_title.setter
    def last_title(self, value):
        self.data["last_title"] = value

    @property
    def last_published(self):
        return self.data.get("last_published")

    @last_published.setter
    def last_published(self, value):
        self.data["last_published"] = value


def item(n: int) -> FeedUpdate:
    """Feed item n; higher n is newer."""
    return FeedUpdate(
        guid=f"guid-{n}",
        title=f"Update {n}",
        description="",
        link=f"http://x/{n}",
        published=f"t{n}",
    )


def make_rss(storage, items, max_backfill=15):
    """items are newest-first, as a real feed returns them."""
    rss = JokersRSS(storage=storage, max_backfill=max_backfill)
    rss.entries = lambda: list(items)
    rss.latest = lambda: items[0] if items else None
    return rss


def test_first_run_records_feed_without_announcing():
    storage = FakeStorage()
    rss = make_rss(storage, [item(3), item(2), item(1)])

    assert rss.check_all() == []
    # whole feed recorded so the backlog is never dumped later
    assert set(storage.get("seen_guids")) == {"guid-1", "guid-2", "guid-3"}


def test_catches_up_on_every_missed_item():
    """The real bug: only the newest item used to be announced."""

    storage = FakeStorage()
    rss = make_rss(storage, [item(1)])
    rss.check_all()  # snapshot

    # Julie goes offline; four more updates post.
    rss = make_rss(storage, [item(5), item(4), item(3), item(2), item(1)])

    fresh = rss.check_all()

    assert [u.guid for u in fresh] == [
        "guid-2", "guid-3", "guid-4", "guid-5",
    ]


def test_announces_oldest_first():
    storage = FakeStorage()
    rss = make_rss(storage, [item(1)])
    rss.check_all()

    rss = make_rss(storage, [item(3), item(2), item(1)])
    fresh = rss.check_all()

    assert [u.title for u in fresh] == ["Update 2", "Update 3"]


def test_nothing_new_returns_empty():
    storage = FakeStorage()
    rss = make_rss(storage, [item(2), item(1)])
    rss.check_all()

    rss = make_rss(storage, [item(2), item(1)])
    assert rss.check_all() == []


def test_item_is_never_announced_twice():
    storage = FakeStorage()
    rss = make_rss(storage, [item(1)])
    rss.check_all()

    rss = make_rss(storage, [item(2), item(1)])
    first = rss.check_all()
    assert [u.guid for u in first] == ["guid-2"]

    # same feed again
    rss = make_rss(storage, [item(2), item(1)])
    assert rss.check_all() == []


def test_rewound_last_guid_does_not_cause_repost():
    """Guards the duplicate seen in production.

    Restoring an older data/storage.json rewound last_guid and caused
    an already-announced item to post a second time. The seen ledger
    must prevent that even when last_guid goes backwards.
    """

    storage = FakeStorage()
    rss = make_rss(storage, [item(1)])
    rss.check_all()

    rss = make_rss(storage, [item(2), item(1)])
    assert [u.guid for u in rss.check_all()] == ["guid-2"]

    # simulate a stale storage.json being restored
    storage.last_guid = "guid-1"

    rss = make_rss(storage, [item(2), item(1)])
    assert rss.check_all() == [], "guid-2 must not be announced twice"


def test_backfill_is_capped_and_remainder_marked_seen():
    storage = FakeStorage()
    rss = make_rss(storage, [item(1)])
    rss.check_all()

    # 10 new items arrive but the cap is 3
    feed = [item(n) for n in range(11, 1, -1)]
    rss = make_rss(storage, feed, max_backfill=3)

    fresh = rss.check_all()

    # only the 3 newest are announced, still oldest-first
    assert [u.guid for u in fresh] == ["guid-9", "guid-10", "guid-11"]

    # the skipped older ones must not resurface on the next pass
    rss = make_rss(storage, feed, max_backfill=3)
    assert rss.check_all() == []


def test_seen_ledger_stays_bounded():
    storage = FakeStorage()
    rss = make_rss(storage, [item(0)])
    rss.check_all()

    for batch in range(1, 6):
        feed = [item(n) for n in range(batch * 100, (batch - 1) * 100, -1)]
        rss = make_rss(storage, feed, max_backfill=100)
        rss.check_all()

    assert len(storage.get("seen_guids")) <= JokersRSS.SEEN_LIMIT


def test_empty_feed_is_safe():
    storage = FakeStorage()
    rss = make_rss(storage, [])
    assert rss.check_all() == []
