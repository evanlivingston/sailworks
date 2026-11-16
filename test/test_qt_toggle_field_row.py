"""The Compare axis inputs use a Blender-style label/toggle/field row."""

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QCheckBox, QLabel, QLineEdit

from sailworks.qt.dropdown import FIELD_FRACTION, TOGGLE_SLOT, FieldRow, ToggleFieldRow
from sailworks.qt.slider import Slider
from test.qt_analysis_fakes import app


def test_row_orders_label_then_reserved_checkbox_slot_then_field():
    app()
    field = QLineEdit()
    row = ToggleFieldRow("Wind limit", field)
    layout = row.layout()
    assert isinstance(layout.itemAt(0).widget(), QLabel)
    assert layout.itemAt(1).widget() is row.checkbox().parent()
    assert layout.itemAt(2).widget() is field

    plain_field = QLineEdit()
    plain = ToggleFieldRow("Tack angle", plain_field, checkbox=False)
    plain_layout = plain.layout()
    plain.resize(400, 24)
    plain.layout().activate()
    assert isinstance(plain_layout.itemAt(0).widget(), QLabel)
    assert plain_layout.itemAt(1).widget().width() == TOGGLE_SLOT
    assert plain_layout.itemAt(2).widget() is plain_field


def test_rows_share_label_right_edge_and_field_left_edge():
    app()
    toggled = ToggleFieldRow("Wind limit", QLineEdit())
    plain = ToggleFieldRow("Tack angle", QLineEdit(), checkbox=False)
    toggled.resize(400, 24)
    toggled.layout().activate()
    plain.resize(400, 24)
    plain.layout().activate()

    def edges(row):
        label = row.layout().itemAt(0).widget()
        field = row.layout().itemAt(2).widget()
        return (
            label.mapTo(row, QPoint(label.width(), 0)).x(),
            field.mapTo(row, QPoint(0, 0)).x(),
        )

    toggled_label, toggled_field = edges(toggled)
    plain_label, plain_field = edges(plain)
    assert toggled_label == plain_label
    assert toggled_field == plain_field
    assert toggled_label < toggled_field


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

    def edges(row):
        label = row.layout().itemAt(0).widget()
        field = row.field()
        return (
            label.mapTo(row, QPoint(label.width(), 0)).x(),
            field.mapTo(row, QPoint(0, 0)).x(),
        )

    lefts = [edges(row) for row in rows]
    # One shared column model: equal label right edges and equal field left
    # edges across a toggle row, a slider row and a plain row.
    assert len({label for label, _ in lefts}) == 1
    assert len({field for _, field in lefts}) == 1
    assert lefts[0][0] < lefts[0][1]


def test_slider_only_insets_its_track_when_it_paints_a_label():
    app()
    plain = Slider()
    plain.resize(400, 24)
    assert plain._field_rect().left() == 0
    assert plain._field_rect().width() == 398

    labelled = Slider("Keep off coast")
    labelled.resize(400, 24)
    assert labelled._field_rect().left() == round(400 * (1.0 - FIELD_FRACTION))


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
