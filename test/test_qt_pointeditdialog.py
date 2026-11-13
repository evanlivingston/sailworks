"""The mark properties dialog edits the full rounding constraint."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from sailworks.core.geo.rounding import ROUNDING_PORT, ROUNDING_STARBOARD
from sailworks.qt.pointeditdialog import PointEditDialog


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_dialog_edits_full_rounding_spec(app):
    dialog = PointEditDialog(
        "Mark",
        rounding=ROUNDING_PORT,
        radius_nm=2.0,
        exit_bearing_min_deg=30,
        exit_bearing_max_deg=60,
    )
    assert dialog.point_rounding_spec() == {
        "side": "port",
        "radius_nm": 2.0,
        "exit_bearing_min_deg": 30.0,
        "exit_bearing_max_deg": 60.0,
    }

    dialog.rounding_combo.setCurrentIndex(
        dialog.rounding_combo.findData(ROUNDING_STARBOARD)
    )
    dialog.radius.setValue(1.5)
    dialog.exit_min.setValue(-1)
    dialog.exit_max.setValue(-1)
    assert dialog.point_rounding_spec() == {
        "side": "starboard",
        "radius_nm": 1.5,
        "exit_bearing_min_deg": None,
        "exit_bearing_max_deg": None,
    }
    dialog.deleteLater()


def test_sail_to_mark_has_no_routing_spec(app):
    dialog = PointEditDialog("Mark", rounding=2)
    dialog.radius.setValue(2.0)
    assert dialog.point_rounding_spec() is None
    dialog.deleteLater()
