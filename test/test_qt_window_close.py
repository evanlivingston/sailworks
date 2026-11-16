"""Close-time prompt for unsaved log edits."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox, QWidget

from sailworks.qt import window as window_module
from sailworks.qt.window import PreviewWindow


class FakeSession:
    def __init__(self, *, source="log", dirty=True):
        self.source = source
        self.log = object() if source == "log" else None
        self.log_edits_dirty = dirty


class FakeController:
    """Stand-in for the Qt controller's log-save surface."""

    def __init__(self, session):
        self.session = session
        self.log_edit_busy = False
        self.log_edit_error = None
        self.error = None
        self.busy_until_timer = False
        self.calls = 0

    def save_log_edits(self):
        self.calls += 1
        if self.error is not None:
            self.log_edit_error = str(self.error)
            return True
        if self.busy_until_timer:
            self.log_edit_busy = True
            QTimer.singleShot(10, self._finish)
            return True
        self.session.log_edits_dirty = False
        return True

    def _finish(self):
        self.log_edit_busy = False
        self.session.log_edits_dirty = False


class FakeMessageBox:
    """Records prompts instead of opening modal dialogs."""

    def __init__(self, answer):
        self.answer = answer
        self.StandardButton = QMessageBox.StandardButton
        self.asked = []
        self.warnings = []

    def question(self, parent, title, body, buttons, default):
        self.asked.append((parent, title, body, buttons, default))
        return self.answer

    def warning(self, parent, title, body):
        self.warnings.append((title, body))


def _widget(qtbot, session, controller):
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.session = session
    parent.controller = controller
    return parent


def _patch(monkeypatch, answer):
    box = FakeMessageBox(answer)
    monkeypatch.setattr(window_module, "QMessageBox", box)
    return box


def test_clean_session_closes_without_prompt(qtbot, monkeypatch):
    session = FakeSession(dirty=False)
    widget = _widget(qtbot, session, FakeController(session))
    box = _patch(monkeypatch, QMessageBox.StandardButton.Cancel)
    assert PreviewWindow._confirm_unsaved_log_edits(widget) is True
    assert box.asked == []


def test_non_log_source_closes_without_prompt(qtbot, monkeypatch):
    session = FakeSession(source="sampler")
    widget = _widget(qtbot, session, FakeController(session))
    box = _patch(monkeypatch, QMessageBox.StandardButton.Cancel)
    assert PreviewWindow._confirm_unsaved_log_edits(widget) is True
    assert box.asked == []


def test_cancel_keeps_the_window_open(qtbot, monkeypatch):
    session = FakeSession()
    controller = FakeController(session)
    widget = _widget(qtbot, session, controller)
    box = _patch(monkeypatch, QMessageBox.StandardButton.Cancel)
    assert PreviewWindow._confirm_unsaved_log_edits(widget) is False
    assert controller.calls == 0
    assert session.log_edits_dirty is True
    title, body = box.asked[0][1], box.asked[0][2]
    assert title == "Unsaved log edits"
    assert "unsaved changes" in body


def test_discard_closes_without_saving(qtbot, monkeypatch):
    session = FakeSession()
    controller = FakeController(session)
    widget = _widget(qtbot, session, controller)
    box = _patch(monkeypatch, QMessageBox.StandardButton.Discard)
    assert PreviewWindow._confirm_unsaved_log_edits(widget) is True
    assert controller.calls == 0
    assert box.warnings == []


def test_save_waits_for_the_worker_to_finish(qtbot, monkeypatch):
    session = FakeSession()
    controller = FakeController(session)
    controller.busy_until_timer = True
    widget = _widget(qtbot, session, controller)
    box = _patch(monkeypatch, QMessageBox.StandardButton.Save)
    assert PreviewWindow._confirm_unsaved_log_edits(widget) is True
    assert controller.calls == 1
    assert controller.log_edit_busy is False
    assert session.log_edits_dirty is False
    assert box.warnings == []


def test_failed_save_keeps_the_window_open_and_warns(qtbot, monkeypatch):
    session = FakeSession()
    controller = FakeController(session)
    controller.error = OSError("disk full")
    widget = _widget(qtbot, session, controller)
    box = _patch(monkeypatch, QMessageBox.StandardButton.Save)
    assert PreviewWindow._confirm_unsaved_log_edits(widget) is False
    assert controller.calls == 1
    assert session.log_edits_dirty is True
    assert [title for title, _body in box.warnings] == ["Unable to save log"]
    assert "disk full" in box.warnings[0][1]
