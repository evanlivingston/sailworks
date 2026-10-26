"""The import worker accepts raw-heavy EBL and exposes replay failures."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from gweatherrouting.core import ebllog
from gweatherrouting.core.loglibrary import LogLibrary
from gweatherrouting.qt import controller as controller_module
from gweatherrouting.qt.controller import SessionController
from test.test_ebllog import VERSION, frame


class SignalRecorder:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


@pytest.mark.parametrize("entry_limit, succeeds", [(1, True), (0, False)])
def test_worker_decodes_raw_heavy_ebl_and_updates_library(
    tmp_path, monkeypatch, entry_limit, succeeds
):
    monkeypatch.setattr(ebllog, "MAX_RAW_FRAMES", 10)
    monkeypatch.setattr(ebllog, "MAX_REPLAY_ENTRIES", entry_limit)
    if succeeds:
        monkeypatch.setattr(
            controller_module,
            "read_nmea_log",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("transient EBL model should avoid reparsing")
            ),
        )
    path = tmp_path / "stitched.ebl"
    speed = b"\x00" + (400).to_bytes(2, "little") + b"\xff" * 5
    raw = VERSION + frame(130000, b"\x00" * 8) * 9 + frame(128259, speed)
    path.write_bytes(raw)
    library = LogLibrary(tmp_path / "library")
    worker = SimpleNamespace(
        closed=False,
        session=SimpleNamespace(streaming=False, begin_load=lambda: 7),
        log_library=library,
        _replay_load_tokens=set(),
        _receive_load_progress=lambda *args: None,
        load_progress=SignalRecorder(),
        load_finished=SignalRecorder(),
    )

    class InlineThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(controller_module, "Thread", InlineThread)
    assert SessionController.load_paths(worker, [path], replay=True)
    assert len(worker.load_finished.calls) == 1
    token, log, error = worker.load_finished.calls[0]
    assert token == 7
    assert worker._replay_load_tokens == {7}
    item = library.entries()[0]
    managed = library.path(item["id"])
    if succeeds:
        assert error is None
        assert log.frame_count == 10
        assert len(log.entries) == 1
        assert "10 raw CAN frames; 1 replay entries" in item["status"]
        # Managed file is the decoded .log (NMEA), not the original EBL
        assert managed.suffix == ".log"
        assert managed.read_bytes().startswith(b"$")
    else:
        assert log is None
        assert "supported replay entry limit exceeded" in str(error)
        assert "Cannot replay" in item["status"]
        # On decode failure, managed file falls back to original EBL
        assert managed.suffix == ".ebl"
        assert managed.read_bytes() == raw
    # Source file is never modified
    assert path.read_bytes() == raw


def test_replay_error_dialog_is_visible_without_verbose_panel():
    app = QApplication.instance() or QApplication([])
    owner = SimpleNamespace(_load_error_dialog=None, parent=lambda: None)
    error = OSError("EBL supported replay entry limit exceeded (1,000,000)")
    SessionController._show_load_error(owner, error)
    dialog = owner._load_error_dialog
    try:
        app.processEvents()
        assert dialog.isVisible()
        assert dialog.informativeText() == str(error)
        assert dialog.detailedText() == str(error)
        assert dialog.textFormat() == Qt.TextFormat.PlainText
    finally:
        dialog.close()


def test_load_completion_reports_only_current_request_errors():
    shown = []
    finished = []
    owner = SimpleNamespace(
        _replay_load_tokens=set(),
        _clear_load_progress=lambda token: None,
        closed=False,
        session=SimpleNamespace(
            _generation=2,
            finish_load=lambda *args: finished.append(args),
        ),
        log_library_changed=SignalRecorder(),
        _show_load_error=shown.append,
    )
    error = OSError("supported replay entry limit exceeded")
    SessionController._finish_load(owner, 1, None, error)
    assert shown == finished == []
    SessionController._finish_load(owner, 2, None, error)
    assert shown == [error]
    assert finished == [(2, None, error)]
