"""Debounced Qt view preferences, separate from GTK layout and NMEA history."""

import base64
import binascii
import logging
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QObject, QTimer
from PySide6.QtGui import QGuiApplication

from gweatherrouting.core.storage import DATA_DIR
from gweatherrouting.core.workspacestate import (
    WorkspaceStateStore,
    mapping,
    number,
    window_geometry,
)

logger = logging.getLogger("gweatherrouting")
LAYOUT_VERSION = 1


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
        self.active_dock = "nmea"
        self.last_saved = None
        self.default_layout = window.saveState(LAYOUT_VERSION)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(750)
        self.timer.timeout.connect(self._autosave)
        self.restore(self.store.load())
        self.restoring = False
        self._watch()

    @property
    def docks(self):
        return (self.window.nmea_dock, self.window.routing_dock)

    def _watch(self):
        window = self.window
        for widget in (window, *self.docks):
            widget.installEventFilter(self)
        for dock in self.docks:
            dock.dockLocationChanged.connect(self.changed)
            dock.topLevelChanged.connect(self.changed)
            dock.visibilityChanged.connect(self.changed)
        window.tabifiedDockWidgetActivated.connect(self._dock_activated)
        window.views.currentChanged.connect(self.changed)
        window.map.view_changed.connect(self.changed)
        panel = window.routing_panel
        for combo in (panel.polar, panel.algorithm, panel.routes):
            combo.currentIndexChanged.connect(self.changed)
        panel.coastlines.toggled.connect(self.changed)
        panel.pages.currentChanged.connect(self.changed)
        window.speed.currentIndexChanged.connect(self.changed)
        window.streaming.toggled.connect(self.changed)

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

    def _dock_activated(self, dock):
        if not self.restoring:
            self.active_dock = dock.objectName()
            self.changed()

    def changed(self, *_):
        if not self.closed and not self.restoring:
            self.timer.start()

    def _autosave(self):
        if self.window.map.drag is not None or self.window.map._zoom_animating:
            self.timer.start()
            return
        self.save()

    def save_state(self):
        window = self.window
        normal = window.normalGeometry()
        if not normal.isValid():
            normal = window.geometry()
        panel = window.routing_panel
        route = panel.routes.currentData()
        return {
            "window": {**geometry(normal), "maximized": window.isMaximized()},
            "layout": bytes(window.saveState(LAYOUT_VERSION).toBase64()).decode(
                "ascii"
            ),
            "docks": {
                dock.objectName(): {
                    "visible": not dock.isHidden(),
                    "floating": dock.isFloating(),
                    "geometry": geometry(dock.geometry()),
                }
                for dock in self.docks
            },
            "active_dock": self.active_dock,
            "view": window.views.tabText(window.views.currentIndex()),
            "map": {
                "latitude": window.map._center_lat,
                "longitude": (window.map._center_lon + 180) % 360 - 180,
                "zoom": window.map._zoom,
            },
            "routing": {
                "page": panel.pages.currentIndex(),
                "polar": panel.polar.currentText(),
                "algorithm": panel.algorithm.currentText(),
                "coastlines": panel.coastlines.isChecked(),
                "selected_route": route.name if route is not None else None,
            },
            "nmea": {
                "speed": window.session.speed,
                "streaming": window.session.streaming,
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
        self._restore_docks(state)
        self._restore_window(mapping(state.get("window")))
        view = state.get("view")
        for index in range(self.window.views.count()):
            if self.window.views.tabText(index) == view:
                self.window.views.setCurrentIndex(index)
        saved_map = mapping(state.get("map"))
        canvas = self.window.map
        canvas._center_lat = number(
            saved_map.get("latitude"), canvas._center_lat, -85, 85
        )
        canvas._center_lon = number(
            saved_map.get("longitude"), canvas._center_lon, -180, 180
        )
        canvas._zoom = number(saved_map.get("zoom"), canvas._zoom, 1, 20)
        canvas.queue_draw()
        self._restore_routing(mapping(state.get("routing")))
        nmea = mapping(state.get("nmea"))
        speed = nmea.get("speed")
        if type(speed) in (int, float) and speed in (0.5, 1, 2, 5, 10):
            self.window.session.speed = speed
        if isinstance(nmea.get("streaming"), bool):
            self.window.session.set_streaming(nmea["streaming"])
        self.window.refresh()

    def _restore_window(self, saved):
        if saved:
            fit_window(self.window, saved)
            self.maximize_on_show = saved.get("maximized") is True

    def _restore_docks(self, state):
        layout = state.get("layout")
        if isinstance(layout, str) and len(layout) <= 65536:
            try:
                decoded = base64.b64decode(layout, validate=True)
                if not self.window.restoreState(QByteArray(decoded), LAYOUT_VERSION):
                    self.window.restoreState(self.default_layout, LAYOUT_VERSION)
            except (ValueError, binascii.Error):
                pass
        saved_docks = mapping(state.get("docks"))
        for dock in self.docks:
            self._restore_dock(dock, mapping(saved_docks.get(dock.objectName())))
        active = state.get("active_dock")
        if active in ("nmea", "routing"):
            self.active_dock = active
            for dock in self.docks:
                if dock.objectName() == active and not dock.isHidden():
                    dock.raise_()

    @staticmethod
    def _restore_dock(dock, saved):
        if isinstance(saved.get("floating"), bool):
            dock.setFloating(saved["floating"])
        if dock.isFloating():
            fit_window(
                dock, mapping(saved.get("geometry")) or geometry(dock.geometry())
            )
        if isinstance(saved.get("visible"), bool):
            dock.setVisible(saved["visible"])

    def _restore_routing(self, saved):
        panel = self.window.routing_panel
        for combo, key in (
            (panel.polar, "polar"),
            (panel.algorithm, "algorithm"),
            (panel.routes, "selected_route"),
        ):
            text = saved.get(key)
            if isinstance(text, str):
                index = combo.findText(text)
                if index >= 0:
                    combo.setCurrentIndex(index)
        page = saved.get("page")
        if type(page) is int and 0 <= page < panel.pages.count():
            panel.pages.setCurrentIndex(page)
        if isinstance(saved.get("coastlines"), bool):
            panel.coastlines.setChecked(saved["coastlines"])

    def reset_layout(self):
        self.restoring = True
        try:
            self.window.restoreState(self.default_layout, LAYOUT_VERSION)
            self.window.views.setCurrentWidget(self.window.map)
            self.active_dock = "nmea"
            self.window.nmea_dock.raise_()
        finally:
            self.restoring = False
        self.changed()

    def close(self):
        if not self.closed:
            self.timer.stop()
            self.save()
            self.closed = True
