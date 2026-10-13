"""Restore and autosave view preferences without retaining instrument sessions."""

import logging
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib

from gweatherrouting.core.workspacestate import (
    WorkspaceStateStore,
    mapping,
    number,
    window_geometry,
)

logger = logging.getLogger("gweatherrouting")
CHART_TOGGLES = ("navigation-toggle", "wind-toggle", "dashboard-toggle")
LOG_TOGGLES = (
    "speed-chart-toggle",
    "apparent-wind-chart-toggle",
    "true-wind-chart-toggle",
    "depth-chart-toggle",
    "heading-chart-toggle",
    "relative-wind-chart-toggle",
)


def workareas():
    display = Gdk.Display.get_default()
    areas = []
    for index in range(display.get_n_monitors()):
        area = display.get_monitor(index).get_workarea()
        areas.append((area.x, area.y, area.width, area.height))
    return areas


def geometry(window):
    x, y = window.get_position()
    width, height = window.get_size()
    return dict(x=x, y=y, width=width, height=height)


class WorkspacePreferences:
    def __init__(self, app, path=None):
        self.app = app
        self.store = WorkspaceStateStore(path)
        self._timer = None
        self._closed = False
        self._last_change = 0.0
        self._connections = []
        self._manager_watched = False
        self._last_maps = {}
        self._normal_geometry = geometry(app.window)
        state = self.store.load()
        if not mapping(state.get("window")):
            app.window.maximize()
        self.restore(state)
        self._watch()

    def _connect(self, widget, signal, callback=None):
        self._connections.append(
            (widget, widget.connect(signal, callback or self.changed))
        )

    def _watch(self):
        app = self.app
        self._connect(app.window, "configure-event", self._configured)
        self._connect(app.window, "window-state-event")
        self._connect(app.window, "destroy", self._destroyed)
        self._connect(app.builder.get_object("stack1"), "notify::visible-child-name")
        self._connect(app.tool_dock, "layout-changed", self._dock_changed)
        self._connect(app.polarStack.body, "notify::position")
        self._connect(app.polarStack.polar_list.get_selection(), "changed")
        self._connect(app.chartStack.builder.get_object("notebook"), "switch-page")
        for stack, keys in (
            (app.chartStack, CHART_TOGGLES),
            (app.logsStack, LOG_TOGGLES),
        ):
            for key in keys:
                self._connect(stack.builder.get_object(key), "toggled")
            self._last_maps[stack.map] = self._map_state(stack.map)
            self._connect(stack.map, "draw", self._map_drawn)
        for control in (
            app.chartStack.marine_panel.current_toggle,
            app.chartStack.tide_panel.tide_toggle,
            app.polarStack.performance.show_points,
            app.chartStack.nmea.live,
        ):
            self._connect(control, "toggled")
        for control in (
            app.polarStack.performance.basis,
            app.polarStack.performance.wind,
            app.chartStack.nmea.speed,
        ):
            self._connect(control, "changed")
        self._watch_manager()

    def _watch_manager(self):
        panel = self.app.polarStack.manager_panel
        if panel is not None and not self._manager_watched:
            self._manager_watched = True
            self._connect(panel.builder.get_object("manager-tabs"), "switch-page")
            self._connect(panel.builder.get_object("orc-search"), "search-changed")

    def _dock_changed(self, *_):
        self._watch_manager()
        self.changed()

    @staticmethod
    def _map_state(canvas):
        return {
            "latitude": canvas._center_lat,
            "longitude": canvas._center_lon,
            "zoom": canvas._zoom,
        }

    def _map_drawn(self, canvas, _):
        state = self._map_state(canvas)
        if self._last_maps[canvas] != state:
            self._last_maps[canvas] = state
            self.changed()
        return False

    def _configured(self, window, _):
        if not self._maximized():
            self._normal_geometry = geometry(window)
        self.changed()
        return False

    def _maximized(self):
        window = self.app.window.get_window()
        return bool(window and window.get_state() & Gdk.WindowState.MAXIMIZED)

    def changed(self, *_):
        if not self._closed:
            self._last_change = time.monotonic()
            if self._timer is None:
                self._timer = GLib.timeout_add(1000, self._autosave)
        return False

    def _autosave(self):
        # Do not write while a map is being dragged, animated, or pinched.
        if time.monotonic() - self._last_change < 0.75:
            return True
        if any(
            canvas._dragging or canvas._zoom_animating or canvas._inertia
            for canvas in self._last_maps
        ):
            return True
        self._timer = None
        self.save()
        return False

    def save_state(self):
        app = self.app
        store, selected = app.polarStack.polar_list.get_selection().get_selected()
        polar = store.get_value(selected, 0) if selected is not None else None
        state = {
            "window": {**self._normal_geometry, "maximized": self._maximized()},
            "active_tab": app.builder.get_object("stack1").get_visible_child_name(),
            "dock": app.tool_dock.save_state(),
            "charts": {
                "map": self._map_state(app.chartStack.map),
                "toggles": self._toggles(app.chartStack, CHART_TOGGLES),
                "navigation_page": app.chartStack.builder.get_object(
                    "notebook"
                ).get_current_page(),
                "currents": app.chartStack.marine_panel.current_toggle.get_active(),
                "tides": app.chartStack.tide_panel.tide_toggle.get_active(),
            },
            "logs": {
                "map": self._map_state(app.logsStack.map),
                "toggles": self._toggles(app.logsStack, LOG_TOGGLES),
            },
            "polars": {
                "selected": polar,
                "table_width": app.polarStack.body.get_position(),
                "performance": app.polarStack.performance.save_state(),
            },
            "nmea": {
                "streaming": app.core.nmea_session.streaming,
                "speed": app.core.nmea_session.speed,
            },
        }
        panel = app.polarStack.manager_panel
        if panel is not None:
            state["polar_manager"] = {
                "page": panel.builder.get_object("manager-tabs").get_current_page(),
                "search": panel.builder.get_object("orc-search").get_text(),
            }
        return state

    @staticmethod
    def _toggles(stack, keys):
        return {key: stack.builder.get_object(key).get_active() for key in keys}

    def restore(self, value):
        state = mapping(value)
        if not state:
            return
        app = self.app
        saved = mapping(state.get("window"))
        if saved:
            self._normal_geometry = window_geometry(saved, workareas())
            app.window.move(self._normal_geometry["x"], self._normal_geometry["y"])
            app.window.resize(
                self._normal_geometry["width"], self._normal_geometry["height"]
            )
            if saved.get("maximized") is True:
                app.window.maximize()
            else:
                app.window.unmaximize()
        active = state.get("active_tab")
        if isinstance(active, str) and active in app.tool_dock.mounts:
            app.builder.get_object("stack1").set_visible_child_name(active)
        for name, stack, keys in (
            ("charts", app.chartStack, CHART_TOGGLES),
            ("logs", app.logsStack, LOG_TOGGLES),
        ):
            self._restore_view(stack, mapping(state.get(name)), keys)
        self._restore_chart_options(mapping(state.get("charts")))
        self._restore_polars(mapping(state.get("polars")))
        self._restore_session_options(mapping(state.get("nmea")))
        if "polar_manager" in state:
            self._restore_manager(mapping(state["polar_manager"]))
        app.tool_dock.restore_state(state.get("dock"), workareas())

    def _restore_chart_options(self, charts):
        app = self.app
        for key, control in (
            ("currents", app.chartStack.marine_panel.current_toggle),
            ("tides", app.chartStack.tide_panel.tide_toggle),
        ):
            if isinstance(charts.get(key), bool):
                control.set_active(charts[key])
        page = charts.get("navigation_page")
        notebook = app.chartStack.builder.get_object("notebook")
        if type(page) is int and 0 <= page < notebook.get_n_pages():
            notebook.set_current_page(page)

    def _restore_session_options(self, nmea):
        app = self.app
        if isinstance(nmea.get("streaming"), bool):
            app.core.nmea_session.set_streaming(nmea["streaming"])
        if nmea.get("speed") in (0.5, 1, 2, 5, 10):
            app.chartStack.nmea.speed.set_active_id(f'{nmea["speed"]:g}')

    def _restore_manager(self, manager):
        panel = self.app.polarStack.ensure_manager()
        page = manager.get("page")
        if type(page) is int and page in (0, 1):
            panel.builder.get_object("manager-tabs").set_current_page(page)
        if isinstance(manager.get("search"), str):
            panel.builder.get_object("orc-search").set_text(manager["search"][:200])

    @staticmethod
    def _restore_view(stack, state, keys):
        saved = mapping(state.get("map"))
        if saved:
            stack.map.set_center_and_zoom(
                number(saved.get("latitude"), 39, -85, 85),
                number(saved.get("longitude"), 9, -180, 180),
                number(saved.get("zoom"), 6, 1, 20),
            )
        toggles = mapping(state.get("toggles"))
        for key in keys:
            if isinstance(toggles.get(key), bool):
                stack.builder.get_object(key).set_active(toggles[key])

    def _restore_polars(self, state):
        stack = self.app.polarStack
        for row in stack.polar_list_store:
            if row[0] == state.get("selected"):
                stack.polar_list.set_cursor(row.path)
                break
        if "table_width" in state:
            stack.tool_dock._restore_split(
                stack.body, int(number(state["table_width"], 500, 0, 20000))
            )
        stack.performance.restore_state(state.get("performance"))

    def save(self):
        try:
            self.store.save(self.save_state())
        except (OSError, ValueError) as exc:
            logger.warning("Could not save workspace preferences: %s", exc)

    def close(self):
        if self._closed:
            return
        self.save()
        self._destroyed()

    def _destroyed(self, *_):
        self._closed = True
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
        for widget, handler in self._connections:
            if widget.handler_is_connected(handler):
                widget.disconnect(handler)
        self._connections.clear()
