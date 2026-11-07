"""Debounced Qt view and panel preferences, separate from NMEA history."""

from datetime import datetime
import logging
import math
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QGuiApplication

from sailworks.core.storage import DATA_DIR
from sailworks.core.workspacestate import (
    WorkspaceStateStore,
    mapping,
    number,
    window_geometry,
)

logger = logging.getLogger("sailworks")


def workareas():
    return [
        (r.x(), r.y(), r.width(), r.height())
        for screen in QGuiApplication.screens()
        for r in [screen.availableGeometry()]
    ]


def geometry(rect):
    return dict(x=rect.x(), y=rect.y(), width=rect.width(), height=rect.height())


def fit_window(widget, saved):
    bounds = window_geometry(saved, workareas())
    widget.setGeometry(bounds["x"], bounds["y"], bounds["width"], bounds["height"])


class WorkspacePreferences(QObject):
    def __init__(self, window, path=None):
        super().__init__(window)
        self.window = window
        self.store = WorkspaceStateStore(
            Path(path) if path is not None else Path(DATA_DIR) / "workspace-qt.json"
        )
        self.closed = False
        self.restoring = True
        self.maximize_on_show = False
        self.error = ""
        self.last_saved = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(750)
        self.timer.timeout.connect(self._autosave)
        self.restore(self.store.load())
        self.restoring = False
        self._watch()

    def _watch(self):
        window = self.window
        widget = window
        widget.installEventFilter(self)
        window.views.currentChanged.connect(self.changed)
        window.source.currentIndexChanged.connect(self.changed)
        window.map.view_changed.connect(self.changed)
        window.log_map.view_changed.connect(self.changed)
        window.logs_view.splitterMoved.connect(self.changed)
        window.polar_workspace.splitter.splitterMoved.connect(self.changed)
        window.polar_workspace.selection_changed.connect(self.changed)
        window.polar_workspace.performance.changed.connect(self.changed)
        window.navigation_panel.tabs.currentChanged.connect(self.changed)
        window.navigation_panel.track_split.splitterMoved.connect(self.changed)
        window.navigation_panel.route_split.splitterMoved.connect(self.changed)
        window.ais_action.toggled.connect(self.changed)
        window.weather_panel.wind.toggled.connect(self.changed)
        for action in window.map_display_actions:
            action.toggled.connect(self.changed)
        panel = window.routing_panel
        for combo in (panel.polar, panel.algorithm):
            combo.currentIndexChanged.connect(self.changed)
        window.navigation_panel.route_selected.connect(self.changed)
        panel.coastlines.toggled.connect(self.changed)
        panel.pages.currentChanged.connect(self.changed)
        for panel in window.marine_panels.values():
            panel.toggle.toggled.connect(self.changed)
        window.routing_panel.options_changed.connect(self.changed)
        window.timeline.step_changed.connect(self.changed)
        window.timeline.region_changed.connect(self.changed)
        window.workspace_toolbar.visibilityChanged.connect(self.changed)

    def eventFilter(self, watched, event):  # noqa: N802 - Qt virtual method name
        if (
            watched is self.window
            and event.type() == QEvent.Type.Show
            and self.maximize_on_show
        ):
            self.maximize_on_show = False
            # Establish native normal bounds before the first maximization.
            QTimer.singleShot(0, self.window.showMaximized)
        if event.type() in (
            QEvent.Type.Move,
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.Hide,
            QEvent.Type.WindowStateChange,
        ):
            self.changed()
        return False

    def changed(self, *_):
        if not self.closed and not self.restoring:
            self.timer.start()

    def _autosave(self):
        if any(canvas.is_moving() for canvas in (self.window.map, self.window.log_map)):
            self.timer.start()
            return
        self.save()

    def save_state(self):
        window = self.window
        normal = window.normalGeometry()
        if not normal.isValid():
            normal = window.geometry()
        panel = window.routing_panel
        route = window.navigation_panel.route
        return {
            "window": {**geometry(normal), "maximized": window.isMaximized()},
            "panels": window.panel_state(),
            "view": window.views.tabText(window.views.currentIndex()),
            "nmea_source": window.session.source,
            "forecast": {
                "step": window.timeline.step_combo.currentText(),
                "region": (
                    list(window.timeline.region)
                    if getattr(window.timeline, "region", None) is not None
                    else None
                ),
                "time": window.controller.time_control.time.isoformat(),
            },
            "marine": {
                "ship_observations": window.map.ship_observations.visible,
                **{
                    kind: panel.toggle.isChecked()
                    for kind, panel in window.marine_panels.items()
                },
            },
            "navigation": {
                "tab": window.navigation_panel.tabs.tabText(
                    window.navigation_panel.tabs.currentIndex()
                ),
                "tracks_split": window.navigation_panel.track_split.sizes(),
                "routes_split": window.navigation_panel.route_split.sizes(),
                "ais_visible": window.ais_action.isChecked(),
            },
            "polars": {
                "selected": window.polar_workspace.name,
                "split": window.polar_workspace.splitter.sizes(),
                "performance": window.polar_workspace.performance.save_state(),
            },
            "logs": {
                "split": window.logs_view.sizes(),
                "series": dict(window.plots.series),
                "map": {
                    "dashboard": window.log_map.dashboard_visible,
                    "wind": window.log_map.wind_visible,
                    "latitude": window.log_map.camera_state.center_lat,
                    "longitude": (window.log_map.camera_state.center_lon + 180) % 360
                    - 180,
                    "zoom": window.log_map.camera_state.zoom,
                },
            },
            "map": {
                "dashboard": window.map.dashboard_visible,
                "wind": window.map.wind_visible,
                "latitude": window.map.camera_state.center_lat,
                "longitude": (window.map.camera_state.center_lon + 180) % 360 - 180,
                "zoom": window.map.camera_state.zoom,
            },
            "routing": {
                "page": panel.pages.currentIndex(),
                "page_name": panel.pages.tabText(panel.pages.currentIndex()),
                "polar": panel.polar.currentText(),
                "algorithm": panel.algorithm.currentText(),
                "coastlines": panel.coastlines.isChecked(),
                "options": panel.saved_options(),
                "selected_route": route.name if route is not None else None,
            },
        }

    def save(self):
        if self.closed or self.restoring:
            return
        try:
            state = self.save_state()
            if state != self.last_saved:
                self.store.save(state)
                self.last_saved = state
            self.error = ""
        except (OSError, ValueError) as exc:
            self.error = str(exc)
            logger.warning("Could not save Qt workspace preferences: %s", exc)
            self.window.statusBar().showMessage(
                "Could not save workspace: " + str(exc), 5000
            )

    def restore(self, value):
        state = mapping(value)
        source = state.get("nmea_source")
        if source in ("live", "log", "none"):
            self.window.source.setCurrentIndex({"live": 0, "log": 1, "none": 2}[source])
        self.window.restore_panel_state(mapping(state.get("panels")))
        self.window._order_toolbars()
        self._restore_window(mapping(state.get("window")))
        self._restore_views(state)
        view = state.get("view")
        for index in range(self.window.views.count()):
            if self.window.views.tabText(index) == view:
                self.window.views.setCurrentIndex(index)
        saved_map = mapping(state.get("map"))
        canvas = self.window.map
        canvas.camera_state.center_lat = number(
            saved_map.get("latitude"), canvas.camera_state.center_lat, -85, 85
        )
        canvas.camera_state.center_lon = number(
            saved_map.get("longitude"), canvas.camera_state.center_lon, -180, 180
        )
        canvas.camera_state.zoom = number(
            saved_map.get("zoom"), canvas.camera_state.zoom, 1, 20
        )
        canvas.queue_draw()
        self._restore_routing(mapping(state.get("routing")))
        self._restore_map_display(self.window.map, saved_map)
        self.window._sync_map_actions()
        marine = mapping(state.get("marine"))
        ship_observations = marine.get("ship_observations")
        if isinstance(ship_observations, bool):
            self.window.map.ship_observations.set_visible(ship_observations)
        for kind, panel in self.window.marine_panels.items():
            if isinstance(marine.get(kind), bool):
                panel.toggle.setChecked(marine[kind])
        self.window._view_changed()
        self.window.refresh()
        step = mapping(state.get("forecast")).get("step")
        if (
            isinstance(step, str)
            and self.window.timeline.step_combo.findText(step) >= 0
        ):
            self.window.timeline.step_combo.setCurrentText(step)
        forecast = mapping(state.get("forecast"))
        region = forecast.get("region")
        if (
            isinstance(region, (list, tuple))
            and len(region) == 2
            and not isinstance(region[0], bool)
            and not isinstance(region[1], bool)
        ):
            try:
                start = float(region[0])
                end = float(region[1])
            except (TypeError, ValueError):
                start = end = None
            if (
                start is not None
                and end is not None
                and math.isfinite(start)
                and math.isfinite(end)
                and start < end
            ):
                self.window.timeline.set_region(start, end)
        time_value = forecast.get("time")
        if isinstance(time_value, str):
            try:
                parsed = datetime.fromisoformat(time_value)
            except ValueError:
                parsed = None
            if parsed is not None:
                self.window.controller.time_control.set_time(parsed)

    def _restore_window(self, saved):
        if saved:
            fit_window(self.window, saved)
            self.maximize_on_show = saved.get("maximized") is True

    def _restore_navigation(self, state):
        navigation = mapping(state.get("navigation"))
        self.window._view_changed()
        panel = self.window.navigation_panel
        for index in range(panel.tabs.count()):
            if panel.tabs.tabText(index) == navigation.get(
                "tab"
            ) and panel.tabs.isTabVisible(index):
                panel.tabs.setCurrentIndex(index)
        for splitter, key in (
            (panel.track_split, "tracks_split"),
            (panel.route_split, "routes_split"),
        ):
            saved = navigation.get(key)
            if isinstance(saved, list) and len(saved) == 2:
                splitter.setSizes([int(number(v, 300, 60, 10000)) for v in saved])
        if isinstance(navigation.get("ais_visible"), bool):
            self.window.ais_action.setChecked(navigation["ais_visible"])

    def _restore_views(self, state):
        self._restore_navigation(state)
        polars = mapping(state.get("polars"))
        workspace = self.window.polar_workspace
        selected = polars.get("selected")
        if isinstance(selected, str) and selected in workspace.names:
            workspace.select(selected)
        workspace.performance.restore_state(polars.get("performance"))
        logs = mapping(state.get("logs"))
        for splitter, saved in (
            (workspace.splitter, polars.get("split")),
            (self.window.logs_view, logs.get("split")),
        ):
            if isinstance(saved, list) and len(saved) == 2:
                splitter.setSizes([int(number(v, 300, 60, 10000)) for v in saved])
        for key, value in mapping(logs.get("series")).items():
            if isinstance(value, bool):
                self.window.plots.set_series(key, value)
        saved_map = mapping(logs.get("map"))
        canvas = self.window.log_map
        self._restore_map_display(canvas, saved_map)
        canvas.camera_state.center_lat = number(
            saved_map.get("latitude"), canvas.camera_state.center_lat, -85, 85
        )
        canvas.camera_state.center_lon = number(
            saved_map.get("longitude"), canvas.camera_state.center_lon, -180, 180
        )
        canvas.camera_state.zoom = number(
            saved_map.get("zoom"), canvas.camera_state.zoom, 1, 20
        )
        canvas.queue_draw()

    @staticmethod
    def _restore_map_display(canvas, saved):
        if isinstance(saved.get("dashboard"), bool):
            canvas.set_dashboard_visible(saved["dashboard"])
        if isinstance(saved.get("wind"), bool):
            canvas.set_wind_visible(saved["wind"])

    def _restore_routing(self, saved):
        panel = self.window.routing_panel
        for combo, key in (
            (panel.polar, "polar"),
            (panel.algorithm, "algorithm"),
        ):
            text = saved.get(key)
            if isinstance(text, str):
                index = combo.findText(text)
                if index >= 0:
                    combo.setCurrentIndex(index)
        if isinstance(saved.get("coastlines"), bool):
            panel.coastlines.setChecked(saved["coastlines"])
        panel.restore_options(mapping(saved.get("options")))
        route = self.window.controller.core.routingManager.get_by_name(
            saved.get("selected_route")
        )
        if route is not None:
            self.window._select_saved_route(route)
        page = saved.get("page_name")
        if not isinstance(page, str):
            # Old layouts included Results between Plan and Compare.
            old_page = saved.get("page")
            page = (
                {0: "Plan", 1: "Plan", 2: "Compare", 3: "Currents"}.get(old_page)
                if type(old_page) is int
                else None
            )
        for index in range(panel.pages.count()):
            if panel.pages.tabText(index) == page:
                panel.pages.setCurrentIndex(index)
                break
        if isinstance(saved.get("coastlines"), bool):
            panel.coastlines.setChecked(saved["coastlines"])

    def reset_layout(self):
        self.restoring = True
        try:
            self.window.restore_panel_state(self.window._default_panel_state)
            self.window.views.setCurrentWidget(self.window.map)
            self.window._view_changed()
        finally:
            self.restoring = False
        self.changed()

    def close(self):
        if not self.closed:
            self.timer.stop()
            self.save()
            self.closed = True
