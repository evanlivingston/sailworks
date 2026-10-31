"""Standalone QWidget host for the desktop Charts and Logs viewports.

The host owns application map state and delegates scene presentation and camera
gestures to the Qt Quick viewport. It does not depend on the retired map widget.
"""

from bisect import bisect_right
from dataclasses import dataclass
import math

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QWidget

from gweatherrouting.core.mapviewport import MercatorProjection
from gweatherrouting.rendering.isochrones import IsochroneFrame
from .mapcoverage import MapCoverageMixin
from .mapcursor import MapCursorMixin
from .mapenvironment import MapEnvironmentMixin
from .mapforecast import MapForecastMixin
from .maphost import MapHostMixin
from .mapinteraction import MapInteractionMixin
from .maplifecycle import MapLifecycleMixin
from .mapoverlays import MapOverlayServices, RetainedOverlayMixin
from .mapservices import MapWeatherServices, MapWeatherServicesMixin
from .mapstate import MapStateMixin


@dataclass
class ViewportCameraState:
    center_lat: float = 48.4
    center_lon: float = -123.4
    zoom: float = 6.0


@dataclass
class ViewportMapState:
    camera: ViewportCameraState
    dashboard_visible: bool = True
    wind_visible: bool = True
    ais_targets: tuple = ()
    ais_visible: bool = True
    points: tuple = ()
    route: tuple = ()
    route_speeds: tuple = ()
    selected_route_point: int = -1
    route_plan: tuple = ()
    route_preview: tuple = ()
    offsets: tuple = ()
    position: float | None = None
    race_boat_positions: tuple = ()


class ViewportMap(
    MapInteractionMixin,
    MapHostMixin,
    MapLifecycleMixin,
    QWidget,
    MercatorProjection,
    MapEnvironmentMixin,
    MapStateMixin,
    RetainedOverlayMixin,
    MapWeatherServicesMixin,
    MapCursorMixin,
    MapForecastMixin,
    MapCoverageMixin,
):
    """Standalone app map host whose base/weather scene is Qt Quick."""

    _desktop_viewport = True
    _MAX_TRACK_POLYLINE = 1500

    route_location = Signal(str, object)
    view_changed = Signal()
    navigation_request = Signal(str, object)
    cursor_readout = Signal(str)

    def __init__(self, controller, parent=None, time_control=None, show_timeline=False):
        super().__init__(parent)
        self._initialize_view_state()
        self._weather_services = MapWeatherServices()
        self.setMinimumSize(240, 180)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        self.controller = controller
        self.charts = controller.charts
        self.charts.add_map(self)
        self.time_control = time_control or controller.time_control
        self.time_control.connect("time_change", self._time_changed)

        self.background = None
        self._render_padding = 0
        self._base_cache = None
        self._environment_cache = None
        self._nav_cache = None
        self._dash_cache = None
        self._gpu = None
        self._gpu_geometry = None
        self._gpu_batches = ()
        self._gpu_wind_raster = None
        self._gpu_raster_key = None
        self._overlay_pending = False
        self._zoom_anchor = None
        self._zoom_target = None
        self._zoom_animating = False
        self._smooth_zoom = True
        self._pan_inertia_enabled = bool(
            getattr(controller.settings, "panInertia", True)
        )
        self.drag = None
        self.move_callback = None
        self.disposed = False
        self._ticks = {}
        self._next_tick = 0
        self._cursor_context = None
        self._cursor_parts = {
            "wind": "",
            "gust": "",
            "current": "",
            "depth": "",
        }
        self._cursor_coords = ""
        self._cursor_position = None
        self._cursor_screen_position = None
        self._cursor_refresh_timer = QTimer(self)
        self._cursor_refresh_timer.setSingleShot(True)
        self._cursor_refresh_timer.timeout.connect(self._refresh_cursor_after_motion)
        self.isochrone_frame = IsochroneFrame((), (), ())

        self._weather_services.initialize_base_chart_renderer(controller)
        self._overlay_services = MapOverlayServices.create(
            controller, self.time_control, marine_visible=time_control is None
        )
        self.dashboard_visible = True
        self.wind_visible = True
        self._weather_services.initialize_wind_cursor(self, controller)
        controller.data.changed.connect(self._cancel_cursor)
        self.initialize_coverage_ui(controller)
        controller.boat_changed.connect(self._boat_changed)
        controller.settings.register_on_change("smoothZoom", self._set_smooth_zoom)
        controller.settings.register_on_change("panInertia", self._set_pan_inertia)
        self._weather_services.initialize_weather_presenter(self)
        controller.display_forecast.changed.connect(self._display_forecast_changed)
        controller.set_renderer_status(self, "pending")
        self.timeline = None
        if show_timeline:
            from .forecasttimeline import ForecastTimeline

            self.timeline = ForecastTimeline(controller, self)
            self.timeline.setAutoFillBackground(True)
            self.timeline.expansion_changed.connect(self._layout_timeline)
            self._layout_timeline()

    def _layout_timeline(self):
        if self.timeline is None:
            return
        height = self.timeline.sizeHint().height()
        self.timeline.setGeometry(
            0, max(0, self.height() - height), self.width(), height
        )
        self.timeline.raise_()

    def is_moving(self):
        presenter = self._weather_presenter
        widget = getattr(presenter, "widget", None)
        return widget is not None and widget.is_moving()

    def _cancel_inertia(self, settle=True):
        presenter = self._weather_presenter
        widget = getattr(presenter, "widget", None)
        if widget is not None:
            widget.cancel_motion()

    def _finish_zoom_silent(self):
        presenter = self._weather_presenter
        widget = getattr(presenter, "widget", None)
        if widget is not None:
            widget.finish_motion()

    def _set_smooth_zoom(self, enabled):
        self._smooth_zoom = bool(enabled)
        presenter = self._weather_presenter
        widget = getattr(presenter, "widget", None)
        if widget is not None:
            widget._smooth_zoom = self._smooth_zoom
        if not self._smooth_zoom:
            self._finish_zoom_silent()

    def _set_pan_inertia(self, enabled):
        self._pan_inertia_enabled = bool(enabled)
        presenter = self._weather_presenter
        widget = getattr(presenter, "widget", None)
        if widget is not None:
            widget._pan_inertia_enabled = self._pan_inertia_enabled
        if not self._pan_inertia_enabled:
            self._cancel_inertia()

    def _paint_foreground(self, painter):
        """Publish shared foreground imagery to the active desktop viewport."""
        self._paint_measurement_overlay(painter)
        presenter = self._weather_presenter
        if presenter is None or not presenter.active or presenter.widget is None:
            return
        self._nav_cache, target = self._resolve_reusable_layer(
            self._nav_cache, self._nav_key(), self._navigation_image
        )
        presenter.widget.set_navigation_overlay(self._nav_cache[4], target)
        self._paint_dashboard(painter)

    def _paint_measurement_overlay(self, painter):
        points = getattr(self, "_measure_points", ())
        if not points:
            return
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QColor, QPen

        def screen(point):
            lat, lon = point
            return QPointF(
                self._lon_to_x(lon)
                - self._lon_to_x(self._center_lon)
                + self.width() / 2,
                self._lat_to_y(lat)
                - self._lat_to_y(self._center_lat)
                + self.height() / 2,
            )

        painter.setPen(QPen(QColor("#ffcc33"), 2))
        if len(points) == 2:
            first, second = map(screen, points)
            painter.drawLine(first, second)
            painter.drawText(
                (first + second) / 2, f"{self._measurement_distance_km():.1f} km"
            )
        elif len(points) == 1 and self._measure_preview_point is not None:
            first, second = map(screen, (points[0], self._measure_preview_point))
            painter.drawLine(first, second)
            distance = self._distance_between_km(points[0], self._measure_preview_point)
            painter.drawText((first + second) / 2, f"{distance:.1f} km")
        else:
            painter.drawEllipse(screen(points[0]), 4, 4)

    def _paint_dashboard(self, painter):
        """Publish the dashboard image through the active desktop viewport."""
        presenter = self._weather_presenter
        widget = getattr(presenter, "widget", None)
        if not self.dashboard_visible:
            if widget is not None:
                widget.set_dashboard_overlay(None)
            return
        if presenter is None or not presenter.active or widget is None:
            return
        key = self._dash_key()
        if self._dash_cache is None or self._dash_cache[0] != key:
            self._dash_cache = (key, self._dashboard_image())
        widget.set_dashboard_overlay(self._dash_cache[1])

    def _initialize_view_state(self):
        """Create viewport-owned camera state after QWidget initialization."""
        self._view_state = ViewportMapState(ViewportCameraState())

    @property
    def dashboard_visible(self):
        return self._view_state.dashboard_visible

    @dashboard_visible.setter
    def dashboard_visible(self, visible):
        self._view_state.dashboard_visible = visible

    @property
    def wind_visible(self):
        return self._view_state.wind_visible

    @wind_visible.setter
    def wind_visible(self, visible):
        self._view_state.wind_visible = visible

    @property
    def camera_state(self):
        """Return the viewport-owned camera state for app persistence/UI."""
        return self._view_state.camera

    @property
    def _camera_state(self):
        return self._view_state.camera

    @_camera_state.setter
    def _camera_state(self, value):
        self._view_state.camera = value

    @property
    def _ais_targets_state(self):
        return self._view_state.ais_targets

    @_ais_targets_state.setter
    def _ais_targets_state(self, value):
        self._view_state.ais_targets = value

    @property
    def _ais_visible_state(self):
        return self._view_state.ais_visible

    @_ais_visible_state.setter
    def _ais_visible_state(self, value):
        self._view_state.ais_visible = value

    @property
    def points(self):
        return self._view_state.points

    @points.setter
    def points(self, value):
        self._view_state.points = value

    @property
    def route(self):
        return self._view_state.route

    @route.setter
    def route(self, value):
        self._view_state.route = value

    @property
    def route_speeds(self):
        return self._view_state.route_speeds

    @route_speeds.setter
    def route_speeds(self, value):
        self._view_state.route_speeds = value

    @property
    def selected_route_point(self):
        return self._view_state.selected_route_point

    @selected_route_point.setter
    def selected_route_point(self, value):
        self._view_state.selected_route_point = value

    @property
    def route_plan(self):
        return self._view_state.route_plan

    @route_plan.setter
    def route_plan(self, value):
        self._view_state.route_plan = value

    @property
    def route_preview(self):
        return self._view_state.route_preview

    @route_preview.setter
    def route_preview(self, value):
        self._view_state.route_preview = value

    @property
    def offsets(self):
        return self._view_state.offsets

    @offsets.setter
    def offsets(self, value):
        self._view_state.offsets = value

    @property
    def position(self):
        return self._view_state.position

    @position.setter
    def position(self, value):
        self._view_state.position = value

    @property
    def race_boat_positions(self):
        return self._view_state.race_boat_positions

    @race_boat_positions.setter
    def race_boat_positions(self, value):
        self._view_state.race_boat_positions = tuple(
            (
                (float(item[0]), float(item[1]))
                if isinstance(item, (list, tuple))
                else tuple(item)
            )
            for item in (value or ())
        )

    @property
    def race_positions(self):
        return self.race_boat_positions

    @race_positions.setter
    def race_positions(self, value):
        self.race_boat_positions = value

    @property
    def _center_lat(self):
        return self._camera_state.center_lat

    @_center_lat.setter
    def _center_lat(self, value):
        self._camera_state.center_lat = value

    @property
    def _center_lon(self):
        return self._camera_state.center_lon

    @_center_lon.setter
    def _center_lon(self, value):
        self._camera_state.center_lon = value

    @property
    def _zoom(self):
        return self._camera_state.zoom

    @_zoom.setter
    def _zoom(self, value):
        self._camera_state.zoom = value

    def requested_viewport_camera(self):
        """Translate app map controls into the viewport's camera contract."""
        from .gribviewport import ViewportCamera

        camera_state = self.camera_state
        return ViewportCamera(
            camera_state.center_lat,
            camera_state.center_lon,
            camera_state.zoom,
            self.width(),
            self.height(),
            self.time_control.time,
            self.controller.settings.gribBarbDensity,
            True,
        )

    def adopt_viewport_camera(self, camera):
        """Mirror viewport-owned camera state for existing map APIs/overlays."""
        state = (camera.center_lat, camera.center_lon, camera.zoom)
        current = self.camera_state
        if state == (current.center_lat, current.center_lon, current.zoom):
            return False
        current.center_lat, current.center_lon, current.zoom = state
        self._cancel_cursor()
        cursor_position = getattr(self, "_cursor_screen_position", None)
        cursor_timer = getattr(self, "_cursor_refresh_timer", None)
        if cursor_position is not None and cursor_timer is not None:
            # Camera updates cancel in-flight samples. Re-sample once a drag,
            # inertia pan, or zoom has actually settled, even if the pointer
            # itself has not moved.
            cursor_timer.start(180)
        self.invalidate_navigation()
        self.view_changed.emit()
        presenter = self._weather_presenter
        foreground = getattr(presenter, "foreground", None)
        if foreground is not None:
            foreground.update()
        widget = getattr(presenter, "widget", None)
        if widget is not None:
            widget.set_route_overlay(self.viewport_route_overlay())
        return True

    def _refresh_cursor_after_motion(self):
        position = self._cursor_screen_position
        if (
            position is None
            or self.disposed
            or not self.isVisible()
            or self.is_moving()
            or self.move_callback is not None
        ):
            return
        if self.rect().contains(position.toPoint()):
            self._hover(position)

    def viewport_route_overlay(self):
        """Route and log-track geometry for the viewport's top scene layer."""
        state = self._view_state
        selected_point = []
        active_route = self.controller.core.routingManager.get_by_name(
            self.navigation.hl_routing
        )
        if (
            active_route is not None
            and active_route.visible
            and 0 <= state.selected_route_point < len(active_route)
        ):
            point = active_route.points[state.selected_route_point]
            selected_point = [point[0], point[1]]
        track_points = []
        track_position = []
        track_heading = None
        track_awa = None
        if state.points:
            count = len(state.points)
            stride = -(-count // self._MAX_TRACK_POLYLINE)
            selected_indices = list(range(0, count, stride))
            if selected_indices[-1] != count - 1:
                selected_indices.append(count - 1)
            track_points = [
                [state.points[index].lat, state.points[index].lon]
                for index in selected_indices
            ]
            marker_index = count - 1
            if state.position is not None and state.offsets:
                marker_index = max(
                    0,
                    min(marker_index, bisect_right(state.offsets, state.position) - 1),
                )
            point = state.points[marker_index]
            track_position = [point.lat, point.lon]
            track_heading = _track_point_heading(state.points, marker_index)
            track_awa = getattr(point, "awa", None)
        return {
            "route_plan": [list(point) for point in state.route_plan],
            "route_preview": [list(point) for point in state.route_preview],
            "selected_point": selected_point,
            "track_points": track_points,
            "track_position": track_position,
            "track_heading": track_heading,
            "track_awa": track_awa,
            "caption": _viewport_caption(
                self.move_callback, self.controller.chart_errors
            ),
            "dashboard_visible": self.dashboard_visible,
        }


def _track_point_heading(points, index):
    """Use logged heading, or infer course from neighboring fixes if absent."""
    heading = getattr(points[index], "hdg", None)
    if heading is not None and math.isfinite(float(heading)):
        return float(heading) % 360.0

    current = points[index]

    def distinct_neighbor(indices):
        for neighbor_index in indices:
            neighbor = points[neighbor_index]
            if (neighbor.lat, neighbor.lon) != (current.lat, current.lon):
                return neighbor
        return None

    previous = distinct_neighbor(range(index - 1, -1, -1))
    following = distinct_neighbor(range(index + 1, len(points)))
    start = previous or current
    end = following or current
    if start is end:
        return None

    lat1, lat2 = math.radians(float(start.lat)), math.radians(float(end.lat))
    delta_lon = math.radians(
        (float(end.lon) - float(start.lon) + 180.0) % 360.0 - 180.0
    )
    east = math.sin(delta_lon) * math.cos(lat2)
    north = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(
        lat2
    ) * math.cos(delta_lon)
    if east == north == 0:
        return None
    return math.degrees(math.atan2(east, north)) % 360.0


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
