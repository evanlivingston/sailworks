from datetime import datetime, timezone
from types import SimpleNamespace

from gweatherrouting.qt.mapforecast import MapForecastMixin


class _Signal:
    def __init__(self):
        self.emissions = 0

    def emit(self):
        self.emissions += 1


class _TimeControl:
    def __init__(self):
        self.values = []

    def set_time(self, value):
        self.values.append(value)


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
