"""The Advanced routing disclosure must explain each setting in plain words."""

import weatherrouting

from PySide6.QtCore import QPoint

from sailworks.qt.dropdown import ROW_SPACING, ToggleFieldRow
from sailworks.qt.routingpanel import (
    PARAMETER_HELP,
    SAFETY_HELP,
    InfoButton,
    RoutingPanel,
)
from test.qt_analysis_fakes import app
from test.test_historical_panel import routing_panel


def test_parameter_help_covers_every_builtin_algorithm_parameter():
    keys = set()
    for entry in weatherrouting.list_routing_algorithms():
        for key in entry["class"].PARAMS:
            keys.add(key)
            assert key in PARAMETER_HELP, key
            # A restated label is not help; require a full sentence.
            assert len(PARAMETER_HELP[key]) > 80, key
    assert keys


def test_safety_help_covers_every_routing_limit():
    assert set(SAFETY_HELP) == {
        "limit_wind",
        "motor_fallback",
        "motor_speed",
        "wave_proxy",
    }
    for text in SAFETY_HELP.values():
        assert len(text) > 80


def test_info_button_opens_a_wrapped_popover_instead_of_a_tooltip():
    app()
    button = RoutingPanel._info_button(SAFETY_HELP["wave_proxy"])
    assert isinstance(button, InfoButton)
    assert button.info_text() == SAFETY_HELP["wave_proxy"]
    # A native tooltip renders the text as an unreadable single line, so the
    # explanation lives in a wrapped popover instead.
    assert button.toolTip() == ""

    button._open_popup()
    assert button._popup is not None
    assert button._popup.wordWrap()
    assert 220 <= button._popup.maximumWidth() <= 360
    assert button._popup.text() == SAFETY_HELP["wave_proxy"]

    button._hide_popup()
    assert button._popup is None


def test_boolean_panel_rows_use_the_label_and_control_columns(
    qtbot, monkeypatch, tmp_path
):
    panel = routing_panel(qtbot, monkeypatch, tmp_path)
    panel.resize(560, 1200)
    panel.show()
    panel.parameters_header.setChecked(True)
    toggle = getattr(panel, "_toggle_parameters", None)
    if toggle is not None:
        toggle(True)
    app().processEvents()

    rows = {
        row.layout().itemAt(0).widget().text(): row
        for row in panel.findChildren(ToggleFieldRow)
    }
    coastlines = rows["Check coastlines"]
    wave_proxy = rows["Wind-based speed reduction (wave proxy)"]

    def label_right(row):
        label = row.layout().itemAt(0).widget()
        return label.mapTo(panel, QPoint(label.width(), 0)).x()

    def checkbox_left(row):
        return row.checkbox().mapTo(panel, QPoint(0, 0)).x()

    # A labelled dropdown in the same form is the control-column reference.
    reference = panel.backend.mapTo(panel, QPoint(0, 0)).x() + round(
        panel.backend._field_rect().left()
    )

    for row in (coastlines, wave_proxy):
        assert row.field() is None
        assert row.checkbox().text() == ""
        assert checkbox_left(row) == reference
        assert label_right(row) == reference - ROW_SPACING
        # Not a flush-left standalone checkbox: it sits at the control
        # column, past the label band.
        row_left = row.mapTo(panel, QPoint(0, 0)).x()
        assert checkbox_left(row) > row_left + ROW_SPACING


def test_idle_panel_hides_progress_and_has_no_wind_only_message(
    qtbot, monkeypatch, tmp_path
):
    panel = routing_panel(qtbot, monkeypatch, tmp_path)
    panel.show()

    # Idle means no message and no bar: the panel must not assert a
    # wind-only route before one has been calculated.
    assert panel.routing.message == ""
    assert panel.status.text() == ""
    assert "wind-only" not in panel.status.text().lower()
    assert not panel.progress.isVisibleTo(panel)
    assert not panel.status.isVisibleTo(panel)

    panel.routing.busy = True
    panel.routing.message = "Routing"
    panel.refresh()
    assert panel.progress.isVisibleTo(panel)
    assert panel.status.isVisibleTo(panel)

    panel.routing.busy = False
    panel.refresh()
    assert not panel.progress.isVisibleTo(panel)
    # The finished-run message stays readable after the run.
    assert panel.status.isVisibleTo(panel)


def test_a_historical_run_keeps_this_progress_bar_hidden(qtbot, monkeypatch, tmp_path):
    panel = routing_panel(qtbot, monkeypatch, tmp_path)
    panel.show()
    panel.historical.is_running = lambda: True
    panel.routing.busy = True
    panel.routing.message = "Routing"
    panel.refresh()
    # The historical panel owns progress while it runs.
    assert not panel.progress.isVisibleTo(panel)
    assert not panel.status.isVisibleTo(panel)
