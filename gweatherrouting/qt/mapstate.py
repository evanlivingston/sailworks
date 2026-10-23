"""Shared route and track state API for Qt map implementations."""

import math

import numpy as np

from gweatherrouting.rendering.isochrones import IsochroneFrame


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

    def set_points(self, points, offsets, fit=False):
        self.points = tuple(points)
        self.offsets = tuple(offsets)
        if fit and points:
            self.fit_track()
        self.invalidate_navigation()
        self.update()

    def fit_track(self):
        if not self.points:
            return
        self.fit_coordinates([(point.lat, point.lon) for point in self.points])

    def set_route_plan(self, coordinates):
        self.route_plan = tuple(coordinates)
        self.invalidate_navigation()
        self.update()

    def set_route(self, coordinates, speeds=()):
        self.route = tuple(coordinates)
        self.route_speeds = tuple(speeds)
        self.selected_route_point = -1
        self.invalidate_navigation()
        self.update()

    def select_route_point(self, row):
        self.selected_route_point = row
        self.invalidate_navigation()
        self.update()

    def set_route_preview(self, coordinates):
        self.route_preview = tuple(coordinates)
        self.invalidate_navigation()
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

    def invalidate_chart_sources(self):
        """Apply explicit source switches even while a gesture preview is retained."""
        self._cancel_cursor()
        self._invalidate_base()
        self.queue_draw()
