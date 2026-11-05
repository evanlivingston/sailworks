from datetime import datetime, timezone
from importlib.util import find_spec
from types import SimpleNamespace

import pytest

from sailworks.qt.mapforecast import MapForecastMixin


class _Signal:
    def __init__(self):
        self.emissions = 0

    def emit(self):
        self.emissions += 1


class _TimeControl:
    def __init__(self):
        self.values = []
        self.time = None

    def set_time(self, value):
        self.values.append(value)
        self.time = value


class _WeatherPresenter:
    def __init__(self):
        self.source_changes = 0

    def source_changed(self):
        self.source_changes += 1


class _Map(MapForecastMixin):
    def __init__(self, sources):
        self.controller = SimpleNamespace(
            display_forecast=SimpleNamespace(sources=lambda: sources)
        )
        self._base_cache = "cached image"
        self.weather_coverage = SimpleNamespace(key="cached coverage")
        self._weather_presenter = _WeatherPresenter()
        self.time_control = _TimeControl()
        self.view_changed = _Signal()
        self.cursor_cancellations = 0
        self.draws = 0

    def _cancel_cursor(self):
        self.cursor_cancellations += 1

    def queue_draw(self):
        self.draws += 1


def _source(key, start_hour):
    return SimpleNamespace(
        key=key,
        start_time=datetime(2026, 10, 29, start_hour, tzinfo=timezone.utc),
    )


def test_progress_notification_for_same_forecast_preserves_view_and_weather():
    original = _source("same-tranche:entry:artifact", 6)
    current = [original]
    map_view = _Map(current)

    map_view._display_forecast_changed()
    assert map_view._base_cache is None
    assert map_view.weather_coverage.key is None
    assert map_view._weather_presenter.source_changes == 1
    assert map_view.time_control.values == [original.start_time]

    # A status/progress notification may rebuild the wrapper around the same
    # immutable artifact, but it must not retire the viewport weather source.
    replacement = _source("same-tranche:entry:artifact", 12)
    current[:] = [replacement]
    map_view._base_cache = "still useful"
    map_view.weather_coverage.key = "still covered"
    previous_draws = map_view.draws
    previous_emissions = map_view.view_changed.emissions
    previous_cancellations = map_view.cursor_cancellations

    map_view._display_forecast_changed()

    assert map_view._base_cache == "still useful"
    assert map_view.weather_coverage.key == "still covered"
    assert map_view._weather_presenter.source_changes == 1
    assert map_view.time_control.values == [original.start_time]
    assert map_view.draws == previous_draws
    assert map_view.view_changed.emissions == previous_emissions
    assert map_view.cursor_cancellations == previous_cancellations


def test_new_forecast_identity_invalidates_weather_source_and_resets_time():
    original = _source("old:entry:artifact", 6)
    current = [original]
    map_view = _Map(current)
    map_view._display_forecast_changed()
    current[:] = [_source("new:entry:artifact", 12)]

    map_view._display_forecast_changed()

    assert map_view._base_cache is None
    assert map_view.weather_coverage.key is None
    assert map_view._weather_presenter.source_changes == 2
    assert map_view.time_control.values[-1] == current[0].start_time
    assert map_view.draws == 2
    assert map_view.view_changed.emissions == 2


def _source_with_steps(key, *steps):
    return SimpleNamespace(
        key=key,
        start_time=datetime.fromtimestamp(steps[0], timezone.utc),
        valid_times=tuple(steps),
    )


def test_model_switch_keeps_clock_inside_user_region():
    start = datetime(2026, 10, 29, 12).timestamp()
    current = [_source_with_steps("old:entry:artifact", start, start + 3600)]
    map_view = _Map(current)
    map_view.timeline = SimpleNamespace(region=(start - 3600, start + 7200))
    user_time = datetime.fromtimestamp(start + 1800)
    map_view.time_control.set_time(user_time)
    map_view.time_control.values.clear()

    map_view._display_forecast_changed()
    assert map_view.time_control.values == []

    current[:] = [
        _source_with_steps("new:entry:artifact", start + 86400, start + 90000)
    ]
    map_view._display_forecast_changed()

    assert map_view.time_control.values == []
    assert map_view.time_control.time == user_time
    assert map_view._base_cache is None
    assert map_view.weather_coverage.key is None


def test_model_switch_moves_clock_outside_user_region():
    start = datetime(2026, 10, 29, 12).timestamp()
    map_view = _Map([_source_with_steps("new:entry:artifact", start, start + 3600)])
    map_view.timeline = SimpleNamespace(region=(start + 5000, start + 9000))
    map_view.time_control.set_time(datetime.fromtimestamp(start - 100))
    map_view.time_control.values.clear()

    map_view._display_forecast_changed()

    assert map_view.time_control.values == [datetime.fromtimestamp(start)]


def test_timeline_without_region_keeps_previous_clock_behaviour():
    start = datetime(2026, 10, 29, 12).timestamp()
    map_view = _Map([_source_with_steps("new:entry:artifact", start, start + 3600)])
    map_view.timeline = SimpleNamespace(region=None)
    map_view.time_control.set_time(datetime.fromtimestamp(start + 1800))
    map_view.time_control.values.clear()

    map_view._display_forecast_changed()

    assert map_view.time_control.values == [datetime.fromtimestamp(start)]


def _controller_with_timeline(timeline):
    from sailworks.qt.controller import SessionController

    controller = SessionController.__new__(SessionController)
    controller.time_control = _TimeControl()
    controller.charts = SimpleNamespace(maps=[SimpleNamespace(timeline=timeline)])
    return controller


def _active(*stamps):
    times = tuple(datetime.fromtimestamp(stamp, timezone.utc) for stamp in stamps)
    return SimpleNamespace(entry=SimpleNamespace(time_steps=times))


@pytest.mark.skipif(find_spec("PySide6") is None, reason="Requires PySide6")
def test_alignment_keeps_clock_inside_user_region():
    start = datetime(2026, 10, 29, 12).timestamp()
    controller = _controller_with_timeline(
        SimpleNamespace(region=(start - 3600, start + 7200))
    )
    user_time = datetime.fromtimestamp(start + 1800)
    controller.time_control.set_time(user_time)
    controller.time_control.values.clear()

    controller._align_clock_to_forecast(_active(start + 86400, start + 90000))

    assert controller.time_control.values == []
    assert controller.time_control.time == user_time


@pytest.mark.skipif(find_spec("PySide6") is None, reason="Requires PySide6")
def test_alignment_moves_clock_outside_user_region():
    start = datetime(2026, 10, 29, 12).timestamp()
    controller = _controller_with_timeline(
        SimpleNamespace(region=(start + 5000, start + 9000))
    )
    controller.time_control.set_time(datetime.fromtimestamp(start - 100))
    controller.time_control.values.clear()

    controller._align_clock_to_forecast(_active(start, start + 3600))

    assert controller.time_control.values == [datetime.fromtimestamp(start)]


@pytest.mark.skipif(find_spec("PySide6") is None, reason="Requires PySide6")
def test_alignment_without_region_keeps_previous_behaviour():
    start = datetime(2026, 10, 29, 12).timestamp()
    timeline = SimpleNamespace(region=None)

    inside = _controller_with_timeline(timeline)
    inside.time_control.set_time(datetime.fromtimestamp(start + 1800))
    inside.time_control.values.clear()
    inside._align_clock_to_forecast(_active(start, start + 3600))
    assert inside.time_control.values == []

    outside = _controller_with_timeline(timeline)
    outside.time_control.set_time(datetime.fromtimestamp(start - 3600))
    outside.time_control.values.clear()
    outside._align_clock_to_forecast(_active(start, start + 3600))
    assert outside.time_control.values == [datetime.fromtimestamp(start)]
