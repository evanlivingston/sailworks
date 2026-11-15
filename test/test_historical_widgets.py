"""Widget-level checks for the Historical years options and results panels."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap

from sailworks.qt.historicalroutingoptions import HistoricalRoutingOptions
from sailworks.qt.historicalroutingprogress import HistoricalProgress, format_eta
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


def test_options_have_no_run_button(qtbot):
    widget = options(qtbot)
    assert not hasattr(widget, "run_button")
    assert not hasattr(widget, "set_setup_ready")
    assert not widget.cancel_button.isEnabled()


def test_running_state_only_switches_cancel(qtbot):
    widget = options(qtbot)
    cancelled = []
    widget.cancel_requested.connect(lambda: cancelled.append(True))

    widget.set_running(True)
    assert widget.cancel_button.isEnabled()
    widget.cancel_button.click()
    assert cancelled == [True]

    widget.set_running(False)
    assert not widget.cancel_button.isEnabled()
    widget.set_available(False, "Native backend capability missing: bound runs")
    assert not widget.cancel_button.isEnabled()


def test_values_follow_the_current_controls(qtbot):
    widget = options(qtbot)
    widget.years.setValue(5)
    widget.offset_days.setValue(2)
    widget.max_duration.setValue(120)
    widget.use_currents.setChecked(True)
    assert widget.values() == {
        "years": 5,
        "offset_days": 2,
        "max_duration_hours": 120,
        "use_currents": True,
    }


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


# -- historical progress -------------------------------------------------


def _snapshot(**overrides):
    snapshot = {
        "running": True,
        "completed": 12,
        "total": 70,
        "route_percent": 41.0,
        "route_indeterminate": False,
        "stage": "forward isochrone",
        "counters": "182k/4.1k/903",
        "elapsed_seconds": 600.0,
    }
    snapshot.update(overrides)
    return snapshot


def test_format_eta_formats_minutes_hours_and_unknowns():
    assert format_eta(120) == "~2 min"
    assert format_eta(5400) == "~1 h 30 min"
    assert format_eta(None) == ""
    assert format_eta(0) == ""
    assert format_eta(-30) == ""


def test_refresh_from_probe_updates_both_real_tiers(qtbot):
    widget = HistoricalProgress()
    qtbot.addWidget(widget)
    widget.set_probe(lambda: _snapshot())
    widget.refresh_from_probe()

    # (12 + 0.41) / 70 = 17.7%, rounded to 18.
    assert widget.overall_bar.value() == 18
    assert widget.overall_label.text() == "12 / 70 routes"
    assert widget.route_bar.value() == 41
    assert widget.route_bar.maximum() == 100
    assert widget.route_label.text() == "41% · forward isochrone"
    assert widget.counters_label.text() == "182k/4.1k/903"
    assert not widget.counters_label.isHidden()
    assert widget.eta_label.text().startswith("~")
    assert widget.eta_label.text().endswith("min")


def test_eta_waits_for_ten_percent_and_thirty_seconds(qtbot):
    widget = HistoricalProgress()
    qtbot.addWidget(widget)
    widget.set_probe(lambda: _snapshot(completed=1, route_percent=None))
    widget.refresh_from_probe()
    assert widget.eta_label.text() == "estimating…"

    widget.set_probe(lambda: _snapshot(elapsed_seconds=10.0))
    widget.refresh_from_probe()
    assert widget.eta_label.text() == "estimating…"


def test_indeterminate_route_uses_a_busy_range(qtbot):
    widget = HistoricalProgress()
    qtbot.addWidget(widget)
    widget.set_probe(lambda: _snapshot(route_percent=None, route_indeterminate=True))
    widget.refresh_from_probe()

    assert widget.route_bar.minimum() == 0
    assert widget.route_bar.maximum() == 0
    assert widget.route_label.text() == "working · forward isochrone"
    # The in-flight route contributes nothing while indeterminate.
    assert widget.overall_bar.value() == round(12 / 70 * 100)


def test_completed_batch_shows_one_hundred_and_no_eta(qtbot):
    widget = HistoricalProgress()
    qtbot.addWidget(widget)
    widget.set_probe(lambda: _snapshot(completed=70, total=70, route_percent=None))
    widget.refresh_from_probe()

    assert widget.overall_bar.value() == 100
    assert widget.overall_label.text() == "70 / 70 routes"
    assert widget.eta_label.text() == ""
    assert widget.eta_label.isHidden()


def test_weather_phase_shows_units_and_hides_the_route_row(qtbot):
    widget = HistoricalProgress()
    qtbot.addWidget(widget)
    widget.set_weather_phase(2, 10, "Downloading ERA5 wind")

    assert widget.overall_bar.value() == 20
    assert widget.overall_label.text() == "2 / 10 weather units"
    assert widget.route_bar.isHidden()
    assert widget.route_caption.isHidden()
    assert widget.route_label.isHidden()
    assert widget.eta_label.text() == "estimating…"
    # The bars carry the detail; the prose line stays out of the panel.
    assert widget.counters_label.isHidden()
    assert widget.request_bar.isHidden()
    assert widget.hour_bar.isHidden()


def test_weather_detail_draws_request_and_hour_bars(qtbot):
    widget = HistoricalProgress()
    qtbot.addWidget(widget)
    widget.set_weather_phase(2, 9, "")

    widget.set_weather_detail(
        {
            "span_done": 2,
            "span_total": 9,
            "chunk_done": 2,
            "chunk_total": 17,
            "hour_done": 3,
            "hour_total": 24,
        }
    )

    assert not widget.request_bar.isHidden()
    assert widget.request_label.text() == "2 / 17"
    assert widget.request_bar.value() == 12
    assert not widget.hour_bar.isHidden()
    assert widget.hour_label.text() == "3 / 24"
    assert widget.hour_bar.value() == 12
    # The batch bar stays on spans, not on the finer tiers.
    assert widget.overall_label.text() == "2 / 9 weather units"


def test_weather_phase_survives_a_probe_refresh_while_acquiring(qtbot):
    widget = HistoricalProgress()
    qtbot.addWidget(widget)
    widget.set_weather_phase(0, 12, "ERA5 wind coverage")
    widget.set_probe(
        lambda: _snapshot(
            completed=0,
            total=0,
            route_percent=None,
            route_indeterminate=True,
            routing=False,
            weather_detail={
                "span_done": 0,
                "span_total": 12,
                "chunk_done": 1,
                "chunk_total": 17,
                "hour_done": 5,
                "hour_total": 24,
            },
        )
    )
    widget.refresh_from_probe()

    assert widget.overall_label.text() == "0 / 12 weather units"
    assert widget.route_bar.isHidden()
    assert widget.route_caption.isHidden()
    assert widget.route_label.isHidden()
    assert widget.counters_label.isHidden()
    # A probe refresh keeps the finer bars live during acquisition.
    assert widget.request_label.text() == "1 / 17"
    assert widget.hour_label.text() == "5 / 24"


def test_weather_phase_hands_over_when_routing_starts(qtbot):
    widget = HistoricalProgress()
    qtbot.addWidget(widget)
    widget.set_weather_phase(0, 12, "ERA5 wind coverage")
    widget.set_probe(lambda: _snapshot(routing=True))
    widget.refresh_from_probe()

    assert widget.overall_label.text() == "12 / 70 routes"
    assert not widget.route_bar.isHidden()
    assert widget.route_label.text() == "41% · forward isochrone"


def test_reset_is_idle_and_finished_leaves_the_outcome(qtbot):
    widget = HistoricalProgress()
    qtbot.addWidget(widget)
    widget.set_weather_phase(2, 10, "Downloading ERA5 wind")
    widget.reset()

    assert widget.isHidden()
    assert widget.overall_bar.value() == 0
    assert widget.overall_label.text() == ""
    assert widget.eta_label.isHidden()

    widget.set_finished(True)
    assert widget.overall_bar.value() == 100
    assert widget.overall_label.text() == "done"
    assert widget.eta_label.isHidden()

    widget.reset()
    widget.set_finished(False, "boom")
    assert widget.overall_label.text() == "boom"
    assert widget.overall_bar.value() == 0


def test_options_embed_progress_and_follow_running_state(qtbot):
    widget = options(qtbot)
    widget.set_progress_probe(lambda: _snapshot())
    assert widget.progress_widget.isHidden()

    widget.set_running(True)
    assert not widget.progress_widget.isHidden()
    assert widget.progress_widget.overall_label.text() == "12 / 70 routes"

    widget.set_running(False)
    assert widget.progress_widget.isHidden()
