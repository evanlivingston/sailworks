"""Widget-level checks for the Historical years options and results panels."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QAbstractItemView, QTableWidget

from sailworks.qt.historicalroutingoptions import HistoricalRoutingOptions
from sailworks.qt.historicalroutingprogress import HistoricalProgress, format_eta
from sailworks.qt.historicalroutingresults import HistoricalRoutingResults
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
    # The panel's bottom Cancel is the only one: the options area has none.
    assert not hasattr(widget, "cancel_button")
    assert not hasattr(widget, "cancel_requested")


def test_running_state_only_drives_the_progress_widget(qtbot):
    widget = options(qtbot)
    assert not hasattr(widget, "cancel_button")

    widget.set_running(True)
    assert widget._running is True
    widget.set_running(False)
    assert widget._running is False

    widget.set_available(False, "Native backend capability missing: bound runs")
    assert widget._available is False
    assert not hasattr(widget, "cancel_button")


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


def test_rows_track_only_the_median_finished_member(qtbot):
    results = HistoricalRoutingResults()
    qtbot.addWidget(results)
    assert results._median_member_id is None

    results.set_rows(ROWS)
    # Only the first row finished, so it is the median despite its neighbours.
    assert results._median_member_id == "2023-000"

    results.set_rows(
        [
            {
                "member_id": "slow",
                "year": 2020,
                "offset_days": 0,
                "outcome": "finished",
                "duration_hours": 240.0,
            },
            {
                "member_id": "quick",
                "year": 2021,
                "offset_days": 0,
                "outcome": "finished",
                "duration_hours": 10.0,
            },
            {
                "member_id": "middle",
                "year": 2022,
                "offset_days": 0,
                "outcome": "finished",
                "duration_hours": 120.0,
            },
            {
                "member_id": "dnf",
                "year": 2023,
                "offset_days": 0,
                "outcome": "no_finish_within_horizon",
                "duration_hours": 240.0,
            },
        ]
    )
    assert results._median_member_id == "middle"

    results.set_rows([])
    assert results._median_member_id is None


def test_results_panel_has_no_table_summary_or_curve(qtbot):
    results = HistoricalRoutingResults()
    qtbot.addWidget(results)
    for name in ("table", "summary", "canvas", "set_summary", "set_curve", "set_strip"):
        assert not hasattr(results, name)
    for name in ("member_selected",):
        assert not hasattr(results, name)
    assert not results.findChildren(QTableWidget)
    assert not results.findChildren(QAbstractItemView)


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
    assert results._median_member_id == "2023-000"
    results.clear()
    assert not results.delete_button.isEnabled()
    assert results._median_member_id is None
    assert results.isHidden()


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
    qtbot.wait(400)  # the fill tweens towards the new count

    # Counts are drawn inside the bar so a narrow panel cannot clip them.
    assert widget.overall_bar.maximum() == 10
    assert widget.overall_bar.value() == 2
    assert widget.overall_bar.format() == "%v / %m"
    assert widget.overall_bar.isTextVisible()
    assert widget.overall_label.isHidden()
    assert widget.route_bar.isHidden()
    assert widget.route_caption.isHidden()
    assert widget.route_label.isHidden()
    assert widget.eta_label.text() == "estimating…"
    # The bars carry the detail; the prose line stays out of the panel.
    assert widget.counters_label.isHidden()
    # The finer rows stay on screen for the whole phase; toggling them made
    # the layout jump on every update.
    assert not widget.request_bar.isHidden()
    assert not widget.hour_bar.isHidden()


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

    qtbot.wait(400)  # the fill tweens towards the new count
    assert not widget.request_bar.isHidden()
    assert widget.request_bar.maximum() == 17
    assert widget.request_bar.value() == 2
    assert not widget.hour_bar.isHidden()
    assert widget.hour_bar.maximum() == 24
    assert widget.hour_bar.value() == 3
    # The batch bar stays on spans, not on the finer tiers.
    assert widget.overall_bar.maximum() == 9
    assert widget.overall_bar.value() == 2


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
    qtbot.wait(400)  # the fill tweens towards the new count

    assert widget.overall_bar.maximum() == 12
    assert widget.route_bar.isHidden()
    assert widget.route_caption.isHidden()
    assert widget.route_label.isHidden()
    assert widget.counters_label.isHidden()
    # A probe refresh keeps the finer bars live during acquisition.
    assert widget.request_bar.maximum() == 17
    assert widget.request_bar.value() == 1
    assert widget.hour_bar.maximum() == 24
    assert widget.hour_bar.value() == 5


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


def test_weather_detail_shows_downloaded_size_and_time_left(qtbot):
    widget = HistoricalProgress()
    qtbot.addWidget(widget)
    widget.set_weather_phase(0, 3, "")

    widget.set_weather_detail(
        {
            "span_done": 0,
            "span_total": 3,
            "chunk_done": 1,
            "chunk_total": 2,
            "hour_done": 6,
            "hour_total": 12,
            "bytes_done": 1.2e9,
            "bytes_total": 17.2e9,
            "eta_seconds": 720,
        }
    )

    text = widget.eta_label.text()
    assert "1.2 GB / ~17.2 GB" in text
    assert "left" in text


def test_weather_bars_fit_the_counts_drawn_inside_them(qtbot):
    widget = HistoricalProgress()
    qtbot.addWidget(widget)
    widget.set_weather_phase(2, 3, "")
    widget.set_weather_detail(
        {
            "span_done": 2,
            "span_total": 3,
            "chunk_done": 5,
            "chunk_total": 17,
            "hour_done": 3,
            "hour_total": 24,
        }
    )

    # A 10 px bar clipped the digits it now contains.
    for bar in (widget.overall_bar, widget.request_bar, widget.hour_bar):
        assert bar.height() >= 16
        assert bar.isTextVisible()
