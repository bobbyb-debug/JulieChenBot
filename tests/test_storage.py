"""Tests for database/storage.py's atomic-write crash safety.

Storage.save() used to write directly to storage.json in "w" mode --
if the process was interrupted mid-write (Railway restart, OOM kill,
crash), the file could be left truncated or corrupt, and the next
load() would silently discard all persisted state (RSS watermark,
seen_guids, recap buffer, house-image hash) and fall back to defaults.

save() now writes to a temp file in the same directory, flushes and
fsyncs it, then swaps it in with os.replace() -- atomic on both POSIX
and Windows. These tests protect that behavior directly: the existing
file must never be touched until the new content is fully durable on
disk, and a failure at any point before the swap must leave the prior
file completely intact.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from database.storage import Storage


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    return Storage()


def _other_files(tmp_path, storage: Storage) -> list:
    return [p for p in tmp_path.iterdir() if p != storage.FILE]


# ==========================================================
# A. Normal set() still persists and can be reloaded.
# ==========================================================


def test_set_persists_and_reloads(storage):
    storage.set("last_guid", "abc-123")

    reloaded = Storage()
    assert reloaded.last_guid == "abc-123"


def test_property_setters_persist_and_reload(storage):
    storage.last_title = "Some Title"
    storage.last_published = "Mon, 01 Jan 2024 00:00:00 GMT"
    storage.feed_state = "UP"

    reloaded = Storage()
    assert reloaded.last_title == "Some Title"
    assert reloaded.last_published == "Mon, 01 Jan 2024 00:00:00 GMT"
    assert reloaded.feed_state == "UP"


# ==========================================================
# B. The resulting storage.json remains valid JSON.
# ==========================================================


def test_saved_file_is_valid_json(storage):
    storage.set("last_title", "Some Title")

    with storage.FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)  # must not raise

    assert data["last_title"] == "Some Title"


def test_saved_file_preserves_all_existing_keys(storage):
    """Schema/keys must be unchanged by the atomic-write rework."""

    storage.set("last_guid", "guid-1")

    with storage.FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)

    for key in Storage.DEFAULT_DATA:
        assert key in data


# ==========================================================
# C. A failure while writing the temp file must leave the
#    existing storage.json completely unchanged.
# ==========================================================


def test_fsync_failure_leaves_existing_file_untouched(storage, monkeypatch):
    """Simulates a failure partway through writing the temp file
    (after content is written but before it's confirmed durable).
    The real storage.json must never be touched."""

    storage.set("last_guid", "original-guid")
    original_bytes = storage.FILE.read_bytes()

    def failing_fsync(fd):
        raise OSError("simulated disk failure during write")

    monkeypatch.setattr(os, "fsync", failing_fsync)

    with pytest.raises(OSError):
        storage.set("last_guid", "should-never-appear")

    assert storage.FILE.read_bytes() == original_bytes

    reloaded = Storage()
    assert reloaded.last_guid == "original-guid"

    # The failed temp file must not be left behind either.
    assert _other_files(storage.FILE.parent, storage) == []


def test_replace_failure_leaves_existing_file_untouched(storage, monkeypatch):
    """Simulates the swap-in step itself failing (e.g. a transient
    OS-level replace error) -- pure Python-level mocking, no
    OS-specific behavior, so this runs identically on Windows."""

    storage.set("last_guid", "original-guid")
    original_bytes = storage.FILE.read_bytes()

    def failing_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError):
        storage.set("last_guid", "should-never-appear")

    assert storage.FILE.read_bytes() == original_bytes

    reloaded = Storage()
    assert reloaded.last_guid == "original-guid"

    assert _other_files(storage.FILE.parent, storage) == []


def test_write_failure_before_any_content_is_flushed(storage, monkeypatch):
    """Simulates the earliest possible failure -- the write() call
    itself raising before anything is flushed to disk."""

    storage.set("last_guid", "original-guid")
    original_bytes = storage.FILE.read_bytes()

    real_fdopen = os.fdopen

    class FailingFile:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def write(self, data):
            raise OSError("simulated write failure")

    monkeypatch.setattr(os, "fdopen", lambda fd, *a, **kw: FailingFile())

    with pytest.raises(OSError):
        storage.set("last_guid", "should-never-appear")

    assert storage.FILE.read_bytes() == original_bytes

    reloaded = Storage()
    assert reloaded.last_guid == "original-guid"


# ==========================================================
# D. A successful write fully replaces the previous contents.
# ==========================================================


def test_successful_write_fully_replaces_previous_contents(storage):
    """A shorter new value must not leave any trailing bytes from a
    longer previous write -- proves the file is fully swapped, not
    edited/truncated in place."""

    storage.set("last_title", "A" * 500)  # long payload first
    storage.set("last_title", "short")  # much shorter payload

    with storage.FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)  # would fail on trailing garbage

    assert data["last_title"] == "short"

    expected = json.dumps(
        storage._data, indent=4, ensure_ascii=False, sort_keys=True
    )
    assert storage.FILE.read_text(encoding="utf-8") == expected


# ==========================================================
# E. No temporary files remain after a successful write.
# ==========================================================


def test_no_leftover_temp_files_after_successful_writes(storage):
    for i in range(5):
        storage.set("last_guid", f"guid-{i}")

    assert _other_files(storage.FILE.parent, storage) == []


# ==========================================================
# F. Thread-lock behavior remains intact.
# ==========================================================


def test_save_blocks_while_lock_is_held_by_another_thread(storage):
    """Directly exercises the lock's blocking semantics, rather than
    just checking that a lock object is structurally present."""

    storage._lock.acquire()
    completed = threading.Event()

    def try_save():
        storage.set("last_guid", "from-other-thread")
        completed.set()

    writer = threading.Thread(target=try_save)
    writer.start()

    # The other thread must be blocked on the held lock, not finished.
    completed.wait(timeout=0.3)
    assert not completed.is_set()

    storage._lock.release()
    writer.join(timeout=2)

    assert completed.is_set()
    assert storage.last_guid == "from-other-thread"


def test_concurrent_saves_never_corrupt_the_file(storage):
    """Stress test: many threads racing set() calls must never leave
    storage.json as invalid/torn JSON. Without the lock serializing
    save(), concurrent temp-file creation and replacement could
    interleave unpredictably."""

    def writer(n: int) -> None:
        for i in range(20):
            storage.set("statistics", {"writer": n, "i": i})

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with storage.FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)  # must not raise

    assert "statistics" in data
    assert _other_files(storage.FILE.parent, storage) == []
