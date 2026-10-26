"""Offscreen UI and asynchronous import checks without charts or live devices."""

import os
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QMimeData, QObject, QPointF, Qt, QUrl, Signal
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from gweatherrouting.core.loglibrary import LogLibrary
from gweatherrouting.core.nmeasession import NMEASession
from gweatherrouting.core.utils import EventDispatcher
from gweatherrouting.qt.controller import SessionController
from gweatherrouting.qt.loglibrary import LogLibraryDialog, dropped_log_paths


class ImportController(QObject):
    """Exercise production controller methods with no unrelated app startup."""

    changed = Signal()
    log_library_changed = Signal()
    load_finished = Signal(int, object, object)
    load_progress = Signal(int, object)
    log_progress_changed = Signal()
    load_path = SessionController.load_path
    load_paths = SessionController.load_paths
    _finish_load = SessionController._finish_load
    _receive_load_progress = SessionController._receive_load_progress
    _clear_load_progress = SessionController._clear_load_progress
    _changed = SessionController._changed
    remove_library_log = SessionController.remove_library_log

    def __init__(self, directory):
        super().__init__()
        self.closed = False
        self._replay_load_tokens = set()
        self._show_load_error = lambda error: None
        self.log_progress = None
        self.log_library = LogLibrary(directory / "library")
        self.session = NMEASession(EventDispatcher(), directory / "recordings")
        self.session.connect("changed", self._changed)
        self.load_finished.connect(self._finish_load)
        self.load_progress.connect(
            self._receive_load_progress, Qt.ConnectionType.QueuedConnection
        )


@pytest.fixture(scope="session")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture
def controller(app, tmp_path):
    instance = ImportController(tmp_path)
    yield instance
    instance.closed = True
    instance.session.close()


def wait_for_import(app, controller):
    deadline = time.monotonic() + 10
    while controller.session.loading and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert not controller.session.loading, "Import worker did not complete"


def text_log(tmp_path):
    path = tmp_path / "race.log"
    path.write_text("$GPRMC,120000,A,4807.038,N,01131.000,E,7,0,010126,,\n")
    return path


def mime_paths(*paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    return mime


def test_drop_validation_accepts_text_and_ebl_but_not_mixed_remote_or_directories(
    tmp_path,
):
    text = text_log(tmp_path)
    ebl = tmp_path / "raw.EBL"
    ebl.write_bytes(b"raw")
    assert dropped_log_paths(mime_paths(text, ebl)) == [str(text), str(ebl)]
    assert not dropped_log_paths(mime_paths(tmp_path))
    assert not dropped_log_paths(mime_paths(tmp_path / "missing.log"))
    unsupported = tmp_path / "forecast.grb"
    unsupported.touch()
    assert not dropped_log_paths(mime_paths(text, unsupported))
    mime = QMimeData()
    mime.setUrls([QUrl("https://example.org/private.ebl")])
    assert not dropped_log_paths(mime)


def test_dialog_import_edit_reload_open_paused_and_remove(
    app, controller, tmp_path, monkeypatch
):
    source = text_log(tmp_path)
    dialog = LogLibraryDialog(controller)
    try:
        assert set(dialog.actions) == {
            "Import…",
            "Save details",
            "Open",
            "File Info…",
            "Remove…",
        }
        monkeypatch.setattr(
            QFileDialog, "getOpenFileNames", lambda *args: ([str(source)], "")
        )
        dialog.import_logs()
        wait_for_import(app, controller)
        assert dialog.logs.rowCount() == 1
        assert controller.session.log.entries
        assert not controller.session.playing
        dialog.actions["File Info…"].click()
        assert dialog.file_info_dialog.isVisible()
        assert "Device metadata unavailable" in dialog.file_info_dialog.notice.text()
        dialog.file_info_dialog.close()
        controller.session.set_playing(True)
        dialog.actions["File Info…"].click()
        dialog.file_info_dialog.close()
        dialog.actions["Save details"].click()
        saved = LogLibrary(controller.log_library.directory).entries()[0]
        assert saved["vessel"] == ""
        assert saved["notes"] == ""
        managed = controller.log_library.path(saved["id"])
        assert managed.read_bytes() == source.read_bytes()
        source.unlink()
        controller.session.set_playing(True)
        dialog.actions["Open"].click()
        wait_for_import(app, controller)
        assert not controller.session.playing
        controller.session.set_playing(True)
        dialog.logs.doubleClicked.emit(dialog.logs.model().index(0, 0))
        wait_for_import(app, controller)
        assert not controller.session.playing
        assert len(controller.log_library.entries()) == 1
        monkeypatch.setattr(
            QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes
        )
        dialog.actions["Remove…"].click()
        assert dialog.logs.rowCount() == 0
        assert not managed.exists()
        assert controller.session.log is None
        assert controller.session.source == "live"
    finally:
        dialog.close()


def test_window_drop_imports_multiple_originals_with_honest_ebl_status(
    app, controller, tmp_path
):
    from gweatherrouting.qt.window import PreviewWindow

    source = text_log(tmp_path)
    archive = tmp_path / "unsupported.ebl"
    archive.write_bytes(b"unknown EBL variant")
    mime = mime_paths(source, archive)
    event = QDropEvent(
        QPointF(1, 1),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    opened = []
    window = SimpleNamespace(
        controller=controller,
        logs_view="logs",
        nmea_dock="nmea",
        views=SimpleNamespace(setCurrentWidget=opened.append),
        _show_panel=opened.append,
    )
    PreviewWindow.dropEvent(window, event)
    assert event.isAccepted()
    wait_for_import(app, controller)
    entries = controller.log_library.entries()
    assert len(entries) == 2
    assert entries[-1]["name"] == "unsupported.ebl"  # fallback: original stored
    assert "Archive only" in entries[-1]["status"]
    assert any("Partial EBL" in message for message in entries[-1]["diagnostics"])
    assert (
        controller.log_library.path(entries[-1]["id"]).read_bytes()
        == archive.read_bytes()
    )
    assert not controller.session.log.entries
    assert not controller.session.playing
    assert opened == ["logs", "nmea"]


def test_failed_text_import_is_retained_with_error_and_previous_replay_survives(
    app, controller, tmp_path
):
    source = text_log(tmp_path)
    assert controller.load_path(source)
    wait_for_import(app, controller)
    previous = controller.session.log
    bad = tmp_path / "bad.log"
    bad.write_text("not interpreted sentences")
    assert controller.load_path(bad)
    wait_for_import(app, controller)
    assert controller.session.log is previous
    assert "interpreted NMEA" in controller.session.error
    assert controller.log_progress is None
    assert len(controller.log_library.entries()) == 2
    assert "Cannot replay" in controller.log_library.entries()[-1]["status"]


def test_window_drop_stitches_stack_once_and_library_opens_whole_timeline_paused(
    app, controller, tmp_path, monkeypatch
):
    from gweatherrouting.qt import controller as controller_module
    from gweatherrouting.qt.window import PreviewWindow
    from test.test_ebllog import VERSION, frame, timestamp

    directory = tmp_path / "EBL000031"
    directory.mkdir()
    paths, originals = [], []
    payload = b"\x00" + (400).to_bytes(2, "little") + b"\xff" * 5
    for suffix, offset in ((9, 0), (74, 9), (100, 120)):
        path = directory / f"000031_{suffix:03}.ebl"
        raw = timestamp(1704153600 + offset) + VERSION + frame(128259, payload)
        path.write_bytes(raw)
        paths.append(path)
        originals.append(raw)
    decoded = []
    read = controller_module.read_nmea_log

    def decode(path, progress=None):
        decoded.append(path)
        return read(path, progress=progress)

    monkeypatch.setattr(controller_module, "read_nmea_log", decode)
    # QDropEvent borrows the MIME data; retain its Python owner until the drop
    # has been handled instead of passing a temporary with a dangling pointer.
    mime = mime_paths(paths[2], paths[0], paths[1])
    event = QDropEvent(
        QPointF(1, 1),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    opened = []
    window = SimpleNamespace(
        controller=controller,
        logs_view="logs",
        nmea_dock="nmea",
        views=SimpleNamespace(setCurrentWidget=opened.append),
        _show_panel=opened.append,
    )
    PreviewWindow.dropEvent(window, event)
    assert event.isAccepted()
    assert opened == ["logs", "nmea"]
    wait_for_import(app, controller)
    assert not controller.session.error
    items = controller.log_library.entries()
    assert len(items) == 1
    managed = controller.log_library.path(items[0]["id"])
    assert decoded == [managed]
    assert managed.read_bytes() == b"".join(originals)
    assert [path.read_bytes() for path in paths] == originals
    assert [offset for offset, _ in controller.session.log.entries] == [0, 9, 120]
    assert controller.session.log.duration == 120
    assert not controller.session.playing
    dialog = LogLibraryDialog(controller)
    try:
        assert dialog.logs.rowCount() == 1
        dialog.actions["Open"].click()
        wait_for_import(app, controller)
        assert not controller.session.playing
        assert len(controller.log_library.entries()) == 1
        assert decoded == [managed, managed]
        # Core replay remains supported even though the UI is scrub-only.
        controller.session.set_playing(True)
        controller.session.advance(120)
        assert controller.session.cursor == 3
        assert not controller.session.playing
    finally:
        dialog.close()


@pytest.mark.parametrize(
    "first_replay,second_replay",
    [(True, False), (False, True), (True, True), (False, False)],
)
@pytest.mark.parametrize("finish_second_inline", [False, True])
def test_load_paths_interleaves_replay_intent_and_multiple_paths(
    controller,
    tmp_path,
    monkeypatch,
    first_replay,
    second_replay,
    finish_second_inline,
):
    # Only scheduling is mocked: both workers import real files and report via
    # the production signal/slot and real NMEASession completion methods.
    workers = []
    completions = []
    controller.load_finished.disconnect(controller._finish_load)
    controller.load_finished.connect(lambda *args: completions.append(args))
    controller.load_finished.connect(controller._finish_load)

    def deferred_thread(*, target, daemon):
        return SimpleNamespace(start=lambda: workers.append(target))

    monkeypatch.setattr("gweatherrouting.qt.controller.Thread", deferred_thread)
    paths = []
    for index in range(4):
        path = tmp_path / f"interleaved-{index}.log"
        path.write_text(f"$GPRMC,12000{index},A,4807.038,N,01131.000,E,7,0,010126,,\n")
        paths.append(path)
    played = []
    set_playing = controller.session.set_playing

    def record_playing(active):
        played.append((controller.session.log.path, active))
        set_playing(active)

    monkeypatch.setattr(controller.session, "set_playing", record_playing)
    started = []
    pending_states = []

    def start_second_import():
        if started:
            return
        started.append(controller.load_paths(paths[2:], replay=second_replay))
        pending_states.append((controller.session.loading, controller.session.playing))
        if finish_second_inline:
            workers[1]()

    controller.log_library_changed.connect(start_second_import)
    assert controller.load_paths(paths[:2], replay=first_replay)
    assert len(workers) == 1
    workers[0]()
    assert started == [True]
    assert pending_states == [(True, False)]
    assert len(workers) == 2
    if not finish_second_inline:
        assert controller.session.loading
        assert not controller.session.playing
        assert played == []
        # Stale delivery while the next worker is pending must not clear that
        # worker's intent or change loading/playback state.
        controller.load_finished.emit(*completions[0])
        assert controller.session.loading
        assert not controller.session.playing
        workers[1]()

    entries = controller.log_library.entries()
    assert {entry["name"] for entry in entries} == {path.name for path in paths}
    first_entry = next(entry for entry in entries if entry["name"] == paths[1].name)
    assert completions[0][1].path == str(controller.log_library.path(first_entry["id"]))
    last_path = str(controller.log_library.path(entries[-1]["id"]))
    assert entries[-1]["name"] == paths[-1].name
    assert controller.session.log.path == last_path
    assert not controller.session.loading
    assert controller.session.playing is second_replay
    assert played == ([(last_path, True)] if second_replay else [])
    assert not controller._replay_load_tokens


@pytest.mark.parametrize(
    "first_replay,second_replay",
    [(True, False), (False, True), (True, True), (False, False)],
)
@pytest.mark.parametrize("older_finishes_first", [False, True])
def test_overlapping_load_paths_latest_request_wins(
    controller,
    tmp_path,
    monkeypatch,
    first_replay,
    second_replay,
    older_finishes_first,
):
    workers = []
    completions = []
    loaded = []
    changed = []
    played = []
    controller.load_finished.disconnect(controller._finish_load)
    controller.load_finished.connect(lambda *args: completions.append(args))
    controller.load_finished.connect(controller._finish_load)
    controller.session.connect("loaded", loaded.append)
    controller.session.connect("changed", changed.append)

    def deferred_thread(*, target, daemon):
        return SimpleNamespace(start=lambda: workers.append(target))

    monkeypatch.setattr("gweatherrouting.qt.controller.Thread", deferred_thread)
    set_playing = controller.session.set_playing

    def record_playing(active):
        played.append((controller.session.log.path, active))
        set_playing(active)

    monkeypatch.setattr(controller.session, "set_playing", record_playing)
    paths = []
    for index in range(4):
        path = tmp_path / f"overlapping-{index}.log"
        path.write_text(f"$GPRMC,12000{index},A,4807.038,N,01131.000,E,7,0,010126,,\n")
        paths.append(path)

    # Both public requests are accepted before either deferred worker runs.
    assert controller.load_paths(paths[:2], replay=first_replay)
    first_token = controller.session._generation
    assert controller.session.loading
    assert controller.load_paths(paths[2:], replay=second_replay)
    second_token = controller.session._generation
    assert second_token > first_token
    assert len(workers) == 2
    assert not completions
    assert controller.session.loading
    assert not controller.session.playing
    assert controller._replay_load_tokens == {
        token
        for token, replay in (
            (first_token, first_replay),
            (second_token, second_replay),
        )
        if replay
    }

    if older_finishes_first:
        changed_count = len(changed)
        workers[0]()
        assert completions[-1][0] == first_token
        assert controller.session.loading
        assert controller.session.log is None
        assert not controller.session.playing
        assert not loaded
        assert not played
        assert len(changed) == changed_count
        # A stale completion cannot consume the newer request's replay intent.
        assert controller._replay_load_tokens == (
            {second_token} if second_replay else set()
        )

    workers[1]()
    assert completions[-1][0] == second_token
    latest_log = completions[-1][1]
    latest_entry = next(
        entry
        for entry in controller.log_library.entries()
        if entry["name"] == paths[-1].name
    )
    latest_path = str(controller.log_library.path(latest_entry["id"]))
    assert latest_log.path == latest_path
    assert controller.session.log is latest_log
    assert not controller.session.loading
    assert controller.session.playing is second_replay
    assert loaded == [latest_log]
    assert played == ([(latest_path, True)] if second_replay else [])
    assert controller._replay_load_tokens == (
        {first_token} if first_replay and not older_finishes_first else set()
    )
    latest_replay_state = (
        controller.session.cursor,
        controller.session.overview,
        controller.session.replay_history.revision,
        tuple(controller.session.replay_history.samples),
    )

    changed_count = len(changed)
    if not older_finishes_first:
        # The older real worker completes only after the newer selection has
        # loaded (and optionally started replaying).
        workers[0]()
        assert completions[-1][0] == first_token
    else:
        # Repeated stale delivery must also be harmless after the latest load.
        controller.load_finished.emit(*completions[0])

    assert [completion[0] for completion in completions] == (
        [first_token, second_token, first_token]
        if older_finishes_first
        else [second_token, first_token]
    )
    assert completions[-1][1] is not latest_log
    assert all(error is None for _, _, error in completions)
    assert controller.session._generation == second_token
    assert controller.session.log is latest_log
    assert controller.session.source == "log"
    assert not controller.session.loading
    assert not controller.session.error
    assert controller.session.playing is second_replay
    assert loaded == [latest_log]
    assert len(changed) == changed_count
    assert played == ([(latest_path, True)] if second_replay else [])
    assert (
        controller.session.cursor,
        controller.session.overview,
        controller.session.replay_history.revision,
        tuple(controller.session.replay_history.samples),
    ) == latest_replay_state
    assert not controller._replay_load_tokens
    entries = controller.log_library.entries()
    assert len(entries) == len(paths)
    assert {entry["name"] for entry in entries} == {path.name for path in paths}
    for entry in entries:
        assert "1 sentences" in entry["status"]
        assert (
            controller.log_library.path(entry["id"]).read_bytes()
            == (tmp_path / entry["name"]).read_bytes()
        )


def test_worker_progress_is_queued_visible_and_stage_aware(
    app, controller, tmp_path, monkeypatch
):
    from threading import Event, get_ident

    from gweatherrouting.core.nmeasession import read_nmea_log
    from gweatherrouting.qt.loglibrary import LogImportProgress

    source = text_log(tmp_path)
    archive = tmp_path / "archive.ebl"
    archive.write_bytes(b"unsupported EBL archive")
    gui_thread = get_ident()
    copy_ready, decode_ready = Event(), Event()
    release_copy, release_decode = Event(), Event()
    import_path = controller.log_library.import_path
    snapshots = []
    notification_threads = []

    def record_progress():
        notification_threads.append(get_ident())
        snapshots.append(controller.log_progress)

    controller.log_progress_changed.connect(record_progress)

    def copy(path, progress=None):
        assert get_ident() != gui_thread
        progress(32, 64)
        copy_ready.set()
        if not release_copy.wait(5):
            raise TimeoutError("test did not release copy")
        return import_path(path, progress=progress)

    def decode(path, progress=None):
        assert get_ident() != gui_thread
        progress(48, 64)
        decode_ready.set()
        if not release_decode.wait(5):
            raise TimeoutError("test did not release decode")
        return read_nmea_log(path, progress=progress)

    monkeypatch.setattr(controller.log_library, "import_path", copy)
    monkeypatch.setattr("gweatherrouting.qt.controller.read_nmea_log", decode)
    dialog = LogLibraryDialog(controller)
    # The same view is installed in the main-window status bar; both instances
    # must map notifications identically, including when opened mid-import.
    status_progress = LogImportProgress(controller)
    try:
        assert controller.load_paths([source, archive])
        assert copy_ready.wait(5)
        assert controller.log_progress["done"] == 0  # worker has not touched UI
        app.processEvents()
        assert "1/2 · Copying · race.log" == dialog.import_progress.label.text()
        assert dialog.import_progress.bar.value() == 500
        assert not dialog.import_progress.isHidden()
        assert status_progress.label.text() == dialog.import_progress.label.text()
        release_copy.set()
        assert decode_ready.wait(5)
        app.processEvents()
        assert "1/2 · Decoding · race.log" == dialog.import_progress.label.text()
        assert dialog.import_progress.bar.value() == 750
        release_decode.set()
        wait_for_import(app, controller)
        assert {(s["index"], s["phase"]) for s in snapshots if s} >= {
            (1, "copy"),
            (1, "decode"),
            (2, "copy"),
            (2, "decode"),
        }
        assert {get_ident()} == set(notification_threads)
        assert controller.log_progress is None
        assert dialog.import_progress.isHidden()
        assert status_progress.isHidden()
        assert not status_progress.label.text()
        assert status_progress.bar.value() == 0
    finally:
        release_copy.set()
        release_decode.set()
        wait_for_import(app, controller)
        dialog.close()
        status_progress.close()


def test_stale_progress_and_completions_never_clear_newer_progress(
    app, controller, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "gweatherrouting.qt.controller.Thread",
        lambda **kwargs: SimpleNamespace(start=lambda: None),
    )
    source = text_log(tmp_path)
    assert controller.load_path(source)
    older = controller.session._generation
    assert controller.load_paths([source], replay=True)
    latest = controller.session._generation
    progress = dict(
        path=str(source), index=1, count=1, phase="decode", done=50, total=100
    )
    controller.load_progress.emit(latest, progress)
    app.processEvents()
    expected = controller.log_progress
    assert expected["done"] == 50
    controller.load_progress.emit(older, {**progress, "done": 75})
    controller.load_finished.emit(older, None, ValueError("stale failure"))
    app.processEvents()
    assert controller.log_progress == expected
    assert controller._replay_load_tokens == {latest}
    controller.load_finished.emit(latest, None, ValueError("decode failed"))
    assert not controller.session.loading
    assert "decode failed" in controller.session.error
    assert controller.log_progress is None
    controller.load_progress.emit(latest, progress)
    app.processEvents()
    assert controller.log_progress is None
    assert not controller._replay_load_tokens


def test_import_errors_clear_visible_progress(app, controller, tmp_path, monkeypatch):
    source = text_log(tmp_path)
    dialog = LogLibraryDialog(controller)

    def fail_copy(path, progress=None):
        progress(0, 100)
        raise OSError("disk full")

    monkeypatch.setattr(controller.log_library, "import_path", fail_copy)
    try:
        assert controller.load_path(source)
        wait_for_import(app, controller)
        assert "disk full" in controller.session.error
        assert controller.log_progress is None
        assert dialog.import_progress.isHidden()
        assert dialog.actions["Import…"].isEnabled()
    finally:
        dialog.close()


def test_worker_start_failure_resets_progress(controller, tmp_path, monkeypatch):
    def fail_start():
        raise RuntimeError("cannot start worker")

    monkeypatch.setattr(
        "gweatherrouting.qt.controller.Thread",
        lambda **kwargs: SimpleNamespace(start=fail_start),
    )
    assert not controller.load_paths([text_log(tmp_path)], replay=True)
    assert not controller.session.loading
    assert "cannot start worker" in controller.session.error
    assert controller.log_progress is None
    assert not controller._replay_load_tokens
