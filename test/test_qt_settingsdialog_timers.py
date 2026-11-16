"""Settings dialog polls connection status only while it is visible."""

from unittest.mock import Mock, patch

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QWidget  # noqa: E402

from sailworks.qt.settingsdialog import SettingsDialog  # noqa: E402


def _controller():
    controller = Mock()
    controller.connections.connections = []
    controller.connections.get_status.return_value = []
    controller.settings = {
        "smoothZoom": True,
        "panInertia": True,
        "developerMode": False,
        "mapRenderer": "cpu",
        "keepEblOriginals": True,
    }
    controller.connection_task.busy = False
    controller.connection_task.message = ""
    controller.weathernext_credentials.path = ""
    return controller


@pytest.fixture
def dialog(qtbot):
    with (
        patch("sailworks.qt.settingsdialog.CurrentsAccount", return_value=QWidget()),
        patch.object(SettingsDialog, "refresh_renderer_status"),
        patch.object(SettingsDialog, "_refresh_weathernext"),
    ):
        widget = SettingsDialog(_controller())
    qtbot.addWidget(widget)
    widget.close()
    return widget


def test_refresh_timer_runs_only_while_dialog_is_visible(qtbot, dialog):
    assert not dialog.timer.isActive()

    dialog.show()
    qtbot.wait(0)
    assert dialog.timer.isActive()

    dialog.hide()
    qtbot.wait(0)
    assert not dialog.timer.isActive()

    dialog.show()
    qtbot.wait(0)
    assert dialog.timer.isActive()

    dialog.close()
    qtbot.wait(0)
    assert not dialog.timer.isActive()
