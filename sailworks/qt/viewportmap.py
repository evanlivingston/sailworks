"""Standalone QWidget host for the desktop Charts and Logs viewports.

The host owns application map state and delegates scene presentation and camera
gestures to the Qt Quick viewport. It does not depend on the retired map widget.
"""

from bisect import bisect_right
from dataclasses import dataclass
import math
import os
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget

from sailworks.core.aismanager import SHIP_TYPE_COLORS
from sailworks.core.mapviewport import MercatorProjection
from sailworks.rendering.isochrones import IsochroneFrame, isochrone_overlay_data
from sailworks.rendering.routingoverlay import routing_overlay_data
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
    wave_visible: bool = False
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


class WaveLegend(QWidget):
    """The wave-height key: the shader's colour strip and 0-4+ m ticks.

    The strip is drawn from :data:`sailworks.common.WAVE_BUCKET_COLORS`, the
    same buckets the wave shader and ``wave_color()`` use, so the legend can
    never drift from the fill it explains.
    """

    TICKS = ("0", "1", "2", "3", "4+")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAccessibleName("Wave height legend")
        self.setToolTip("Significant wave height in metres")
        self.setFixedSize(176, 56)

    def paintEvent(self, event):  # noqa: N802 - Qt virtual method name
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QLinearGradient, QPainter

        from sailworks.common import WAVE_BUCKET_COLORS

        painter = QPainter(self)
        strip = QRectF(8, 8, self.width() - 16, 14)
        gradient = QLinearGradient(strip.topLeft(), strip.topRight())
        positions = len(WAVE_BUCKET_COLORS) - 1
        for index, color in enumerate(WAVE_BUCKET_COLORS):
            gradient.setColorAt(index / positions, QColor("#" + color))
        painter.fillRect(strip, gradient)
        painter.setPen(QColor("#e6edf3"))
        painter.drawRect(strip)
        step = strip.width() / positions
        for index, label in enumerate(self.TICKS):
            x = strip.left() + index * step
            painter.drawLine(int(round(x)), 23, int(round(x)), 27)
            rect = QRectF(x - 12, 29, 24, 16)
            painter.drawText(rect, Qt.AlignmentFlag.AlignHCenter, label)
        painter.end()


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
    drawer_toggle_requested = Signal(str)

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
        self._drawer_state = (False, False)
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
            "wave": "",
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
        self.wave_visible = bool(getattr(controller.settings, "gribWaveVisible", False))
        self.wave_legend = WaveLegend(self)
        self.wave_legend.hide()
        self._layout_wave_legend()
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
            # A native overlay stays above the Quick view; the map keeps its
            # full size and renders behind the expanded panel.
            self.timeline.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
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
        presenter = getattr(self, "_weather_presenter", None)
        layout_host = getattr(presenter, "_layout_host", None)
        if layout_host is not None:
            layout_host()

    def _layout_wave_legend(self):
        legend = getattr(self, "wave_legend", None)
        if legend is None:
            return
        legend.move(12, max(0, self.height() - legend.height() - 12))
        if legend.isVisible():
            legend.raise_()

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
        self._qml_navigation_overlay = False
        self._qml_race_overlay = False
        self._qml_ais_overlay = False
        self._qml_isochrone_overlay = False
        self._qml_dashboard_overlay = False
        presenter = self._weather_presenter
        if presenter is None or not presenter.active or presenter.widget is None:
            return
        # POIs, tracks and race tracks are drawn by the viewport's QML overlay;
        # the Cairo navigation image keeps the remaining layers until they migrate.
        self._qml_navigation_overlay = True
        self._qml_race_overlay = True
        self._qml_ais_overlay = True
        self._qml_isochrone_overlay = True
        self._qml_dashboard_overlay = True
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
        if getattr(self, "_qml_dashboard_overlay", False):
            widget.set_dashboard_overlay(None)
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
    def wave_visible(self):
        return self._view_state.wave_visible

    @wave_visible.setter
    def wave_visible(self, visible):
        self._view_state.wave_visible = visible

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

    def drawer_state(self):
        return self._drawer_state

    def set_drawer_state(self, left_closed, right_closed, force=False):
        """Mirror the window's sidebar drawers in the scene's edge tabs."""
        closed = (bool(left_closed), bool(right_closed))
        if not force and closed == self._drawer_state:
            return
        self._drawer_state = closed
        presenter = getattr(self, "_weather_presenter", None)
        widget = getattr(presenter, "widget", None) if presenter is not None else None
        set_drawer_state = getattr(widget, "set_drawer_state", None)
        if set_drawer_state is not None:
            if force:
                widget._drawer_closed_state = None
            set_drawer_state(*closed)

    def set_drawer_tabs_visible(self, visible):
        """Show or hide the scene's built-in sidebar drawer tabs."""
        presenter = getattr(self, "_weather_presenter", None)
        widget = getattr(presenter, "widget", None) if presenter is not None else None
        set_visible = getattr(widget, "set_drawer_tabs_visible", None)
        if set_visible is not None:
            set_visible(visible)

    def _drawer_toggle_requested(self, side):
        self.drawer_toggle_requested.emit(side)

    def viewport_rect(self):
        """The canvas area the native viewport actually presents.

        The forecast timeline is a native overlay above the Quick view, so it
        does not shorten the camera or reflow the chart when it opens.
        """
        return self.rect()

    def _view_pixel_size(self):
        """Layer placement uses the viewport the scene actually presents."""
        rect = self.viewport_rect()
        return (
            rect.width(),
            rect.height(),
            round(float(self.devicePixelRatioF()), 3),
        )

    def requested_viewport_camera(self):
        """Translate app map controls into the viewport's camera contract."""
        from .gribviewport import ViewportCamera

        viewport = self.viewport_rect()
        camera_state = self.camera_state
        return ViewportCamera(
            camera_state.center_lat,
            camera_state.center_lon,
            camera_state.zoom,
            viewport.width(),
            viewport.height(),
            self.time_control.time,
            self.controller.settings.gribBarbDensity,
            True,
        )

    def refresh_route_overlay(self):
        """Publish current overlay data after an explicit content edit."""
        self._viewport_overlay_cache = None
        self.publish_route_overlay(force=True)

    def preview_move_point(self, element, index, lat, lon):
        """Show a point under the cursor without rebuilding track geometry."""
        self._move_preview_state = (element, int(index), float(lat), float(lon))
        visible = _overlay_tracks(self)[1]
        track_index = next(
            (i for i, track in enumerate(visible) if track is element), -1
        )
        preview = (
            [track_index, int(index), float(lat), float(lon)]
            if track_index >= 0
            else []
        )
        self.set_move_preview(preview)
        return True

    def clear_move_preview(self):
        self._move_preview_state = None
        self.set_move_preview([])

    def set_move_preview(self, preview):
        presenter = getattr(self, "_weather_presenter", None)
        widget = getattr(presenter, "widget", None)
        setter = getattr(widget, "set_move_preview", None)
        if callable(setter):
            return setter(preview)
        return False

    def _retained_qml_dir(self):
        configured = os.environ.get("SAILWORKS_ROUTING_QML_PATH")
        if configured:
            return configured if Path(configured).is_dir() else None
        default = Path(__file__).resolve().parents[2] / "build" / "routing-qt" / "qml"
        return str(default) if default.is_dir() else None

    def _sync_retained_overlay(self):
        """Prefer the retained QSG item when its Qt module can be loaded.

        Falls back to the image-backed canvas payload when the C++ module is
        absent, for example when the app runs on PySide6's bundled Qt.
        """
        presenter = getattr(self, "_weather_presenter", None)
        widget = getattr(presenter, "widget", None)
        root = (
            widget.rootObject()
            if widget is not None and hasattr(widget, "rootObject")
            else None
        )
        if root is None:
            return False
        model = getattr(self, "native_routing", None)
        qml_dir = self._retained_qml_dir() if model is not None else None
        if model is None or qml_dir is None:
            if root.property("retainedRoutingSource"):
                root.setProperty("retainedRoutingSource", "")
            return False
        if not getattr(self, "_retained_import_added", False):
            widget.engine().addImportPath(qml_dir)
            self._retained_import_added = True
        overlay = Path(__file__).with_name("shaders") / "RoutingRetainedOverlay.qml"
        source = QUrl.fromLocalFile(str(overlay)).toString()
        if root.property("retainedRoutingSource") != source:
            root.setProperty("retainedRoutingSource", source)
        item = root.findChild(QObject, "retainedRoutingOverlay")
        if item is None:
            return False
        camera = self.camera_state
        segments = [
            [[float(point[0]), float(point[1])] for point in polyline.points]
            for polyline in model.polylines
            if (polyline.kind != "trace" or model.traces_visible)
            and (
                polyline.kind != "contour" or model.is_layer_visible(polyline.layer_id)
            )
        ]
        item.setProperty("segments", segments)
        item.setProperty("color", QColor(0, 0, 204, 160))
        item.setProperty("lineWidth", 1.5)
        item.setProperty("centerLatitude", float(camera.center_lat))
        item.setProperty("centerLongitude", float(camera.center_lon))
        item.setProperty("zoom", float(camera.zoom))
        return True

    def _update_routing_hover(self, latitude, longitude):
        """Preview the nearest retained trace under the cursor."""
        model = getattr(self, "native_routing", None)
        if model is None:
            return
        trace = model.pick_trace_near(latitude, longitude)
        if trace != model.hover_trace_id:
            model.set_hover_trace(trace)
            self._viewport_overlay_cache = None
            self.publish_route_overlay(force=True)

    def pin_routing_trace(self):
        """Pin the hovered retained trace through the shared selection."""
        model = getattr(self, "native_routing", None)
        routing = getattr(getattr(self, "controller", None), "routing", None)
        selection = getattr(routing, "native_selection", None)
        if model is None or selection is None:
            return False
        trace = model.hover_trace_id
        if trace is None:
            return False
        selection.select_trace(trace)
        self._viewport_overlay_cache = None
        self.publish_route_overlay(force=True)
        return True

    def publish_route_overlay(self, force=False):
        """Publish the overlay only when its content or host changed.

        A camera-only change moves the retained overlay through the viewport's
        own pan window; it must not rebuild or re-key the content.
        """
        self._sync_retained_overlay()
        presenter = getattr(self, "_weather_presenter", None)
        widget = getattr(presenter, "widget", None)
        if widget is None:
            return False
        overlay = self.viewport_route_overlay()
        published = getattr(self, "_viewport_overlay_published", None)
        if (
            not force
            and published is not None
            and published[0] is widget
            and published[1] is overlay
        ):
            return False
        if not widget.set_route_overlay(overlay):
            # The retained Canvas or its camera is not ready; retry later.
            return False
        self._viewport_overlay_published = (widget, overlay)
        return True

    def adopt_viewport_camera(self, camera):
        """Mirror viewport-owned camera state for existing map APIs/overlays."""
        fly = getattr(self, "_fly_tick", None)
        if fly is not None and fly.isActive():
            # Retained weather/chart refreshes re-emit the viewport's previous
            # camera while the fly animation is already ahead of it. User
            # input cancels the animation before it reaches this path.
            return False
        state = (camera.center_lat, camera.center_lon, camera.zoom)
        current = self.camera_state
        if state == (current.center_lat, current.center_lon, current.zoom):
            return False
        cancel_fly = getattr(self, "cancel_fly", None)
        if cancel_fly is not None:
            cancel_fly()
        zoom_changed = current.zoom != camera.zoom
        current.center_lat, current.center_lon, current.zoom = state
        self._cancel_cursor()
        cursor_position = getattr(self, "_cursor_screen_position", None)
        cursor_timer = getattr(self, "_cursor_refresh_timer", None)
        if cursor_position is not None and cursor_timer is not None:
            # Camera updates cancel in-flight samples. Re-sample once a drag,
            # inertia pan, or zoom has actually settled, even if the pointer
            # itself has not moved.
            cursor_timer.start(180)
        # Navigation imagery is padded and can be repositioned across a pan;
        # only a zoom change makes its existing raster unusable.
        if zoom_changed:
            self.invalidate_navigation()
        self.view_changed.emit()
        presenter = self._weather_presenter
        widget = getattr(presenter, "widget", None)
        if widget is not None:
            self.publish_route_overlay()
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
        """Return the overlay payload, rebuilding it only when content changed."""
        key = _viewport_overlay_key(self)
        cached = getattr(self, "_viewport_overlay_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        overlay = _build_viewport_route_overlay(self)
        state = self._view_state
        # Keep the identified inputs alive: an id() in the key must not be
        # reused by a different object while its payload is still cached.
        guard = (
            getattr(self, "isochrone_frame", None),
            getattr(self, "race_tracks_overlay", None),
            getattr(self, "race_courses", None),
            getattr(self, "race_boat_markers", None),
            getattr(self, "ais_targets", ()),
            state.points,
            getattr(state, "offsets", None),
            getattr(state, "route_plan", ()),
            getattr(state, "route_preview", ()),
            getattr(self, "dashboard", None),
        )
        self._viewport_overlay_cache = (key, overlay, guard)
        return overlay


def _viewport_overlay_key(host):
    """Identify overlay content without formatting or resampling it."""
    state = host._view_state
    frame = getattr(host, "isochrone_frame", None)
    boat = getattr(host.controller.core, "boat_info", None)
    return (
        _overlay_pois(host)[0],
        _overlay_tracks(host)[0],
        _overlay_routes(host, host.time_control.time.timestamp()),
        tuple(tuple(point) for point in (state.route_plan or ())),
        tuple(tuple(point) for point in (state.route_preview or ())),
        getattr(state, "selected_route_point", -1),
        id(state.points),
        len(state.points),
        getattr(state, "position", None),
        id(getattr(state, "offsets", None)),
        id(getattr(host, "race_tracks_overlay", None)),
        _overlay_size(getattr(host, "race_tracks_overlay", ())),
        id(getattr(host, "race_courses", None)),
        _overlay_size(getattr(host, "race_courses", ())),
        id(getattr(host, "race_boat_markers", None)),
        _overlay_size(getattr(host, "race_boat_markers", ())),
        _overlay_ais(host),
        id(frame),
        _overlay_size(getattr(frame, "isochrones", ())),
        _overlay_size(getattr(frame, "path", ())),
        _overlay_size(getattr(frame, "exclusions", ())),
        id(getattr(host, "dashboard", None)),
        bool(getattr(host, "dashboard_visible", False)),
        (
            (boat.sog, boat.cog, boat.aws, boat.awa, boat.tws, boat.twa, boat.depth)
            if boat is not None
            else None
        ),
        getattr(host, "move_callback", None) is not None,
        bool(getattr(host.controller, "chart_errors", None)),
        tuple(tuple(point) for point in getattr(host, "_measure_points", ()) or ()),
        (
            tuple(host._measure_preview_point)
            if getattr(host, "_measure_preview_point", None) is not None
            else None
        ),
    )


def _viewport_measurement(host):
    """Measurement points, preview and distance for the route canvas."""
    points = [list(point) for point in getattr(host, "_measure_points", ()) or ()]
    preview = getattr(host, "_measure_preview_point", None)
    preview = list(preview) if preview is not None else []
    distance = -1.0
    try:
        if len(points) == 2:
            distance = float(host._measurement_distance_km())
        elif len(points) == 1 and preview:
            distance = float(host._distance_between_km(points[0], preview))
    except (AttributeError, TypeError, ValueError):
        distance = -1.0
    return points, preview, distance


def _build_viewport_route_overlay(host):
    """Route and log-track geometry for the viewport's top scene layer."""
    state = host._view_state
    measure = _viewport_measurement(host)
    selected_point = []
    active_route = host.controller.core.routingManager.get_by_name(
        host.navigation.hl_routing
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
        stride = -(-count // host._MAX_TRACK_POLYLINE)
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
    manager = host.controller.core.trackManager
    race_tracks = list(getattr(host, "race_tracks_overlay", ()) or ())
    race_courses = list(getattr(host, "race_courses", ()) or ())
    race_positions = list(getattr(host, "race_boat_markers", ()) or ())
    race_tracks_key = repr(race_tracks)
    tracks_key, visible_tracks = _overlay_tracks(host)
    tracks_cache = getattr(host, "_viewport_tracks_cache", None)
    if tracks_cache is None or tracks_cache[0] != tracks_key:
        tracks = [
            {
                "name": str(track.name),
                "points": [[float(point[0]), float(point[1])] for point in track],
                "active": bool(manager.is_active(track)),
                "highlight": (
                    int(host.navigation.hl_track_item)
                    if manager.is_active(track)
                    and host.navigation.hl_track_item is not None
                    else -1
                ),
                "editing": host.navigation.edit_track is track,
            }
            for track in visible_tracks
        ]
        tracks_cache = (tracks_key, tracks, object())
        host._viewport_tracks_cache = tracks_cache
    poi_key, visible_pois = _overlay_pois(host)
    poi_cache = _viewport_section_cache(
        host,
        "pois",
        poi_key,
        lambda: [
            [
                float(poi.position[0]),
                float(poi.position[1]),
                str(poi.name),
                poi.name == host.navigation.hl_poi,
            ]
            for poi in visible_pois
        ],
    )
    timestamp = host.time_control.time.timestamp()
    route_key = _overlay_routes(host, timestamp)
    if route_key is not None:
        route_cache = _viewport_section_cache(
            host,
            "routes",
            route_key,
            lambda: host.navigation.route_overlay_data(timestamp),
        )
    else:
        route_cache = (None, host.navigation.route_overlay_data(timestamp), None)
    overlay = {
        "route_plan": [list(point) for point in state.route_plan],
        "route_preview": [list(point) for point in state.route_preview],
        "selected_point": selected_point,
        "track_points": track_points,
        "track_position": track_position,
        "track_heading": track_heading,
        "track_awa": track_awa,
        "pois": poi_cache[1],
        "_pois_key": poi_cache[2],
        "tracks": tracks_cache[1],
        "_tracks_key": tracks_cache[2],
        "race_tracks": race_tracks,
        "_race_tracks_key": race_tracks_key,
        "race_courses": race_courses,
        "race_positions": race_positions,
        "routes": route_cache[1],
        "_routes_key": route_cache[2],
        "ais_targets": (
            [
                {
                    "lat": float(target.latitude),
                    "lon": float(target.longitude),
                    "angle": float(
                        target.cog
                        if target.cog is not None
                        else (target.heading or 0.0)
                    ),
                    "sog": float(target.sog or 0.0),
                    "color": list(
                        SHIP_TYPE_COLORS.get(target.category, (0.5, 0.5, 0.5))
                    ),
                    "label": target.name or str(target.mmsi),
                }
                for target in host.ais_targets
                if target.has_valid_position()
            ]
            if host.ais_visible
            else []
        ),
        "isochrones": _routing_overlay(host),
        "dashboard": (host.dashboard.overlay_data() if host.dashboard_visible else []),
        "caption": _viewport_caption(host.move_callback, host.controller.chart_errors),
        "dashboard_visible": host.dashboard_visible,
    }
    if measure[0] or measure[1]:
        overlay["measure_points"] = measure[0]
        overlay["measure_preview"] = measure[1]
        overlay["measure_distance"] = measure[2]
    return overlay


def _overlay_size(value):
    try:
        return len(value)
    except TypeError:
        return -1


def _overlay_pois(host):
    """The POI cache key and the visible POIs it identifies."""
    manager = host.controller.core.poiManager
    visible = tuple(poi for poi in manager if poi.visible and poi.position is not None)
    return (
        id(manager),
        getattr(manager, "revision", None),
        tuple(
            (id(poi), poi.revision, str(poi.name), tuple(poi.position))
            for poi in visible
        ),
        host.navigation.hl_poi,
    ), visible


def _overlay_tracks(host):
    """The track cache key and the visible tracks it identifies."""
    manager = host.controller.core.trackManager
    visible = tuple(track for track in manager if track.visible)
    return (
        id(manager),
        getattr(manager, "revision", 0),
        tuple(
            (
                id(track),
                track.revision,
                str(track.name),
                (
                    tuple(track.color)
                    if getattr(track, "color", None) is not None
                    else None
                ),
                bool(manager.is_active(track)),
                host.navigation.edit_track is track,
            )
            for track in visible
        ),
        host.navigation.hl_track_item,
    ), visible


def _overlay_routes(host, timestamp):
    """The route cache key, or None when a host does not identify routes."""
    manager = host.controller.core.routingManager
    if not hasattr(manager, "revision"):
        return None
    return (
        id(manager),
        manager.revision,
        tuple(
            (id(route), route.revision, str(route.name))
            for route in manager
            if route.visible
        ),
        host.navigation.hl_routing,
        timestamp,
    )


def _overlay_ais(host):
    """The AIS values the overlay draws, not just their positions."""
    if not getattr(host, "ais_visible", False):
        return ()
    return tuple(
        (
            getattr(target, "mmsi", None),
            round(float(target.latitude), 5),
            round(float(target.longitude), 5),
            getattr(target, "cog", None),
            getattr(target, "heading", None),
            getattr(target, "sog", None),
            getattr(target, "name", None),
            getattr(target, "category", None),
        )
        for target in host.ais_targets
        if target.has_valid_position()
    )


def _viewport_section_cache(host, section, key, build):
    attribute = "_viewport_" + section + "_cache"
    cache = getattr(host, attribute, None)
    if cache is None or cache[0] != key:
        cache = (key, build(), object())
        setattr(host, attribute, cache)
    return cache


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


def _routing_overlay(host):
    """Prefer the validated native routing geometry when one is attached.

    The legacy preview payload keeps its proximity joins only while no native
    calculation is selected, so a native result is never drawn with invented
    connections.
    """
    model = getattr(host, "native_routing", None)
    if model is not None:
        return routing_overlay_data(model)
    return isochrone_overlay_data(host.isochrone_frame)


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
