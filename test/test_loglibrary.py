"""Managed originals and metadata survive reload independently of sources."""

import json
from unittest.mock import patch

import pytest

from gweatherrouting.core.loglibrary import LogLibrary


def test_import_preserves_bytes_and_metadata_across_restart(tmp_path):
    source = tmp_path / "sailing.EBL"
    original = b"\x1b\x00\xff\r\nraw bytes"
    source.write_bytes(original)
    library = LogLibrary(tmp_path / "library")
    entry = library.import_path(source)
    assert entry["name"] == "sailing.EBL"
    assert entry["vessel"] == entry["notes"] == ""
    managed = library.path(entry["id"])
    assert managed.read_bytes() == original
    library.update(entry["id"], vessel="My vessel", notes="Race 1\nNight watch ⚓")
    source.unlink()
    reopened = LogLibrary(library.directory)
    assert reopened.path(entry["id"]).read_bytes() == original
    assert reopened.entries()[0]["vessel"] == "My vessel"
    assert reopened.entries()[0]["notes"] == "Race 1\nNight watch ⚓"
    assert reopened.import_path(managed)["id"] == entry["id"]
    assert len(reopened.entries()) == 1


def test_same_basename_does_not_overwrite_and_remove_never_deletes_source(tmp_path):
    library = LogLibrary(tmp_path / "library")
    first = tmp_path / "trip.log"
    first.write_bytes(b"first")
    a = library.import_path(first)
    first.write_bytes(b"second")
    b = library.import_path(first)
    assert library.path(a["id"]).read_bytes() == b"first"
    assert library.path(b["id"]).read_bytes() == b"second"
    managed = library.path(a["id"])
    library.remove(a["id"])
    assert not managed.exists()
    assert first.read_bytes() == b"second"
    assert [e["id"] for e in LogLibrary(library.directory).entries()] == [b["id"]]


def test_failed_index_commit_does_not_change_metadata_or_remove_original(tmp_path):
    source = tmp_path / "trip.log"
    source.write_bytes(b"original")
    library = LogLibrary(tmp_path / "library")
    item = library.import_path(source)
    previous_index = library.index.read_bytes()
    other_source = tmp_path / "trip-copy.log"
    other_source.write_bytes(b"another original")
    with patch(
        "gweatherrouting.core.loglibrary.os.replace", side_effect=OSError("disk full")
    ):
        with pytest.raises(OSError):
            library.update(item["id"], vessel="Not saved")
        with pytest.raises(OSError):
            library.remove(item["id"])
        with pytest.raises(OSError):
            library.import_path(other_source)
    assert library.index.read_bytes() == previous_index
    assert library.entries()[0]["vessel"] == ""
    assert library.path(item["id"]).read_bytes() == b"original"
    assert len(list(library.originals.iterdir())) == 1


def test_rejects_unsupported_sources_and_immutable_metadata(tmp_path):
    source = tmp_path / "trip.log"
    source.write_bytes(b"original")
    library = LogLibrary(tmp_path / "library")
    item = library.import_path(source)
    with pytest.raises(ValueError):
        library.update(item["id"], file="../../outside.log")
    with pytest.raises(ValueError):
        library.import_path(tmp_path)
    with pytest.raises(ValueError):
        library.import_path(tmp_path / "missing.ebl")


def test_manifest_path_traversal_is_not_followed(tmp_path):
    tmp_path.joinpath("index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "logs": [{"id": "bad", "file": "../../outside.log"}],
            }
        )
    )
    library = LogLibrary(tmp_path)
    with pytest.raises(ValueError, match="filename"):
        library.path("bad")


def test_copy_progress_is_chunked_and_managed_reopen_reports_completion(tmp_path):
    source = tmp_path / "large.ebl"
    source.write_bytes(b"x" * (3 * 65536 + 17))
    library = LogLibrary(tmp_path / "library")
    updates = []
    item = library.import_path(source, progress=lambda *args: updates.append(args))
    total = source.stat().st_size
    assert updates == [(0, total)] + [
        (min(chunk * 65536, total), total) for chunk in range(1, 5)
    ]
    assert library.path(item["id"]).read_bytes() == source.read_bytes()
    updates.clear()
    assert (
        library.import_path(
            library.path(item["id"]), progress=lambda *args: updates.append(args)
        )["id"]
        == item["id"]
    )
    assert updates == [(0, total), (total, total)]


def test_copy_does_not_lock_out_library_readers(tmp_path):
    from threading import Event, Thread

    source = tmp_path / "trip.log"
    source.write_bytes(b"original")
    library = LogLibrary(tmp_path / "library")
    read_done = Event()
    readers = []

    def progress(done, total):
        if readers:
            return

        def inspect():
            library.entries()
            read_done.set()

        reader = Thread(target=inspect, daemon=True)
        readers.append(reader)
        reader.start()
        assert read_done.wait(2), "Copy must not hold the metadata lock"

    library.import_path(source, progress=progress)
    readers[0].join(2)


def test_progress_callback_failure_cleans_up_partial_copy(tmp_path):
    source = tmp_path / "trip.log"
    source.write_bytes(b"original")
    library = LogLibrary(tmp_path / "library")

    def fail(done, total):
        if done:
            raise OSError("callback failed")

    with pytest.raises(OSError, match="callback failed"):
        library.import_path(source, progress=fail)
    assert not library.entries()
    assert not list(library.originals.iterdir())
    assert source.read_bytes() == b"original"


def test_ebl_stack_streams_numeric_order_to_one_atomic_original(tmp_path, monkeypatch):
    from pathlib import Path
    from gweatherrouting.core import loglibrary

    directory = tmp_path / "EBL000031"
    directory.mkdir()
    sources = [directory / f"000031_{suffix}.ebl" for suffix in (74, 9, 100)]
    contents = [bytes([suffix]) * (65536 + 17) for suffix in (74, 9, 100)]
    for path, content in zip(sources, contents):
        path.write_bytes(content)
        path.chmod(0o444)
    library = LogLibrary(tmp_path / "library")
    updates, published = [], []
    replace = loglibrary.os.replace

    def publish(source, target):
        if Path(target).suffix == ".ebl":
            published.append((Path(source), Path(target)))
        replace(source, target)

    def progress(done, total):
        # Only the temporary file is visible until the entire stream is closed.
        assert not list(library.originals.glob("*.ebl"))
        assert not library.entries()
        updates.append((done, total))

    monkeypatch.setattr(loglibrary.os, "replace", publish)
    items = library.import_paths(sources, progress=progress)
    assert len(items) == len(library.entries()) == len(published) == 1
    managed = library.path(items[0]["id"])
    assert managed.read_bytes() == contents[1] + contents[0] + contents[2]
    assert published[0][0].parent == managed.parent
    assert published[0][1] == managed
    assert list(library.originals.iterdir()) == [managed]
    total = sum(map(len, contents))
    assert updates[0] == (0, total)
    assert updates[-1] == (total, total)
    assert all(0 < b[0] - a[0] <= 65536 for a, b in zip(updates, updates[1:]))
    assert [path.read_bytes() for path in sources] == contents


def test_ebl_groups_never_cross_directories_ids_or_text_and_keep_import_order(tmp_path):
    library = LogLibrary(tmp_path / "library")

    def source(parent, name, content):
        directory = tmp_path / parent
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_bytes(content)
        return path

    text = source("a", "000031_001.log", b"text")
    a2 = source("a", "000031_002.ebl", b"a2")
    a1 = source("a", "000031_001.EBL", b"a1")
    b1 = source("b", "000031_001.ebl", b"b1")
    b2 = source("b", "000031_002.ebl", b"b2")
    c2 = source("a", "000032_002.ebl", b"c2")
    c1 = source("a", "000032_001.ebl", b"c1")
    single = source("a", "000033_001.ebl", b"single")
    other = source("a", "race.ebl", b"other")
    paths = [text, a2, b2, c2, other, a1, b1, c1, single, a1]
    before = {path: path.read_bytes() for path in paths}
    items = library.import_paths(paths)
    assert [library.path(item["id"]).read_bytes() for item in items] == [
        b"text",
        b"a1a2",
        b"b1b2",
        b"c1c2",
        b"other",
        b"single",
    ]
    assert {path: path.read_bytes() for path in paths} == before
    assert items[0]["name"] == text.name
    assert items[-1]["name"] == single.name
    assert items[-2]["name"] == other.name


def test_successful_ebl_stack_persists_decoded_log_not_transient_model(tmp_path):
    from gweatherrouting.core.nmeasession import read_nmea_log
    from test.test_ebllog import VERSION, frame, timestamp

    directory = tmp_path / "EBL000031"
    directory.mkdir()
    paths = [directory / f"000031_{index:03}.ebl" for index in (1, 2)]
    payload = b"\x00" + (400).to_bytes(2, "little") + b"\xff" * 5
    raw = timestamp(1704153600) + VERSION + frame(128259, payload)
    paths[0].write_bytes(raw[: len(raw) // 2])
    paths[1].write_bytes(raw[len(raw) // 2 :])
    sources = [path.read_bytes() for path in paths]
    library = LogLibrary(tmp_path / "library")
    (item,) = library.import_paths(paths)
    assert item["_decoded_log"]
    managed = library.path(item["id"])
    assert managed.suffix == ".log"
    assert not list(library.originals.glob("*.ebl"))
    assert [path.read_bytes() for path in paths] == sources
    assert read_nmea_log(managed).entries
    reopened = LogLibrary(library.directory)
    reopened_item = reopened.entries()[0]
    assert "_decoded_log" not in reopened_item
    json.loads(reopened.index.read_text())


def test_saved_ripple_edit_keeps_sentence_timestamps_and_managed_source_safe(tmp_path):
    from gweatherrouting.core.nmeasession import NMEASession, read_nmea_log
    from gweatherrouting.core.utils import EventDispatcher

    source = tmp_path / "race.log"
    original = "\n".join(
        (
            "$GPRMC,120000,A,4807.038,N,01131.000,E,7,0,010126,,",
            "$GPRMC,120010,A,4807.038,N,01131.000,E,7,0,010126,,",
            "$GPRMC,120020,A,4807.038,N,01131.000,E,7,0,010126,,",
        )
    )
    source.write_text(original)
    library = LogLibrary(tmp_path / "library")
    item = library.import_path(source)
    managed = library.path(item["id"])
    before_save = managed.read_bytes()
    log = read_nmea_log(managed)
    session = NMEASession(EventDispatcher(), tmp_path / "recordings")
    try:
        session.finish_load(session.begin_load(), log)
        assert session.delete_log_range(5, 15)
        assert managed.read_bytes() == before_save
        assert source.read_text() == original
        library.save_log(session.log)
        session.mark_log_saved()

        reopened = read_nmea_log(managed)
        assert [offset for offset, _ in reopened.entries] == [0, 10]
        assert [
            packet.data.timestamp.strftime("%H%M%S") for _, packet in reopened.entries
        ] == [
            "120000",
            "120020",
        ]
        assert source.read_text() == original
        assert not session.log_edits_dirty
    finally:
        session.close()


@pytest.mark.parametrize("failure", ["copy", "publish", "index"])
def test_failed_stack_import_removes_temporary_and_managed_files(
    tmp_path, monkeypatch, failure
):
    from pathlib import Path
    from gweatherrouting.core import loglibrary

    paths = [tmp_path / f"000031_{index}.ebl" for index in (1, 2)]
    for path in paths:
        path.write_bytes(b"original" * 10000)
    library = LogLibrary(tmp_path / "library")
    replace = loglibrary.os.replace

    def publish(source, target):
        if (failure == "publish" and Path(target).suffix == ".ebl") or (
            failure == "index" and Path(target) == library.index
        ):
            raise OSError("disk full")
        replace(source, target)

    def progress(done, total):
        if failure == "copy" and done > paths[0].stat().st_size:
            raise OSError("disk full")

    monkeypatch.setattr(loglibrary.os, "replace", publish)
    with pytest.raises(OSError, match="disk full"):
        library.import_paths(paths, progress=progress)
    assert not library.entries()
    assert not library.index.exists()
    assert not list(library.originals.iterdir())
    assert all(path.read_bytes() == b"original" * 10000 for path in paths)
