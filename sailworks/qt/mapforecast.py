"""Forecast and time callbacks shared by Qt map hosts."""

from datetime import datetime


def region_contains(timeline, clock):
    """Whether ``clock`` lies inside the timeline's user-owned region.

    The region is stored as epoch seconds while ``clock`` is the naive wall
    time reported by ``TimeControl``; compare both as timestamps so the two
    never need converting into each other's representation.
    """
    region = getattr(timeline, "region", None)
    if region is None or clock is None:
        return False
    try:
        start, end = (float(value) for value in region)
        return start <= clock.timestamp() <= end
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False


class MapForecastMixin:
    def _display_forecast_changed(self):
        sources = tuple(self.controller.display_forecast.sources() or ())
        identity = tuple(self._forecast_source_identity(source) for source in sources)
        if identity == getattr(self, "_display_source_identity", None):
            return
        self._display_source_identity = identity
        self._cancel_cursor()
        self._base_cache = None
        self.weather_coverage.key = None
        if self._weather_presenter is not None:
            self._weather_presenter.source_changed()
        if sources:
            # start_time is the model run, which is before the first valid step.
            # Jump to the first valid time so the clock starts inside coverage,
            # but never move a time the user has placed inside their region.
            timeline = getattr(self, "timeline", None)
            clock = getattr(self.time_control, "time", None)
            if not region_contains(timeline, clock):
                stamps = tuple(getattr(sources[0], "valid_times", ()) or ())
                if stamps:
                    self.time_control.set_time(datetime.fromtimestamp(stamps[0]))
                else:
                    self.time_control.set_time(sources[0].start_time)
        self.queue_draw()
        self.view_changed.emit()

    @staticmethod
    def _forecast_source_identity(source):
        for attribute in ("key", "display_identity", "path"):
            identity = getattr(source, attribute, None)
            if identity is not None:
                return attribute, identity
        return "object", id(source)

    def _time_changed(self, _):
        self._cancel_cursor()
        gpu = getattr(self, "_gpu", None)
        stager = getattr(gpu, "weather", None)
        if (
            gpu is not None
            and gpu.active
            and stager is not None
            and stager.request(self) is not None
        ):
            # The numerical field is already staged. CPU fill/isobar workers
            # still receive exact-time requests in _gpu_frame, and queue_draw
            # invalidates the base when a completed background arrives. Do
            # not upload the same cached background for every uniform change.
            self.invalidate_navigation()
            self.update()
            return
        self.queue_draw()
