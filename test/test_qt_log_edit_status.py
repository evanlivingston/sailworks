import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from sailworks.qt.window import PreviewWindow


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _status_widget(*, busy=False, error=None, cursor_text="", connection=""):
    """A bare widget carrying only what the status helper reads."""
    widget = QWidget()
    widget.controller = SimpleNamespace(log_edit_busy=busy, log_edit_error=error)
    widget.cursor_status = SimpleNamespace(text=lambda: cursor_text)
    widget._connection_message = lambda: connection
    return widget


def test_log_edit_status_reports_busy():
    widget = _status_widget(busy=True, error="stale")
    assert PreviewWindow._log_edit_status_message(widget) == "Updating log"


def test_log_edit_status_reports_generic_failure():
    widget = _status_widget(error="disk full")
    message = PreviewWindow._log_edit_status_message(widget)
    assert message.startswith("Log edit failed:")
    assert "disk full" in message


def test_log_edit_status_falls_back_to_connection_message():
    widget = _status_widget(connection="Connected to NMEA")
    assert PreviewWindow._log_edit_status_message(widget) == "Connected to NMEA"


def test_log_edit_status_defers_to_cursor_status():
    widget = _status_widget(cursor_text="48.1 -123.2", connection="Connected")
    assert PreviewWindow._log_edit_status_message(widget) == ""


def test_routing_absence_caption_reports_overlay_failure():
    from sailworks.qt.viewportmap import _routing_absence_caption

    caption = _routing_absence_caption(None, overlay_error="boom")
    assert "Overlay draw failed: boom" in caption
