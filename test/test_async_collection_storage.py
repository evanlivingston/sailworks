import json
import threading

import pytest

from sailworks.core import storage
from sailworks.core.geo.collection import CollectionStorage, CollectionWithActiveElement
from sailworks.core.geo.track import Track


@pytest.fixture
def collection_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    return CollectionStorage("tracks")


def test_coalesces_and_flushes_latest(collection_storage, tmp_path, monkeypatch):
    original = storage._write_json
    started = threading.Event()
    release = threading.Event()
    writes = []

    def blocked(path, payload):
        writes.append(payload["data"])
        if len(writes) == 1:
            started.set()
            assert release.wait(5)
        original(path, payload)

    monkeypatch.setattr(storage, "_write_json", blocked)
    collection_storage.save_async_data({"value": 1})
    assert started.wait(5)
    collection_storage.save_async_data({"value": 2})
    collection_storage.save_async_data({"value": 3})
    flushed = threading.Event()
    waiter = threading.Thread(
        target=lambda: (collection_storage.flush_async(), flushed.set())
    )
    waiter.start()
    assert not flushed.wait(0.05)
    release.set()
    waiter.join(5)
    assert flushed.is_set()
    assert writes == [{"value": 1}, {"value": 3}]
    async_bytes = (tmp_path / "tracks.json").read_bytes()
    collection_storage.save()
    assert (tmp_path / "tracks.json").read_bytes() == async_bytes


def test_atomic_failure_preserves_old_file(collection_storage, tmp_path, monkeypatch):
    path = tmp_path / "tracks.json"
    old = path.read_bytes()
    monkeypatch.setattr(
        storage.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("failed"))
    )
    with pytest.raises(OSError, match="failed"):
        collection_storage.save()
    assert path.read_bytes() == old
    assert list(tmp_path.iterdir()) == [path]


def test_sync_save_waits_for_async(collection_storage, tmp_path, monkeypatch):
    original = storage._write_json
    started = threading.Event()
    release = threading.Event()

    def blocked(path, payload):
        if payload["data"] == {"value": 1}:
            started.set()
            assert release.wait(5)
        original(path, payload)

    monkeypatch.setattr(storage, "_write_json", blocked)
    collection_storage.save_async_data({"value": 1})
    assert started.wait(5)
    done = threading.Event()
    thread = threading.Thread(
        target=lambda: (setattr(collection_storage, "data", {"value": 2}), done.set())
    )
    thread.start()
    assert not done.wait(0.05)
    release.set()
    thread.join(5)
    assert done.is_set()
    assert json.loads((tmp_path / "tracks.json").read_text())["data"] == {"value": 2}


def test_file_lock_shared_by_storage_instances(
    collection_storage, tmp_path, monkeypatch
):
    other = CollectionStorage("tracks")
    entered = threading.Event()
    release = threading.Event()
    original_replace = storage.os.replace
    calls = []

    def blocked_replace(src, dst):
        calls.append(src)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        original_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", blocked_replace)
    collection_storage.save_async_data({"value": 1})
    assert entered.wait(5)
    done = threading.Event()
    thread = threading.Thread(
        target=lambda: (setattr(other, "data", {"value": 2}), done.set())
    )
    thread.start()
    assert not done.wait(0.05)
    assert len(calls) == 1
    release.set()
    thread.join(5)
    collection_storage.flush_async()
    assert done.is_set()
    assert json.loads((tmp_path / "tracks.json").read_text())["data"] == {"value": 2}


def test_async_failure_is_logged(collection_storage, caplog, monkeypatch):
    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "_write_json", fail)
    collection_storage.save_async_data({"value": 1})
    collection_storage.flush_async()
    assert "disk full" in caplog.text


def test_track_snapshot_does_not_follow_tracked_list_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    collection = CollectionWithActiveElement(Track, "tracks")
    track = Track("boat", [(1.0, 2.0)], collection=collection)
    collection.append_many([track], save=False)
    collection.save_async()
    collection.flush_async()
    assert json.loads((tmp_path / "tracks.json").read_text())["data"]["elements"][0][
        "points"
    ] == [[1.0, 2.0]]


def test_track_snapshot_is_isolated_from_later_edits(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    collection = CollectionWithActiveElement(Track, "tracks")
    track = Track("boat", [(1.0, 2.0)], collection=collection)
    collection.append_many([track], save=False)
    started = threading.Event()
    release = threading.Event()
    original = storage._write_json

    def blocked(path, payload):
        started.set()
        assert release.wait(5)
        original(path, payload)

    monkeypatch.setattr(storage, "_write_json", blocked)
    collection.save_async()
    assert started.wait(5)
    track.points.append((3.0, 4.0))
    release.set()
    collection.flush_async()
    assert json.loads((tmp_path / "tracks.json").read_text())["data"]["elements"][0][
        "points"
    ] == [[1.0, 2.0]]
