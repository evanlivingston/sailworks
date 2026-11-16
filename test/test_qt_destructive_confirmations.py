"""Destructive actions ask once and default to keeping the item."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QMessageBox, QWidget  # noqa: E402

from sailworks.qt.datalibrary import DataLibrary  # noqa: E402
from sailworks.qt.polarlibrary import PolarLibrary  # noqa: E402
from sailworks.qt.routingpanel import RoutingPanel  # noqa: E402
from sailworks.qt.settingsdialog import SettingsDialog  # noqa: E402
from test.qt_analysis_fakes import app  # noqa: E402

YES_NO = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No


class _FakeLibrary(QWidget):
    _chart_display_name = DataLibrary._chart_display_name

    def __init__(self):
        super().__init__()
        self.data = SimpleNamespace(remove_chart=Mock())

    def attempt(self, operation):
        operation()


def _chart(name="NOAA 12345", path="/charts/NOAA.000"):
    return SimpleNamespace(name=name, path=path, ctype="vector", deferred=False)


def test_remove_chart_cancels_on_no(qtbot):
    app()
    library = _FakeLibrary()
    qtbot.addWidget(library)
    chart = _chart()
    with patch(
        "sailworks.qt.datalibrary.QMessageBox.question",
        return_value=QMessageBox.StandardButton.No,
    ) as question:
        DataLibrary._confirm_remove_chart(library, chart)

    args = question.call_args.args
    assert args[0] is library
    assert args[1] == "Remove chart?"
    assert "'NOAA 12345'" in args[2]
    assert "not deleted" in args[2]
    assert args[3] == YES_NO
    assert args[4] == QMessageBox.StandardButton.No
    library.data.remove_chart.assert_not_called()


def test_remove_chart_removes_once_on_yes(qtbot):
    app()
    library = _FakeLibrary()
    qtbot.addWidget(library)
    chart = _chart()
    with patch(
        "sailworks.qt.datalibrary.QMessageBox.question",
        return_value=QMessageBox.StandardButton.Yes,
    ):
        DataLibrary._confirm_remove_chart(library, chart)

    library.data.remove_chart.assert_called_once_with(chart)


def test_remove_chart_without_a_name_uses_a_generic_body(qtbot):
    app()
    library = _FakeLibrary()
    qtbot.addWidget(library)
    with patch(
        "sailworks.qt.datalibrary.QMessageBox.question",
        return_value=QMessageBox.StandardButton.No,
    ) as question:
        DataLibrary._confirm_remove_chart(library, _chart(name="", path=""))

    assert "this chart" in question.call_args.args[2]


def test_priority_help_is_parented_to_the_panel(qtbot):
    app()
    panel = QWidget()
    qtbot.addWidget(panel)
    with patch("sailworks.qt.routingpanel.QMessageBox.information") as information:
        RoutingPanel._explain_priority(panel)

    args = information.call_args.args
    assert args[0] is panel
    assert args[0] is not None
    assert "Finest detail" in args[2]


def test_remove_polar_defaults_to_keep(qtbot):
    app()
    library = SimpleNamespace(selected_name=lambda: "boat.orc", attempt=Mock())
    with patch(
        "sailworks.qt.polarlibrary.QMessageBox.question",
        return_value=QMessageBox.StandardButton.No,
    ) as question:
        PolarLibrary.remove(library)

    args = question.call_args.args
    assert "boat.orc" in args[2]
    assert args[3] == YES_NO
    assert args[4] == QMessageBox.StandardButton.No
    library.attempt.assert_not_called()


def test_remove_connection_cancels_on_no(qtbot):
    app()
    dialog = QWidget()
    qtbot.addWidget(dialog)
    dialog.connections = SimpleNamespace(currentRow=lambda: 0)
    dialog.draft = [{"type": "serial", "data-port": "/dev/ttyUSB0"}]
    dialog.reload = Mock()
    with patch(
        "sailworks.qt.settingsdialog.QMessageBox.question",
        return_value=QMessageBox.StandardButton.No,
    ) as question:
        SettingsDialog.remove_connection(dialog)

    args = question.call_args.args
    assert args[0] is dialog
    assert "'/dev/ttyUSB0'" in args[2]
    assert "another provider" in args[2]
    assert args[3] == YES_NO
    assert args[4] == QMessageBox.StandardButton.No
    assert dialog.draft == [{"type": "serial", "data-port": "/dev/ttyUSB0"}]
    dialog.reload.assert_not_called()
