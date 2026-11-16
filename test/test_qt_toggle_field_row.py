"""The Compare axis inputs use a Blender-style label/toggle/field row."""

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QCheckBox, QLabel, QLineEdit

from sailworks.qt.dropdown import (
    FIELD_FRACTION,
    ROW_SPACING,
    TOGGLE_SLOT,
    Dropdown,
    FieldRow,
    ToggleFieldRow,
    label_band,
)
from sailworks.qt.slider import Slider
from test.qt_analysis_fakes import app


def test_row_orders_label_then_checkbox_column_then_field():
    app()
    field = QLineEdit()
    row = ToggleFieldRow("Wind limit", field)
    layout = row.layout()
    assert isinstance(layout.itemAt(0).widget(), QLabel)
    assert layout.itemAt(1).widget() is row.checkbox().parent()
    assert layout.itemAt(2).widget() is field

    # ``checkbox=False`` adds no slot widget at all: label and field only.
    plain_field = QLineEdit()
    plain = ToggleFieldRow("Tack angle", plain_field, checkbox=False)
    plain_layout = plain.layout()
    plain.resize(400, 24)
    plain.layout().activate()
    assert plain_layout.count() == 2
    assert isinstance(plain_layout.itemAt(0).widget(), QLabel)
    assert plain_layout.itemAt(1).widget() is plain_field


def test_rows_share_label_right_edge_and_control_left_edge():
    application = app()
    toggled = ToggleFieldRow("Wind limit", QLineEdit())
    plain = ToggleFieldRow("Tack angle", QLineEdit(), checkbox=False)
    for row in (toggled, plain):
        row.resize(400, 24)
        row.show()
    application.processEvents()

    def label_right(row):
        label = row.layout().itemAt(0).widget()
        return label.mapTo(row, QPoint(label.width(), 0)).x()

    def control_left(row):
        control = row.checkbox() if row.checkbox() is not None else row.field()
        return control.mapTo(row, QPoint(0, 0)).x()

    assert label_right(toggled) == label_right(plain)
    assert control_left(toggled) == control_left(plain)
    toggled_field = toggled.field().mapTo(toggled, QPoint(0, 0)).x()
    assert toggled_field == control_left(toggled) + TOGGLE_SLOT + ROW_SPACING


def test_row_without_a_toggle_keeps_the_field_enabled_and_has_no_slot():
    application = app()
    field = QLineEdit("90, 100")
    plain = ToggleFieldRow("Polar efficiency", field, checkbox=False)
    assert plain.checkbox() is None
    assert plain.field() is field
    assert field.isEnabled()
    # No empty column: the layout holds only the label and the field.
    assert plain.layout().count() == 2

    toggled = ToggleFieldRow("Polar efficiency", QLineEdit("90, 100"))
    for row in (toggled, plain):
        row.resize(400, 24)
        row.show()
    application.processEvents()

    # The checkbox-free row starts its control at the checkbox column, and the
    # toggle row's field is inset past the checkbox slot.
    plain_x = field.mapTo(plain, QPoint(0, 0)).x()
    checkbox_x = toggled.checkbox().mapTo(toggled, QPoint(0, 0)).x()
    toggled_x = toggled.field().mapTo(toggled, QPoint(0, 0)).x()
    assert plain_x == checkbox_x
    assert toggled_x == checkbox_x + TOGGLE_SLOT + ROW_SPACING


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


def test_plain_toggle_and_slider_rows_share_one_column_model():
    application = app()
    axis = ToggleFieldRow("Departure offsets (hours)", QLineEdit())
    slider_row = ToggleFieldRow("Years", Slider(), checkbox=False)
    plain = FieldRow("Shape", QLineEdit())
    rows = (axis, slider_row, plain)
    for row in rows:
        row.resize(420, 28)
        row.show()
    application.processEvents()

    def label_right(row):
        label = row.layout().itemAt(0).widget()
        return label.mapTo(row, QPoint(label.width(), 0)).x()

    def control_left(row):
        checkbox = row.checkbox() if hasattr(row, "checkbox") else None
        control = checkbox if checkbox is not None else row.field()
        return control.mapTo(row, QPoint(0, 0)).x()

    def field_left(row):
        return row.field().mapTo(row, QPoint(0, 0)).x()

    # One shared column model: equal label right edges and equal control left
    # edges across a toggle row, a slider row and a plain row. The checkbox
    # sits at the control column; only its own row's field is inset past it.
    assert len({label_right(row) for row in rows}) == 1
    assert len({control_left(row) for row in rows}) == 1
    control = control_left(axis)
    assert field_left(axis) == control + TOGGLE_SLOT + ROW_SPACING
    assert field_left(slider_row) == control
    assert field_left(plain) == control
    # Every control ends flush with its row's right edge.
    for row in rows:
        assert field_left(row) + row.field().width() == row.width()


def test_slider_only_insets_its_track_when_it_paints_a_label():
    app()
    plain = Slider()
    plain.resize(400, 24)
    assert plain._field_rect().left() == 0
    assert plain._field_rect().width() == 398

    labelled = Slider("Keep off coast")
    labelled.resize(400, 24)
    assert labelled._field_rect().left() == round(400 * (1.0 - FIELD_FRACTION))


def test_painted_labels_sit_in_the_same_columns_as_a_toggle_row():
    """A Dropdown and a Slider that paint their label share one column model."""
    application = app()
    toggle = ToggleFieldRow("Maximum wind", QLineEdit())
    dropdown = Dropdown("Calculation")
    dropdown.addItem("Legacy (default)")
    slider = Slider("Sailing speed")
    widgets = (toggle, dropdown, slider)
    for widget in widgets:
        widget.resize(420, 28)
        widget.show()
    application.processEvents()

    def label_right(widget):
        if isinstance(widget, ToggleFieldRow):
            label = widget.layout().itemAt(0).widget()
            return label.mapTo(widget, QPoint(label.width(), 0)).x()
        return round(label_band(widget._column_width(), widget.height()).right())

    def control_left(widget):
        if isinstance(widget, ToggleFieldRow):
            return widget.checkbox().mapTo(widget, QPoint(0, 0)).x()
        return round(widget._field_rect().left())

    # The painted label ends where the layout label does, and the painted
    # control starts at the toggle row's control column.
    assert len({label_right(w) for w in widgets}) == 1
    assert len({control_left(w) for w in widgets}) == 1
    assert label_right(dropdown) == control_left(dropdown) - ROW_SPACING


def test_painted_row_keeps_its_columns_when_a_trailing_button_takes_width():
    """An info button shortens the field's right edge, never the columns."""
    app()
    plain = Dropdown("Overlapping models")
    plain.resize(500, 28)
    with_button = Dropdown("Overlapping models")
    with_button.resize(478, 28)
    with_button.set_column_gutter(22)

    assert with_button._field_rect().left() == plain._field_rect().left()
    # Only the right edge moves: the field stops before the button.
    assert with_button._field_rect().right() == plain._field_rect().right() - 22


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
