"""Forecast and time callbacks shared by Qt map hosts."""

import math
import sys


class MapForecastMixin:
    def _display_forecast_changed(self):
        self._cancel_cursor()
        self.wind.forget(self)
        self._base_cache = None
        self.weather_coverage.key = None
        if self._weather_presenter is not None:
            self._weather_presenter.source_changed()
        # Apply the selected forecast's geographic extents and time bounds
        sources = self.controller.display_forecast.sources()
        if sources:
            self._apply_forecast_extents(sources[0].path)
            self.time_control.set_time(sources[0].start_time)
        self.queue_draw()
        self.view_changed.emit()

    def _time_changed(self, _):
        self._cancel_cursor()
        gpu = getattr(self, "_gpu", None)
        stager = getattr(gpu, "weather", None)
        if (
            gpu is not None
            and gpu.active
            and stager is not None
            and not self._marine_time_dependent()
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

    def _apply_forecast_extents(self, path=None):
        """Zoom to the file's bounds when a display forecast is selected."""
        try:
            from gweatherrouting.core.grib import Grib

            meta = Grib.parse_metadata(str(path or ""))
            bounds = meta.bounds
            # bounds from parse_metadata: [west, south, east, north]
            west, south, east, north = (
                (float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3]))
                if len(bounds) >= 4
                else (0, 0, 0, 0)
            )
            self._center_lat = (south + north) / 2
            self._center_lon = (west + east) / 2
            # Rough zoom from span; adjust for view size
            lat_span = abs(north - south)
            lon_span = abs(east - west)
            span = max(lat_span, lon_span)
            if span > 0:
                # Approximate: world ~180 lat; at zoom ~3 per 30 deg; scale roughly
                self._zoom = max(
                    1, min(20, round(math.log2(180.0 / max(span, 1.0)) + 3))
                )
            else:
                self._zoom = 6.0
            self._finish_zoom_silent()
            self._invalidate_base()
            self.queue_draw()
        except Exception as exc:
            import traceback

            traceback.print_exc()
            sys.stderr.write(
                f"_apply_forecast_extents failed for path={path!r}: {exc}\n"
            )
