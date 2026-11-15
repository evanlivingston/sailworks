"""Widget-level checks for the Historical years options and results panels."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap

from sailworks.qt.historicalroutingoptions import HistoricalRoutingOptions
from sailworks.qt.historicalroutingresults import HistoricalRoutingResults, _CurveCanvas
from sailworks.qt.slider import Slider

ROWS = [
    {
        "member_id": "2023-000",
        "year": 2023,
        "offset_days": 0,
        "departure": "2023-06-15 12:00",
        "outcome": "finished",
        "duration_hours": 120.5,
        "route_uid": "route-a",
    },
    {
        "member_id": "2022-+01",
        "year": 2022,
        "offset_days": 1,
        "departure": "2022-06-16 12:00",
        "outcome": "no_finish_within_horizon",
        "duration_hours": 240.0,
        "route_uid": "",
    },
    {
        "member_id": "2021--02",
        "year": 2021,
        "offset_days": -2,
        "departure": "2021-06-13 12:00",
        "outcome": "failed_execution",
        "duration_hours": None,
        "route_uid": "",
    },
]


def options(qtbot):
    widget = HistoricalRoutingOptions()
    qtbot.addWidget(widget)
    return widget


def test_defaults_match_the_spec(qtbot):
    widget = HistoricalRoutingOptions()
    qtbot.addWidget(widget)
    values = widget.values()
    assert values == {
        "years": 10,
        "offset_days": 3,
        "max_duration_hours": 240,
        "use_currents": False,
    }
    assert widget.validation_message() == ""
    for control in (
        widget.years,
        widget.offset_days,
        widget.max_duration,
        widget.use_currents,
    ):
        assert control.accessibleName()


def test_deleted_controls_are_gone(qtbot):
    widget = options(qtbot)
    for name in (
        "edit_plan",
        "edit_region",
        "region_label",
        "current_priority",
        "setup_hint",
    ):
        assert not hasattr(widget, name)
    for name in ("set_setup_ready", "is_setup_ready"):
        assert not hasattr(widget, name)
    for name in ("edit_plan_requested", "edit_region_requested"):
        assert not hasattr(widget, name)


def test_member_cap_is_reported_and_clears(qtbot):
    widget = options(qtbot)
    assert widget.validation_message() == ""
    widget.years.setValue(30)
    widget.offset_days.setValue(5)
    assert widget.years.value() * (2 * widget.offset_days.value() + 1) == 330
    message = widget.validation_message()
    assert message and "256" in message
    widget.years.setValue(10)
    assert widget.validation_message() == ""


def test_ranges_clamp_and_horizon_snaps_to_six_hours(qtbot):
    widget = options(qtbot)
    widget.years.setValue(0)
    assert widget.years.value() == 1
    widget.years.setValue(31)
    assert widget.years.value() == 30
    widget.offset_days.setValue(15)
    assert widget.offset_days.value() == 14
    widget.max_duration.setValue(241)
    assert widget.max_duration.value() == 240
    widget.max_duration.setValue(6)
    assert widget.max_duration.value() == 6
    widget.max_duration.setValue(720)
    assert widget.max_duration.value() == 720
    widget.years.setValue(10)
    widget.offset_days.setValue(3)
    assert widget.validation_message() == ""


def test_set_values_round_trip_ignores_retired_keys(qtbot):
    widget = HistoricalRoutingOptions()
    qtbot.addWidget(widget)
    widget.set_values(
        {
            "years": 7,
            "offset_days": 2,
            "max_duration_hours": 120,
            "use_currents": True,
            "region": (1.0, -2.0, 3.0, 4.0),
            "current_priority": "newest",
        }
    )
    assert widget.values() == {
        "years": 7,
        "offset_days": 2,
        "max_duration_hours": 120,
        "use_currents": True,
    }


def test_numeric_inputs_use_the_standard_slider(qtbot):
    widget = options(qtbot)
    for control in (widget.years, widget.offset_days, widget.max_duration):
        assert isinstance(control, Slider)
        assert control.accessibleName()


def test_run_button_never_gates_on_route_readiness(qtbot):
    widget = options(qtbot)
    assert not hasattr(widget, "set_setup_ready")
    assert widget.run_button.isEnabled()
    assert widget.run_button.toolTip() == ""

    widget.set_running(True)
    assert not widget.run_button.isEnabled()
    widget.set_running(False)
    assert widget.run_button.isEnabled()


def test_running_state_switches_buttons(qtbot):
    widget = options(qtbot)
    widget.set_running(True)
    assert not widget.run_button.isEnabled()
    assert widget.cancel_button.isEnabled()
    widget.set_running(False)
    assert widget.run_button.isEnabled()
    assert not widget.cancel_button.isEnabled()
    widget.set_available(False, "Native backend capability missing: bound runs")
    assert not widget.run_button.isEnabled()
    assert (
        widget.run_button.toolTip() == "Native backend capability missing: bound runs"
    )


def test_run_button_emits_the_current_values(qtbot):
    widget = options(qtbot)
    captured = []
    widget.run_requested.connect(captured.append)
    widget.run_button.click()
    assert captured == [widget.values()]


def test_preflight_panel_hides_when_empty(qtbot):
    widget = options(qtbot)
    widget.set_preflight([])
    assert widget.preflight.isHidden()
    widget.set_preflight(["Feb 29 -> Feb 28 in 2023", "DST gap advanced 1 h"])
    assert not widget.preflight.isHidden()
    assert widget.preflight.toPlainText().splitlines() == [
        "Feb 29 -> Feb 28 in 2023",
        "DST gap advanced 1 h",
    ]
    widget.set_preflight([])
    assert widget.preflight.isHidden()


def test_rows_populate_table_with_outcome_prefixes_and_clicks(qtbot):
    results = HistoricalRoutingResults()
    qtbot.addWidget(results)
    selected = []
    results.member_selected.connect(selected.append)
    results.set_rows(ROWS)
    assert results.table.rowCount() == 3

    outcomes = [
        results.table.item(row, 3).text() for row in range(results.table.rowCount())
    ]
    prefixes = {text.split(" ")[0] for text in outcomes}
    assert prefixes == {"✓", "—", "✕"}
    assert any("Finished" in text for text in outcomes)
    assert any("No finish" in text for text in outcomes)

    row = next(
        index
        for index in range(results.table.rowCount())
        if results.table.item(index, 0).data(Qt.ItemDataRole.UserRole) == "2023-000"
    )
    results.table.cellClicked.emit(row, 3)
    assert selected == ["2023-000"]


def test_results_stay_hidden_until_a_run_is_displayed(qtbot):
    results = HistoricalRoutingResults()
    qtbot.addWidget(results)
    assert not hasattr(results, "history_selector")
    assert not hasattr(results, "run_selected")
    assert results.isHidden()
    assert not results.empty_label.isHidden()
    assert "No historical run yet" in results.empty_label.text()

    results.set_has_run(True)
    assert not results.isHidden()
    assert results.empty_label.isHidden()
    results.set_has_run(False)
    assert results.isHidden()
    assert not results.empty_label.isHidden()


def test_only_three_result_actions_remain_and_emit(qtbot):
    results = HistoricalRoutingResults()
    qtbot.addWidget(results)
    for name in (
        "median_button",
        "representatives_button",
        "show_all_button",
        "hide_all_button",
    ):
        assert not hasattr(results, name)
    assert results.findChildren(type(results.delete_button)) == [
        results.retry_button,
        results.recheck_button,
        results.delete_button,
    ]

    results.set_actions_enabled(True, True, True)
    calls = []
    results.retry_failed_requested.connect(lambda: calls.append("retry"))
    results.recheck_requested.connect(lambda: calls.append("recheck"))
    results.delete_requested.connect(lambda: calls.append("delete"))
    results.retry_button.click()
    results.recheck_button.click()
    results.delete_button.click()
    assert calls == ["retry", "recheck", "delete"]


def test_actions_follow_job_state(qtbot):
    results = HistoricalRoutingResults()
    qtbot.addWidget(results)
    results.set_actions_enabled(True, True, True)
    for button in (results.retry_button, results.recheck_button, results.delete_button):
        assert button.isEnabled()

    results.set_actions_enabled(False, False, False)
    for button in (results.retry_button, results.recheck_button, results.delete_button):
        assert not button.isEnabled()

    results.set_rows(ROWS)
    results.set_actions_enabled(False, True, True)
    assert not results.retry_button.isEnabled()
    assert results.recheck_button.isEnabled()
    results.clear()
    assert not results.delete_button.isEnabled()
    assert results.table.rowCount() == 0


def test_curve_canvas_paints_empty_and_populated_data(qtbot):
    canvas = _CurveCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(QSize(320, 200))
    pixmap = QPixmap(canvas.size())
    canvas.render(pixmap)
    assert not pixmap.isNull()

    canvas.set_curve([])
    canvas.set_strip([])
    canvas.render(QPixmap(canvas.size()))

    canvas.set_curve([(0.0, 0.0), (60.0, 0.35), (120.0, 0.8), (180.0, 1.0)])
    canvas.set_strip(
        [
            {
                "year": 2022,
                "offset_days": 0,
                "duration_hours": 120.0,
                "outcome": "finished",
            },
            {
                "year": 2023,
                "offset_days": 0,
                "duration_hours": 140.0,
                "outcome": "finished",
            },
            {
                "year": 2023,
                "offset_days": 1,
                "duration_hours": 240.0,
                "outcome": "no_finish",
            },
        ]
    )
    painted = QPixmap(canvas.size())
    canvas.render(painted)
    assert not painted.isNull()

    results = HistoricalRoutingResults()
    qtbot.addWidget(results)
    results.resize(QSize(640, 520))
    results.set_summary("Requested 10 years · Effective 9 · Excluded 1 · 63 routes")
    results.set_rows(ROWS)
    results.set_curve([(0.0, 0.0), (120.0, 0.8)])
    results.set_strip(canvas._strip)
    results.render(QPixmap(results.size()))
