"""Settings dialog polls connection status only while it is visible."""

from unittest.mock import Mock, patch

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialog, QWidget  # noqa: E402

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


def test_editing_a_connection_commits_without_pressing_apply(dialog):
    original = {
        "type": "serial",
        "protocol": "nmea0183",
        "direction": "in",
        "data-port": "COM1",
        "baudrate": 4800,
    }
    dialog.draft = [dict(original)]
    dialog.reload()
    dialog.connections.setCurrentRow(0)
    edited = dict(original, **{"data-port": "COM2"})
    connection = Mock()
    connection.exec.return_value = QDialog.DialogCode.Accepted
    connection.result_config = edited
    with (
        patch("sailworks.qt.settingsdialog.ConnectionDialog", return_value=connection),
        patch.object(SettingsDialog, "apply_connections") as apply_connections,
    ):
        dialog.edit_connection(True)
    apply_connections.assert_called_once_with()
    assert dialog.draft[0]["data-port"] == "COM2"
