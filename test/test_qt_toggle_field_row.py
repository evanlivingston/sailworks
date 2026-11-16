"""The Compare axis inputs use a Blender-style label/toggle/field row."""

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QCheckBox, QLineEdit

from sailworks.qt.dropdown import ToggleFieldRow
from test.qt_analysis_fakes import app


def test_row_without_a_toggle_keeps_the_field_enabled_and_the_slot_reserved():
    app()
    field = QLineEdit("90, 100")
    plain = ToggleFieldRow("Polar efficiency", field, checkbox=False)
    assert plain.checkbox() is None
    assert plain.field() is field
    assert field.isEnabled()

    toggled = ToggleFieldRow("Polar efficiency", QLineEdit("90, 100"))
    toggled.resize(400, 24)
    toggled.layout().activate()
    plain.resize(400, 24)
    plain.layout().activate()

    # The empty column still shifts the label, so both fields start together.
    plain_x = field.mapTo(plain, QPoint(0, 0)).x()
    toggled_x = toggled.field().mapTo(toggled, QPoint(0, 0)).x()
    assert plain_x == toggled_x


def test_row_checkbox_starts_disabled_and_reports_each_change_once():
    app()
    field = QLineEdit()
    row = ToggleFieldRow("Wind limit", field)
    checkbox = row.checkbox()
    assert isinstance(checkbox, QCheckBox)
    assert checkbox.text() == ""
    assert checkbox.accessibleName() == "Enable Wind limit"
    assert not field.isEnabled()
    assert not row.is_checked()

    seen = []
    row.checked_changed.connect(seen.append)
    row.set_checked(True)
    assert seen == [True]
    assert row.is_checked()
    assert field.isEnabled()

    row.set_checked(False)
    assert seen == [True, False]
    assert not field.isEnabled()


def test_row_accepts_supplied_widgets_and_owns_their_enablement():
    app()
    checkbox, field = QCheckBox(), QLineEdit()
    row = ToggleFieldRow("Wind limit", field, checkbox)
    assert row.checkbox() is checkbox
    assert row.field() is field
    assert not field.isEnabled()

    checkbox.setChecked(True)
    assert field.isEnabled()


def test_compare_page_builds_one_toggle_row_per_axis(qtbot, monkeypatch, tmp_path):
    from test.test_historical_panel import routing_panel

    panel = routing_panel(qtbot, monkeypatch, tmp_path)
    assert len(panel.comparison_axes) == 4
    for checkbox, values in panel.comparison_axes.values():
        assert checkbox is not None
        assert checkbox.text() == ""
        assert checkbox.accessibleName().startswith("Enable ")
        assert not values.isEnabled()
        checkbox.setChecked(True)
        assert values.isEnabled()
