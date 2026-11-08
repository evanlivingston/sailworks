"""Shared route and track state API for Qt map implementations."""

import math
import time

import numpy as np
from PySide6.QtCore import QTimer

from sailworks.rendering.isochrones import IsochroneFrame


def fit_zoom(spread_x, spread_y, width, height):
    """Return the fractional zoom that fits projected bounds with 80px margin."""
    spread_x = max(spread_x, 0.0001)
    spread_y = max(spread_y, 0.0001)
    return max(
        1,
        min(
            14,
            math.log2(
                min(
                    max(1, width - 80) / spread_x,
                    max(1, height - 80) / spread_y,
                )
            ),
        ),
    )


class MapStateMixin:
    """Mutators for track, route and position state used by map widgets."""

    def _publish_route_overlay(self):
        """Refresh the retained QML overlay after overlay content changes.

        The QML overlay is normally moved by the camera; mutators that change
        its content must republish it or the change stays invisible until the
        next pan or zoom. Force a rebuild so identity-cached frames cannot keep
        stale isochrones.
        """
        refresh = getattr(self, "refresh_route_overlay", None)
        if callable(refresh):
            refresh()
            return
        publish = getattr(self, "publish_route_overlay", None)
        if callable(publish):
            publish()

    @property
    def ais_targets(self):
        return self._ais_targets_state

    @ais_targets.setter
    def ais_targets(self, targets):
        self._ais_targets_state = tuple(targets)
        self.invalidate_navigation()

    @property
    def ais_visible(self):
        return self._ais_visible_state

    @ais_visible.setter
    def ais_visible(self, visible):
        self._ais_visible_state = bool(visible)
        self.invalidate_navigation()

    def set_isochrones(self, frontiers):
        self.isochrone_frame = IsochroneFrame(tuple(frontiers), (), ())
        if not frontiers:
            self.isochrones.forget(self)
        self.invalidate_navigation()
        self._publish_route_overlay()
        self.update()

    def fit_coordinates(self, coordinates):
        if not coordinates:
            return
        self._cancel_cursor()
        self._cancel_inertia(settle=False)
        lats, lons = zip(*coordinates)
        self._center_lat = (min(lats) + max(lats)) / 2
        self._center_lon = (min(lons) + max(lons)) / 2
        self._zoom = 0
        xs, ys = self.convert_geographic_to_screen_batch(np.array(lats), np.array(lons))
        self._zoom = fit_zoom(np.ptp(xs), np.ptp(ys), self.width(), self.height())
        self.background = None
        self._finish_zoom_silent()
        self._invalidate_base()
        self.invalidate_navigation()
        self.update()
        self.view_changed.emit()

    def center_on(self, latitude, longitude, zoom=None):
        """Center the map on one place, optionally at a specific zoom."""
        self.cancel_fly()
        self._cancel_cursor()
        self._cancel_inertia(settle=False)
        self._center_lat = float(latitude)
        self._center_lon = float(longitude)
        if zoom is not None:
            self._zoom = max(1.0, min(14.0, float(zoom)))
        self.background = None
        self._finish_zoom_silent()
        self._invalidate_base()
        self.invalidate_navigation()
        self.update()
        self.view_changed.emit()

    def cancel_fly(self):
        timer = getattr(self, "_fly_tick", None)
        if timer is not None:
            timer.stop()

    def fly_to(self, latitude, longitude, zoom=None, duration_ms=900):
        """Animate a Google-Earth-style zoom-out, pan and zoom-in.

        The camera dips toward a transit zoom for distant targets so the
        intermediate frames still show context instead of a blind slide.
        User input cancels the animation through adopt_viewport_camera().
        """
        self.cancel_fly()
        self._cancel_cursor()
        self._cancel_inertia(settle=False)
        self._finish_zoom_silent()
        start_lat, start_lon, start_zoom = (
            float(self._center_lat),
            float(self._center_lon),
            float(self._zoom),
        )
        target_lat = max(-85.0, min(85.0, float(latitude)))
        target_lon = ((float(longitude) + 180.0) % 360.0) - 180.0
        target_zoom = start_zoom if zoom is None else max(1.0, min(14.0, float(zoom)))
        lon_delta = ((target_lon - start_lon + 180.0) % 360.0) - 180.0
        separation = math.hypot(
            target_lat - start_lat,
            lon_delta * math.cos(math.radians((start_lat + target_lat) / 2.0)),
        )
        if separation < 0.02:
            dip = 0.0
        elif separation < 1.0:
            dip = 0.75
        else:
            dip = min(2.5, 0.75 + separation * 0.04)
        transit_zoom = max(1.0, min(start_zoom, target_zoom) - dip)
        duration = max(250, int(duration_ms)) / 1000.0
        started = time.monotonic()

        def ease(value):
            value = max(0.0, min(1.0, value))
            return value * value * (3.0 - 2.0 * value)

        def advance():
            elapsed = time.monotonic() - started
            fraction = min(1.0, elapsed / duration)
            if fraction < 0.35:
                zoom_value = start_zoom + (transit_zoom - start_zoom) * ease(
                    fraction / 0.35
                )
            elif fraction < 0.6:
                zoom_value = transit_zoom
            else:
                zoom_value = transit_zoom + (target_zoom - transit_zoom) * ease(
                    (fraction - 0.6) / 0.4
                )
            centre = ease(fraction)
            self._center_lat = start_lat + (target_lat - start_lat) * centre
            self._center_lon = (
                start_lon + lon_delta * centre if fraction < 1.0 else target_lon
            )
            self._zoom = target_zoom if fraction >= 1.0 else zoom_value
            self._invalidate_base()
            self.invalidate_navigation()
            self.update()
            if fraction >= 1.0:
                timer.stop()
                self._finish_zoom_silent()
                self.view_changed.emit()

        timer = QTimer(self)
        timer.setInterval(16)
        timer.timeout.connect(advance)
        old = getattr(self, "_fly_tick", None)
        if old is not None:
            old.stop()
            old.deleteLater()
        self._fly_tick = timer
        timer.start()

    def set_points(self, points, offsets, fit=False):
        self.points = tuple(points)
        self.offsets = tuple(offsets)
        if fit and points:
            self.fit_track()
        self.invalidate_navigation()
        self._publish_route_overlay()
        self.update()

    def fit_track(self):
        if not self.points:
            return
        self.fit_coordinates([(point.lat, point.lon) for point in self.points])

    def set_route_plan(self, coordinates):
        self.route_plan = tuple(coordinates)
        self.invalidate_navigation()
        self._publish_route_overlay()
        self.update()

    def set_route(self, coordinates, speeds=()):
        self.route = tuple(coordinates)
        self.route_speeds = tuple(speeds)
        self.selected_route_point = -1
        self.invalidate_navigation()
        self._publish_route_overlay()
        self.update()

    def select_route_point(self, row):
        self.selected_route_point = row
        self.invalidate_navigation()
        self._publish_route_overlay()
        self.update()

    def set_route_preview(self, coordinates):
        self.route_preview = tuple(coordinates)
        self.invalidate_navigation()
        self._publish_route_overlay()
        self.update()

    def fit_route(self):
        self.fit_coordinates((*self.route, *self.route_plan))

    def set_position(self, position):
        self.position = position
        self.invalidate_navigation()
        self.update()

    def set_dashboard_visible(self, visible):
        self.dashboard_visible = visible
        self.update()

    def set_wind_visible(self, visible):
        self.wind_visible = visible
        self._cancel_cursor()
        self.queue_draw()

    def set_wave_visible(self, visible):
        self.wave_visible = visible
        self._cancel_cursor()
        self.queue_draw()

    def invalidate_chart_sources(self):
        """Apply explicit source switches even while a gesture preview is retained."""
        presenter = getattr(self, "_weather_presenter", None)
        widget = getattr(presenter, "widget", None)
        model = getattr(widget, "_chart_model", None)
        if model is not None:
            model.invalidate_sources()
            widget._chart_force_replace = True
            widget._restart_chart_settle()
        self._cancel_cursor()
        self._invalidate_base()
        self.queue_draw()
