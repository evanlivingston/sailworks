"""The desktop viewport host and its application compatibility boundary.

The viewport owns the visible base/weather scene and camera gestures.
``TrackMap`` supplies existing route/navigation APIs, shared overlays and the
explicit CPU recovery path; this class translates viewport camera changes for
those compatibility consumers.
"""

from bisect import bisect_right

from .mapcanvas import TrackMap


class ViewportMap(TrackMap):
    """Application map host whose active base/weather presentation is Quick."""

    _desktop_viewport = True

    def requested_viewport_camera(self):
        """Translate app map controls into the viewport's camera contract."""
        from .gribviewport import ViewportCamera

        return ViewportCamera(
            self._center_lat,
            self._center_lon,
            self._zoom,
            self.width(),
            self.height(),
            self.time_control.time,
            self.controller.settings.gribBarbDensity,
            True,
        )

    def adopt_viewport_camera(self, camera):
        """Mirror viewport-owned camera state for existing map APIs/overlays."""
        state = (camera.center_lat, camera.center_lon, camera.zoom)
        if state == (self._center_lat, self._center_lon, self._zoom):
            return False
        self._center_lat, self._center_lon, self._zoom = state
        self._cancel_cursor()
        self.invalidate_navigation()
        self.view_changed.emit()
        presenter = self._weather_presenter
        if presenter is not None and presenter.foreground is not None:
            presenter.foreground.update()
        if presenter is not None and presenter.widget is not None:
            presenter.widget.set_route_overlay(self.viewport_route_overlay())
        return True

    def viewport_route_overlay(self):
        """Route and log-track geometry for the viewport's top scene layer."""
        selected_point = []
        active_route = self.controller.core.routingManager.get_by_name(
            self.navigation.hl_routing
        )
        if (
            active_route is not None
            and active_route.visible
            and 0 <= self.selected_route_point < len(active_route)
        ):
            point = active_route.points[self.selected_route_point]
            selected_point = [point[0], point[1]]
        track_points = []
        track_position = []
        if self.points:
            count = len(self.points)
            stride = -(-count // self._MAX_TRACK_POLYLINE)
            selected_indices = list(range(0, count, stride))
            if selected_indices[-1] != count - 1:
                selected_indices.append(count - 1)
            track_points = [
                [self.points[index].lat, self.points[index].lon]
                for index in selected_indices
            ]
            marker_index = count - 1
            if self.position is not None and self.offsets:
                marker_index = max(
                    0, min(marker_index, bisect_right(self.offsets, self.position) - 1)
                )
            point = self.points[marker_index]
            track_position = [point.lat, point.lon]
        return {
            "route_plan": [list(point) for point in self.route_plan],
            "route_preview": [list(point) for point in self.route_preview],
            "selected_point": selected_point,
            "track_points": track_points,
            "track_position": track_position,
            "caption": _viewport_caption(
                self.move_callback, self.controller.chart_errors
            ),
            "dashboard_visible": self.dashboard_visible,
        }


def _viewport_caption(move_callback, chart_errors):
    if move_callback is not None and chart_errors:
        return (
            "Click to place the selected point · Esc to cancel · "
            "Some saved charts could not be opened"
        )
    if move_callback is not None:
        return "Click to place the selected point · Esc to cancel"
    if chart_errors:
        return "Some saved charts could not be opened"
    return ""
