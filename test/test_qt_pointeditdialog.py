"""The mark properties dialog edits side and exit bearings, not a radius."""

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


def test_dialog_has_no_radius_control(app):
    dialog = PointEditDialog("Mark", rounding=ROUNDING_PORT, radius_nm=2.0)
    assert not hasattr(dialog, "radius")
    dialog.deleteLater()


def test_dialog_edits_rounding_side_and_exit_bearings(app):
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
    dialog.exit_min.setValue(-1)
    dialog.exit_max.setValue(-1)
    assert dialog.point_rounding_spec() == {
        "side": "starboard",
        "radius_nm": 2.0,
        "exit_bearing_min_deg": None,
        "exit_bearing_max_deg": None,
    }
    dialog.deleteLater()


def test_dialog_preserves_inert_radius_untouched(app):
    for radius in (None, 0.0, -1.5, 7.25):
        dialog = PointEditDialog("Mark", rounding=ROUNDING_PORT, radius_nm=radius)
        spec = dialog.point_rounding_spec()
        if radius is None:
            # A missing radius keeps the parser fallback, never a control value.
            assert spec["radius_nm"] > 0
        else:
            assert spec["radius_nm"] == radius
        dialog.deleteLater()


def test_sail_to_mark_has_no_routing_spec_and_hides_details(app):
    dialog = PointEditDialog("Mark", rounding=2)
    assert dialog.rounding_details.isHidden() is True
    assert dialog.point_rounding_spec() is None
    dialog.rounding_combo.setCurrentIndex(dialog.rounding_combo.findData(ROUNDING_PORT))
    assert dialog.rounding_details.isHidden() is False
    dialog.deleteLater()
