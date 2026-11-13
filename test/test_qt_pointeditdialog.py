"""The mark properties dialog edits rounding side, not a radius or exit range."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from sailworks.core.geo.rounding import (
    ROUNDING_LABELS,
    ROUNDING_PORT,
    ROUNDING_STARBOARD,
)
from sailworks.qt.pointeditdialog import PointEditDialog


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_dialog_has_no_radius_control(app):
    dialog = PointEditDialog("Mark", rounding=ROUNDING_PORT, radius_nm=2.0)
    assert not hasattr(dialog, "radius")
    dialog.deleteLater()


def test_dialog_edits_rounding_side(app):
    dialog = PointEditDialog("Mark", rounding=ROUNDING_PORT, radius_nm=2.0)
    assert dialog.point_rounding_spec() == {"side": "port", "radius_nm": 2.0}

    dialog.rounding_combo.setCurrentIndex(
        dialog.rounding_combo.findData(ROUNDING_STARBOARD)
    )
    assert dialog.point_rounding_spec() == {"side": "starboard", "radius_nm": 2.0}
    assert not hasattr(dialog, "exit_min")
    assert not hasattr(dialog, "exit_max")
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


def test_sail_to_mark_has_no_routing_spec(app):
    dialog = PointEditDialog("Mark", rounding=2)
    assert dialog.point_rounding_spec() is None
    dialog.rounding_combo.setCurrentIndex(dialog.rounding_combo.findData(ROUNDING_PORT))
    assert dialog.point_rounding_spec() == {"side": "port", "radius_nm": 0.5}
    dialog.deleteLater()


def test_form_uses_one_label_column_and_one_field_column(app):
    dialog = PointEditDialog("Mark", rounding=ROUNDING_PORT, coordinates=(51.5, -1.25))
    dialog.show()
    app.processEvents()

    form = dialog.form
    assert form.columnCount() == 2
    rows = (
        (dialog.name_label, dialog.name_edit),
        (dialog.rounding_label, dialog.rounding_combo),
        (dialog.position_label, dialog.position_value),
    )
    for label, field in rows:
        label_row, label_column, _, _ = form.getItemPosition(form.indexOf(label))
        field_row, field_column, _, _ = form.getItemPosition(form.indexOf(field))
        assert label_column == 0
        assert field_column == 1
        assert label_row == field_row

    label_lefts = {label.geometry().left() for label, _ in rows}
    field_lefts = {field.geometry().left() for _, field in rows}
    assert len(label_lefts) == 1
    assert len(field_lefts) == 1
    dialog.deleteLater()


def test_rounding_combo_is_at_least_as_wide_as_the_name_field(app):
    dialog = PointEditDialog("Mark", rounding=ROUNDING_PORT, coordinates=(51.5, -1.25))
    dialog.show()
    app.processEvents()

    assert dialog.rounding_combo.width() >= dialog.name_edit.width()
    dialog.deleteLater()


def test_rounding_value_starts_at_the_name_field_text(app):
    dialog = PointEditDialog("Mark", rounding=ROUNDING_PORT, coordinates=(51.5, -1.25))
    dialog.show()
    app.processEvents()

    combo = dialog.rounding_combo
    value_x = combo.mapTo(dialog, QPoint(int(combo.value_rect().left()), 0)).x()
    name_x = dialog.name_edit.mapTo(
        dialog, QPoint(dialog._line_edit_text_inset(), 0)
    ).x()

    # No label band is reserved, so the value lines up with the Name field.
    assert value_x == name_x
    assert combo.value_rect().left() < combo.width() * 0.25
    dialog.deleteLater()


def test_rounding_combo_does_not_truncate_the_longest_label(app):
    dialog = PointEditDialog("Mark", rounding=ROUNDING_PORT)
    dialog.show()
    app.processEvents()

    metrics = dialog.rounding_combo.fontMetrics()
    widest = max(metrics.horizontalAdvance(label) for label in ROUNDING_LABELS.values())

    assert dialog.rounding_combo.value_rect().width() >= widest
    dialog.deleteLater()
