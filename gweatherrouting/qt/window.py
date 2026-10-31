"""Small Qt frontend for evaluating NMEA, plotting, docking, and map integration."""

import os
import sys
from bisect import bisect_right
from datetime import timedelta, timezone
from html import escape

from PySide6.QtCore import QEvent, Qt, QTimer, QSize, QSignalBlocker
from PySide6.QtGui import (
    QAction,
    QColor,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTabBar,
    QSplitter,
    QScrollArea,
    QScrollBar,
    QFrame,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
)

from gweatherrouting.core.timecontrol import TimeControl, naive_wall_time
from gweatherrouting.core.n2kdevices import source_label
from gweatherrouting.common import resource_path
from .theme import section_label, symbol_icon
from .dockchrome import style_dock, style_scrollarea
from .viewportmap import ViewportMap
from .routingpanel import RoutingPanel
from .settingsdialog import SettingsDialog
from .datalibrary import DataLibrary
from .plotdata import elapsed_offsets
from .workspace import WorkspacePreferences
from .polarworkspace import PolarWorkspace
from .polarlibrary import PolarLibrary
from .navigationpanel import NavigationPanel
from .racepanel import RacesPanel
from .marinepanel import MarinePanel
from .weatherpanel import WeatherPanel
from .modelregion import region_from_visible_workspace
from .chartdebugpanel import ChartDebugPanel
from .cursorstatus import CursorStatus
from .marinesettings import MarineSettings
from .loglibrary import (
    LogImportProgress,
    LogLibraryDialog,
    dropped_log_paths,
)


class NavigationScroll(QScrollArea):
    def minimumSizeHint(self):  # noqa: N802 - Qt virtual method name
        return QSize(0, 0)


class PreviewWindow(QMainWindow):
    def __init__(self, controller, workspace_path=None):
        super().__init__()
        self.setAutoFillBackground(True)
        self.setDockOptions(
            self.dockOptions() | QMainWindow.DockOption.AllowNestedDocks
        )
        self.controller = controller
        self.session = controller.session
        self.syncing = False
        self._drawer_collapsed = {"left": False, "right": False}
        self._drawer_saved = {"left": [], "right": []}
        self._drawer_syncing = False
        self._instruments_syncing = False
        self.drawer_flags = {}
        self.data_key = None
        self._plot_log_generation = None
        self.settings_dialog = None
        self.library_dialog = None
        self.log_library_dialog = None
        self.setAcceptDrops(True)
        self.marine_settings = None
        self.marine_open = {"currents": False, "tides": False}
        self.marine_syncing = False
        self.setWindowTitle("GWeatherRouting")
        self.setWindowIcon(QIcon(resource_path("gweatherrouting", "data/icon.png")))
        self.resize(1200, 800)
        if sys.platform == "darwin":
            self.setUnifiedTitleAndToolBarOnMac(True)
        self.map = ViewportMap(controller, self, show_timeline=True)
        self.timeline = self.map.timeline
        header = QToolBar("Workspaces", self)
        header.setObjectName("workspaces")
        self.workspace_toolbar = header
        header.setMovable(False)
        self.addToolBar(header)
        self.workspace_tabs = QTabBar()
        self.workspace_tabs.setExpanding(False)
        for name in ("Charts", "Logs", "Polars"):
            self.workspace_tabs.addTab(name)
        header.addWidget(self.workspace_tabs)
        self.plots = self._build_plots(controller)
        self.log_time = TimeControl()
        self.log_map = ViewportMap(controller, self, time_control=self.log_time)
        self.log_plot_panel = QWidget(self)
        plot_layout = QVBoxLayout(self.log_plot_panel)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        plot_layout.addWidget(self.plots, 1)
        plot_controls = QHBoxLayout()
        self.undo_log_edit = QPushButton("Undo")
        self.undo_log_edit.setEnabled(False)
        self.undo_log_edit.clicked.connect(controller.undo_log_edit)
        plot_controls.addWidget(self.undo_log_edit)
        self.save_log_edit = QPushButton("Save edits")
        self.save_log_edit.setEnabled(False)
        self.save_log_edit.clicked.connect(self._save_log_edits)
        plot_controls.addWidget(self.save_log_edit)
        self.log_viewport = QScrollBar(Qt.Orientation.Horizontal)
        self.log_viewport.setRange(0, 10000)
        self.log_viewport.setEnabled(False)
        self.log_viewport.valueChanged.connect(
            lambda value: self.plots.set_viewport_fraction(value / 10000)
        )
        plot_controls.addWidget(self.log_viewport, 1)
        plot_layout.addLayout(plot_controls)
        self.plots.set_viewport_callback(self._plot_viewport_changed)
        self.logs_view = QSplitter(Qt.Orientation.Vertical)
        self.logs_view.addWidget(self.log_map)
        self.logs_view.addWidget(self.log_plot_panel)
        self.logs_view.setSizes([350, 300])
        self.logs_view.setCollapsible(0, False)
        self.logs_view.setCollapsible(1, False)
        self.polar_workspace = PolarWorkspace(controller, self)
        self.views = QTabWidget()
        self.views.setDocumentMode(True)
        self.views.addTab(self.map, "Charts")
        self.views.addTab(self.logs_view, "Logs")
        self.views.addTab(self.polar_workspace, "Polars")
        self.views.tabBar().hide()
        self.workspace_tabs.currentChanged.connect(self.views.setCurrentIndex)
        self.views.currentChanged.connect(self.workspace_tabs.setCurrentIndex)
        self.setCentralWidget(self.views)
        self.nmea_dock = QDockWidget("NMEA", self)
        self.nmea_dock.setObjectName("nmea")
        style_dock(self.nmea_dock)
        self.nmea_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        nmea_scroll = QScrollArea()
        style_scrollarea(nmea_scroll)
        nmea_scroll.setWidgetResizable(True)
        nmea_scroll.setFrameShape(QFrame.Shape.NoFrame)
        nmea_scroll.setWidget(self._nmea_panel())
        self.nmea_dock.setWidget(nmea_scroll)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.nmea_dock)
        self.routing_panel = RoutingPanel(controller)
        controller.polars.changed.connect(self.routing_panel.refresh_polars)
        controller.polars.use_requested.connect(self._use_polar)
        self.routing_dock = QDockWidget("Routing", self)
        self.routing_dock.setObjectName("routing")
        style_dock(self.routing_dock)
        self.routing_dock.setWidget(self.routing_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.routing_dock)
        self.tabifyDockWidget(self.nmea_dock, self.routing_dock)
        self.undo_stack = QUndoStack(self)
        self.navigation_panel = NavigationPanel(
            controller, self.map, self, undo_stack=self.undo_stack
        )
        navigation_scroll = NavigationScroll()
        style_scrollarea(navigation_scroll)
        navigation_scroll.setWidgetResizable(True)
        navigation_scroll.setFrameShape(QFrame.Shape.NoFrame)
        navigation_scroll.setWidget(self.navigation_panel)
        self.navigation_dock = self._dock(
            "Navigation", "chart-navigation", navigation_scroll
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.navigation_dock)
        self.resizeDocks([self.navigation_dock], [373], Qt.Orientation.Horizontal)
        self.navigation_open = True
        self.navigation_syncing = False
        self.navigation_dock.visibilityChanged.connect(self._navigation_visibility)
        self.navigation_dock.toggleViewAction().triggered.connect(
            self._toggle_navigation
        )
        self.navigation_panel.changed.connect(self._navigation_changed)
        self.navigation_panel.route_selected.connect(self._navigation_route_selected)
        self.navigation_panel.move_requested.connect(self._move_navigation_point)
        self.navigation_panel.ais_updated.connect(self._ais_updated)
        self._ais_updated(tuple(self.navigation_panel.ais_targets))
        self._race_track_times = {}
        self._race_boat_positions = []
        self._race_timeline_bounds = None
        self.race_panel = RacesPanel(controller)
        self.race_panel.tracks_changed.connect(self._race_tracks_changed)
        self.race_panel.race_selected.connect(self._race_selected)
        controller.display_forecast.changed.connect(self._apply_race_timeline_bounds)
        controller.time_control.connect("time_change", self._race_time_changed)
        # RacesPanel loads persisted races in its constructor, before these
        # window signals can be connected. Reapply the loaded race/track state.
        self._race_tracks_changed()
        if getattr(self.race_panel, "race", None) is not None:
            self._race_selected(self.race_panel.race)
        race_scroll = NavigationScroll()
        style_scrollarea(race_scroll)
        race_scroll.setWidgetResizable(True)
        race_scroll.setFrameShape(QFrame.Shape.NoFrame)
        race_scroll.setWidget(self.race_panel)
        self.race_dock = self._dock("Races", "races", race_scroll)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.race_dock)
        self.race_dock.hide()
        self.map.navigation_request.connect(self._navigation_request)
        self.log_map.navigation_request.connect(self._navigation_request)
        self.setTabPosition(
            Qt.DockWidgetArea.AllDockWidgetAreas, QTabWidget.TabPosition.North
        )
        self.polar_selector_dock = self._dock(
            "Polar selector", "polar-selector", self.polar_workspace.selector
        )
        self.performance_dock = self._dock(
            "Performance", "nmea-performance", self.polar_workspace.performance
        )
        self.tabifyDockWidget(self.nmea_dock, self.polar_selector_dock)
        self.tabifyDockWidget(self.nmea_dock, self.performance_dock)
        self.polar_manager_panel = PolarLibrary(controller, self)
        self.polar_manager_dock = self._dock(
            "Polar Manager", "polar-manager", self.polar_manager_panel
        )
        self.tabifyDockWidget(self.nmea_dock, self.polar_manager_dock)
        self.polar_manager_dock.hide()
        self.polar_selector_dock.hide()
        self.performance_dock.hide()
        self.polars_visited = False
        self.nmea_dock.raise_()
        self.resizeDocks(
            [self.nmea_dock, self.routing_dock], [350, 350], Qt.Orientation.Horizontal
        )
        file_menu = self.menuBar().addMenu("File")
        edit_menu = self.menuBar().addMenu("Edit")
        self.undo_action = self.undo_stack.createUndoAction(self, "Undo")
        self.undo_action.setShortcut(QKeySequence("Ctrl+Z"))
        self.undo_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.redo_action = self.undo_stack.createRedoAction(self, "Redo")
        self.redo_action.setShortcut(QKeySequence("Ctrl+Shift+Z"))
        self.redo_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        open_log = file_menu.addAction(
            symbol_icon("document-open-symbolic"),
            "Open NMEA log…",
            self.show_log_library,
        )
        open_log.setShortcut(QKeySequence.StandardKey.Open)
        file_menu.addAction(
            symbol_icon("weather-windy-symbolic"),
            "Open forecast…",
            self.show_library,
        )
        file_menu.addAction("Data Library…", self.show_library)
        file_menu.addAction("Polars", self.show_polar_workspace)
        file_menu.addSeparator()
        file_menu.addAction("Close window", self.close).setShortcut(
            QKeySequence.StandardKey.Close
        )
        self.settings_action = file_menu.addAction("Settings…", self.show_settings)
        self.settings_action.setMenuRole(QAction.MenuRole.PreferencesRole)
        self.settings_action.setShortcut(QKeySequence.StandardKey.Preferences)
        self._create_marine_panels(controller)
        self.weather_open = False
        self.weather_syncing = False
        self.weather_panel = WeatherPanel(
            controller, self._active_map, self.show_library
        )
        self.weather_dock = self._dock("Weather", "weather", self.weather_panel)
        self.tabifyDockWidget(self.nmea_dock, self.weather_dock)
        self.weather_dock.hide()
        self.weather_dock.visibilityChanged.connect(self._weather_visibility)
        self.weather_dock.toggleViewAction().triggered.connect(
            lambda checked: self._show_panel(self.weather_dock) if checked else None
        )
        self.chart_debug_panel = ChartDebugPanel(
            controller, self._active_map, (self.map, self.log_map)
        )
        self.chart_debug_dock = self._dock(
            "Chart Debug", "chart-debug", self.chart_debug_panel
        )
        self.chart_debug_dock.hide()
        self._create_drawer_flags()
        for dock in self._drawer_dock_pool():
            dock.installEventFilter(self)
            dock.toggleViewAction().triggered.connect(
                lambda checked, dock=dock: self._menu_dock_toggled(dock, checked)
            )
        view_menu = self.menuBar().addMenu("View")
        view_menu.addAction(self.nmea_dock.toggleViewAction())
        view_menu.addAction(self.routing_dock.toggleViewAction())
        view_menu.addAction(self.polar_selector_dock.toggleViewAction())
        view_menu.addAction(self.performance_dock.toggleViewAction())
        view_menu.addAction(self.polar_manager_dock.toggleViewAction())
        view_menu.addAction(self.navigation_dock.toggleViewAction())
        view_menu.addAction(self.race_dock.toggleViewAction())
        view_menu.addAction(self.weather_dock.toggleViewAction())
        view_menu.addAction(self.chart_debug_dock.toggleViewAction())
        view_menu.addAction(header.toggleViewAction())
        self.ais_action = view_menu.addAction("AIS targets")
        self.ais_action.setCheckable(True)
        self.ais_action.setChecked(True)
        self.ais_action.toggled.connect(self._toggle_ais)
        self.dashboard_action = view_menu.addAction("Dashboard")
        self.dashboard_action.setCheckable(True)
        self.dashboard_action.toggled.connect(self._toggle_instruments)
        self.map_display_actions = (self.ais_action, self.dashboard_action)
        window_menu = self.menuBar().addMenu("Window")
        window_menu.addSection("Panels")
        window_menu.addAction(self.navigation_dock.toggleViewAction())
        window_menu.addAction(self.race_dock.toggleViewAction())
        window_menu.addAction(self.weather_dock.toggleViewAction())
        window_menu.addAction(self.nmea_dock.toggleViewAction())
        window_menu.addAction(self.routing_dock.toggleViewAction())
        window_menu.addAction(self.polar_selector_dock.toggleViewAction())
        window_menu.addAction(self.performance_dock.toggleViewAction())
        window_menu.addAction(self.polar_manager_dock.toggleViewAction())
        for dock in self.marine_docks.values():
            window_menu.addAction(dock.toggleViewAction())
            view_menu.addAction(dock.toggleViewAction())
        window_menu.addSeparator()
        window_menu.addAction("Reset Panels", self.reset_panels)
        file_menu.addAction("Import GPX…", self.navigation_panel.import_gpx)
        file_menu.addAction("Export navigation…", self._export_navigation)
        fit_menu = self.menuBar().addMenu("Navigate")
        fit_menu.addAction("Fit NMEA track", self._fit_track)
        fit_menu.addAction("Fit route", self._fit_route)
        self.views.currentChanged.connect(self._view_changed)
        self.workspace_toolbar.visibilityChanged.connect(self._sync_timeline)
        self._view_changed()
        self.routing_panel.plan_changed.connect(self.map.set_route_plan)
        controller.routing.completed.connect(self._routing_completed)
        self.map.route_location.connect(self.routing_panel.set_location)
        controller.routing.preview_changed.connect(self.map.set_route_preview)
        controller.routing.isochrones_changed.connect(self.map.set_isochrones)
        controller.routing.changed.connect(self._routing_changed)
        self.routing_panel._plan_changed()
        self._select_saved_route(
            controller.core.routingManager.get_active()
            or next(iter(controller.core.routingManager), None)
        )
        self.statusBar().setContentsMargins(12, 4, 12, 4)
        self.instruments = QLabel()
        self.cursor_status = CursorStatus()
        self.statusBar().addPermanentWidget(self.cursor_status, 1)
        for canvas in (self.map, self.log_map):
            canvas.cursor_readout.connect(
                lambda text, canvas=canvas: self._cursor_readout(canvas, text)
            )
        self.statusBar().addPermanentWidget(self.instruments)
        self.log_import_progress = LogImportProgress(controller, self)
        self.statusBar().addPermanentWidget(self.log_import_progress)
        controller.boat_changed.connect(self._boat_changed)
        controller.changed.connect(self.refresh)
        controller.connection_task.changed.connect(self.refresh)
        controller.model_changed.connect(self._refresh_data)
        self.live_timer = QTimer(self)
        self.live_timer.setInterval(500)
        self.live_timer.timeout.connect(self._refresh_data)
        self.live_timer.start()
        self.refresh()
        self.workspace = WorkspacePreferences(self, workspace_path)
        self._show_startup_status(controller)
        view_menu.addSeparator()
        view_menu.addAction("Reset layout", self.workspace.reset_layout)
        log_menu = self.menuBar().addMenu("Logs")
        log_menu.addAction("Log library…", self.show_log_library)
        QTimer.singleShot(0, self._offer_map_data)

    def _show_startup_status(self, controller):
        """Report staged chart initialization while it is pending."""
        startup = getattr(controller, "chart_startup", None)
        if startup is None:
            return
        self.startup_status = QLabel(startup.message)
        self.statusBar().addPermanentWidget(self.startup_status)
        startup.changed.connect(lambda: self.startup_status.setText(startup.message))
        startup.finished.connect(self._offer_map_data)

    def _build_plots(self, controller):
        """Build the log plot canvas, deferring Matplotlib when it is unused."""
        if getattr(controller, "early_weather", False):
            from .lazyplots import LazyLogPlots

            return LazyLogPlots(
                self,
                factory=lambda parent: self._interactive_plots(parent, controller),
            )
        return self._interactive_plots(self, controller)

    @staticmethod
    def _interactive_plots(parent, controller):
        from .plots import LogPlots

        return LogPlots(
            parent,
            preview=controller.preview_log_position,
            seek=controller.seek_log_position,
            delete_range=getattr(controller, "delete_log_range", None),
            undo=getattr(controller, "undo_log_edit", None),
        )

    def _plot_viewport_changed(self, fraction):
        if not hasattr(self, "log_viewport"):
            return
        with QSignalBlocker(self.log_viewport):
            self.log_viewport.setValue(round(fraction * self.log_viewport.maximum()))

    def _save_log_edits(self):
        try:
            if self.controller.save_log_edits():
                self.statusBar().showMessage("Saved edited log", 3000)
        except Exception as exc:
            QMessageBox.warning(self, "Unable to save log", str(exc))

    def _offer_map_data(self):
        if (
            not self.controller.closed
            and self.controller.load_charts
            and (
                self.controller.chart_startup is None
                or self.controller.chart_startup.state == "ready"
            )
            and any(self.controller.map_data.missing().values())
        ):
            self._ensure_library(chart_section=2)

    def _create_marine_panels(self, controller):
        self.marine_panels = {}
        self.marine_docks = {}
        for kind, title in (("currents", "Currents"), ("tides", "Tides")):
            panel = MarinePanel(
                controller,
                self.map,
                kind,
                self.show_marine_settings,
                self.show_marine_library,
            )
            dock = self._dock(title, kind, panel)
            self.marine_panels[kind], self.marine_docks[kind] = panel, dock
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
            self.tabifyDockWidget(self.nmea_dock, dock)
            dock.hide()
            dock.visibilityChanged.connect(self._marine_visibility)
            dock.toggleViewAction().triggered.connect(
                lambda checked, dock=dock: self._show_panel(dock) if checked else None
            )

        self.nmea_dock.raise_()

    @staticmethod
    def _set_chart_toolbar_icons(toolbar):
        icons = {
            "Import…": "document-open-symbolic",
            "Export…": "document-save-symbolic",
            "Tabs": "compass",
            "AIS": "ais-ship-symbolic",
            "Weather": "weather-windy-symbolic",
            "Dashboard": "eye-open-negative-filled-symbolic",
            "NMEA": "gnome-dev-wavelan",
            "Routing": "routing-symbolic",
            "Fit view": "view-fullscreen-symbolic",
        }
        for action in toolbar.actions():
            if action.text() in icons:
                action.setIcon(symbol_icon(icons[action.text()]))
                if not action.toolTip():
                    action.setToolTip(action.text())

    def _dock(self, title, name, content):
        dock = QDockWidget(title, self)
        dock.setObjectName(name)
        style_dock(dock)
        if isinstance(content, QScrollArea):
            style_scrollarea(content)
        dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        dock.setWidget(content)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        return dock

    def _drawer_dock_pool(self):
        return (
            self.navigation_dock,
            self.race_dock,
            self.nmea_dock,
            self.routing_dock,
            self.polar_selector_dock,
            self.performance_dock,
            self.polar_manager_dock,
            self.weather_dock,
            self.chart_debug_dock,
            *self.marine_docks.values(),
        )

    def _drawer_side(self, dock):
        if dock.isFloating():
            return None
        area = self.dockWidgetArea(dock)
        if area == Qt.DockWidgetArea.LeftDockWidgetArea:
            return "left"
        if area == Qt.DockWidgetArea.RightDockWidgetArea:
            return "right"
        return None

    def _drawer_docks(self, side):
        area = (
            Qt.DockWidgetArea.LeftDockWidgetArea
            if side == "left"
            else Qt.DockWidgetArea.RightDockWidgetArea
        )
        return [
            dock
            for dock in self._drawer_dock_pool()
            if not dock.isFloating() and self.dockWidgetArea(dock) == area
        ]

    def _create_drawer_flags(self):
        for side in ("left", "right"):
            flag = QToolButton(self)
            flag.setObjectName("drawer-flag-" + side)
            flag.setAutoRaise(True)
            flag.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            flag.setCursor(Qt.CursorShape.PointingHandCursor)
            flag.setFixedSize(18, 64)
            flag.clicked.connect(
                lambda checked=False, side=side: self._toggle_drawer(side)
            )
            self.drawer_flags[side] = flag
        self._position_drawer_flags()
        QTimer.singleShot(0, self._position_drawer_flags)

    def _visible_drawer_docks(self, side):
        return [dock for dock in self._drawer_docks(side) if not dock.isHidden()]

    def _drawer_closed(self, side):
        return self._drawer_collapsed[side] or not self._visible_drawer_docks(side)

    def _update_drawer_flags(self):
        for side, flag in self.drawer_flags.items():
            closed = self._drawer_closed(side)
            if side == "left":
                flag.setText("›" if closed else "‹")
                flag.setToolTip(
                    "Expand left panels" if closed else "Collapse left panels"
                )
            else:
                flag.setText("‹" if closed else "›")
                flag.setToolTip(
                    "Expand right panels" if closed else "Collapse right panels"
                )

    def _position_drawer_flags(self):
        if not self.drawer_flags:
            return
        self._update_drawer_flags()
        central = self.views.geometry()
        top = central.top() + max(0, (central.height() - 64) // 2)
        for side, flag in self.drawer_flags.items():
            if side == "left":
                x = central.left()
            else:
                x = central.right() + 1 - 18
            x = max(0, min(self.width() - 18, x))
            flag.setGeometry(x, top, 18, 64)
            flag.show()
            flag.raise_()

    def _toggle_drawer(self, side):
        if self._drawer_closed(side):
            self._expand_drawer(side)
        else:
            self._collapse_drawer(side)

    def _collapse_drawer(self, side):
        if self._drawer_collapsed[side]:
            return
        docks = self._drawer_docks(side)
        visible = self._visible_drawer_docks(side)
        if not visible:
            return
        self._drawer_saved[side] = visible
        self._drawer_collapsed[side] = True
        self._drawer_syncing = True
        try:
            for dock in docks:
                dock.hide()
        finally:
            self._drawer_syncing = False
        self._position_drawer_flags()
        QTimer.singleShot(0, self._position_drawer_flags)

    def _expand_drawer(self, side):
        if not self._drawer_closed(side):
            return
        saved = self._drawer_saved[side]
        if not saved:
            saved = [self.navigation_dock] if side == "left" else [self.nmea_dock]
        self._drawer_collapsed[side] = False
        self._drawer_saved[side] = []
        self._drawer_syncing = True
        try:
            if side == "left":
                self.navigation_open = True
                self.views.setCurrentWidget(self.map)
            for dock in saved:
                dock.show()
            saved[-1].raise_()
        finally:
            self._drawer_syncing = False
        self._position_drawer_flags()
        QTimer.singleShot(0, self._position_drawer_flags)

    def _menu_dock_toggled(self, dock, checked):
        if checked:
            side = self._drawer_side(dock)
            if side is not None:
                self._expand_drawer(side)
            dock.show()
            dock.raise_()

    def resizeEvent(self, event):  # noqa: N802 - Qt virtual method name
        super().resizeEvent(event)
        self._position_drawer_flags()

    def eventFilter(self, watched, event):  # noqa: N802 - Qt virtual method name
        if isinstance(watched, QDockWidget) and event.type() in (
            QEvent.Type.Move,
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.Hide,
        ):
            QTimer.singleShot(0, self._position_drawer_flags)
        return super().eventFilter(watched, event)

    def _sync_timeline(self, *_):
        if self.map.timeline is not None:
            self.map.timeline.setVisible(
                self.views.currentWidget() is self.map
                and not self.workspace_toolbar.isHidden()
            )

    def _order_toolbars(self):
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.workspace_toolbar)
        self._sync_timeline()

    def _view_changed(self, *_):
        current = self.views.currentWidget()
        self._sync_timeline()
        self.weather_syncing = True
        self.weather_dock.setVisible(
            current in (self.map, self.logs_view)
            and self.weather_open
            and not self._drawer_collapsed["right"]
        )
        self.weather_syncing = False
        self.navigation_syncing = True
        self.navigation_dock.setVisible(
            current is self.map
            and self.navigation_open
            and not self._drawer_collapsed["left"]
        )
        self.navigation_syncing = False
        self.race_dock.setVisible(
            current in (self.map, self.logs_view) and not self._drawer_collapsed["left"]
        )
        self.marine_syncing = True
        for kind, dock in self.marine_docks.items():
            dock.setVisible(
                current is self.map
                and self.marine_open[kind]
                and not self._drawer_collapsed["right"]
            )
        self.marine_syncing = False
        self._sync_map_actions()
        if hasattr(self, "cursor_status"):
            self.cursor_status.clear()
        for bar in self.findChildren(QTabBar):
            if bar.parent() is self:
                bar.setElideMode(Qt.TextElideMode.ElideNone)
                bar.setUsesScrollButtons(True)
        if current is self.polar_workspace and not self.polars_visited:
            self.polars_visited = True
            self.polar_selector_dock.show()
            self.performance_dock.show()
            self.polar_selector_dock.raise_()

    def _toggle_log(self, key, checked):
        self.plots.set_series(key, checked)

    def _active_map(self):
        return (
            self.log_map if self.views.currentWidget() is self.logs_view else self.map
        )

    def _capture_analysis_region(self):
        return region_from_visible_workspace(
            self.views.currentIndex(), self.map, self.log_map
        )

    def _sync_map_actions(self):
        canvas = self._active_map()
        for widget, enabled in (
            (self.dashboard_action, canvas.dashboard_visible),
            (self.instruments_toggle, canvas.dashboard_visible),
            (self.ais_action, canvas.ais_visible),
        ):
            with QSignalBlocker(widget):
                widget.setChecked(enabled)

        self.weather_panel.refresh()

    def _weather_visibility(self, *_):
        if (
            not self.weather_syncing
            and not self._drawer_syncing
            and self.views.currentWidget() in (self.map, self.logs_view)
        ):
            self.weather_open = not self.weather_dock.isHidden()

    def _toggle_weather(self, enabled):
        self.weather_open = enabled
        if enabled:
            self._show_panel(self.weather_dock)
        else:
            self.weather_dock.hide()

    def _toggle_instruments(self, enabled):
        if self._instruments_syncing:
            return
        self._instruments_syncing = True
        try:
            self.dashboard_action.setChecked(enabled)
            self.instruments_toggle.setChecked(enabled)
            self._active_map().set_dashboard_visible(enabled)
        finally:
            self._instruments_syncing = False

    def _toggle_map_display(self, layer, enabled):
        canvas = self._active_map()
        if layer == "wind":
            canvas.set_wind_visible(enabled)
        else:
            canvas.set_dashboard_visible(enabled)

    def _cursor_readout(self, canvas, text):
        if (
            canvas is self._active_map()
            and self.views.currentWidget() is not self.polar_workspace
        ):
            self.cursor_status.set_readout(
                canvas._cursor_parts, canvas._cursor_coords if text else "", text
            )
            self.cursor_status.setToolTip(text)
            self.statusBar().showMessage("" if text else self._connection_message())

    def _fit_track(self):
        canvas = (
            self.log_map if self.views.currentWidget() is self.logs_view else self.map
        )
        canvas.fit_track()

    def _fit_route(self):
        self.views.setCurrentWidget(self.map)
        self.map.fit_route()

    def show_polar_workspace(self):
        self.views.setCurrentWidget(self.polar_workspace)
        self._show_panel(self.polar_selector_dock)

    def _select_saved_route(self, route):
        self.navigation_panel.select_route(route)
        self._navigation_route_selected(route)

    def _routing_completed(self, route):
        self._select_saved_route(route)
        self.map.fit_route()

    def _navigation_route_selected(self, route):
        coordinates = (
            tuple((p[0], p[1]) for p in route.points) if route is not None else ()
        )
        speeds = tuple(p[5] or 0 for p in route.points) if route is not None else ()
        self.map.set_route(coordinates, speeds)
        self.map.navigation.hl_routing = route.name if route is not None else None
        self._sync_route_isochrones()

    def _navigation_visibility(self, _):
        if (
            not self.navigation_syncing
            and not self._drawer_syncing
            and self.views.currentWidget() is self.map
        ):
            self.navigation_open = not self.navigation_dock.isHidden()

    def _toggle_navigation(self, checked):
        self.navigation_open = checked
        if checked:
            self._expand_drawer("left")
            self.views.setCurrentWidget(self.map)
        self.navigation_dock.setVisible(checked)

    def _race_selected(self, race):
        self._race_timeline_bounds = None
        if getattr(race, "start_time", None) and getattr(race, "end_time", None):
            self._race_timeline_bounds = (
                race.start_time.replace(tzinfo=timezone.utc).timestamp(),
                race.end_time.replace(tzinfo=timezone.utc).timestamp(),
                tuple(
                    p.timestamp.replace(tzinfo=timezone.utc).timestamp()
                    for boat in getattr(race, "boats", ())
                    for p in boat.positions
                ),
            )
        self._apply_race_timeline_bounds()

    def _apply_race_timeline_bounds(self):
        bounds = getattr(self, "_race_timeline_bounds", None)
        timeline = getattr(self.map, "timeline", None)
        if timeline is None:
            return
        if bounds is None:
            timeline.clear_bounds_override()
            return
        start, end, valid_times = bounds
        timeline.set_bounds_override(start, end, valid_times)

    def _race_tracks_changed(self):
        self._race_track_times = dict(getattr(self.race_panel, "race_track_times", {}))
        self._race_track_names = set(getattr(self.race_panel, "_race_track_names", []))
        self.navigation_panel.refresh()
        self._navigation_changed()
        self._race_time_changed(self.controller.time_control.time)

    def _race_time_changed(self, when):
        if not getattr(self, "_race_track_times", None):
            # No imported race is active; make sure stale boat markers disappear.
            self._race_boat_positions = []
            for canvas in (self.map, self.log_map):
                try:
                    canvas.race_boat_positions = []
                    canvas.race_positions = []
                except Exception:
                    pass
                try:
                    canvas.invalidate_navigation()
                except Exception:
                    pass
                try:
                    canvas.update()
                except Exception:
                    pass
            return
        active = self.controller.core.trackManager.get_active()
        if active is not None and active.name in self._race_track_times:
            offsets = self._race_track_times[active.name]
            if offsets:
                try:
                    position = when.replace(tzinfo=timezone.utc).timestamp()
                except Exception:
                    position = None
                if position is not None:
                    for canvas in (self.map, self.log_map):
                        canvas.offsets = tuple(offsets)
                        canvas.position = position
        # Keep every imported race boat visible at the scrubbed time, not just
        # the currently active track. Only show boats that still have
        # their tracks in the trackManager (user may have deleted some).
        try:
            position_ts = when.replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            position_ts = None
        race = getattr(getattr(self, "race_panel", None), "race", None)
        track_manager = getattr(getattr(self.controller, "core", None), "trackManager", None)
        positions = []
        if race is not None and track_manager is not None:
            try:
                when_dt = when
                # Build set of boat IDs that have visible tracks in trackManager
                visible_boat_ids = set()
                track_names = getattr(self, "_race_track_names", {})
                for track in track_manager:
                    if track.name in track_names:
                        # Find which boat this track belongs to
                        for boat in race.boats:
                            if f"{race.name} - {boat.name or boat.boat_id}" in track.name:
                                visible_boat_ids.add(boat.boat_id)
                                break
                for boat_id, boat_position in race.positions_at(when_dt).items():
                    if boat_id in visible_boat_ids:
                        positions.append(
                            [float(boat_position.latitude), float(boat_position.longitude)]
                        )
            except Exception:
                positions = []
        self._race_boat_positions = positions
        for canvas in (self.map, self.log_map):
            try:
                canvas.race_boat_positions = positions
                canvas.race_positions = positions
            except Exception:
                pass
            canvas.invalidate_navigation()
            canvas.update()

    def _navigation_changed(self):
        self.routing_panel.refresh_destinations()
        self._navigation_route_selected(self.navigation_panel.route)
        self.map.invalidate_navigation()
        self.log_map.invalidate_navigation()
        self.map.update()
        self.log_map.update()

    def _navigation_request(self, kind, coordinates):
        self._show_panel(self.navigation_dock)
        if kind == "poi":
            self.navigation_panel.add_poi(coordinates)
        else:
            self.navigation_panel.add_point(coordinates)

    def _new_track(self):
        self._show_panel(self.navigation_dock)
        self.navigation_panel.new_track()

    def _move_navigation_point(self, callback):
        self.views.setCurrentWidget(self.map)
        self.map.begin_move(callback)

    def _toggle_ais(self, visible):
        for canvas in (self.map, self.log_map):
            canvas.ais_visible = visible
            canvas.update()

    def _ais_updated(self, targets):
        self.log_map.ais_targets = targets
        self.log_map.update()

    def _export_navigation(self):
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        menu.addAction("Export GPX…", self.navigation_panel.export_all)
        menu.addAction("Export POIs as NMEA PFEC…", self.navigation_panel.export_pfec)
        menu.exec(
            self.navigation_dock.mapToGlobal(self.navigation_dock.rect().bottomLeft())
        )

    def _show_panel(self, dock):
        side = self._drawer_side(dock)
        if side is not None:
            self._expand_drawer(side)
        if dock is self.weather_dock:
            self.weather_open = True
            if self.views.currentWidget() not in (self.map, self.logs_view):
                self.views.setCurrentWidget(self.map)
        if dock is self.navigation_dock:
            self.navigation_open = True
            self.views.setCurrentWidget(self.map)
        for kind, marine_dock in self.marine_docks.items():
            if dock is marine_dock:
                self.marine_open[kind] = True
                self.views.setCurrentWidget(self.map)
        dock.show()
        if (
            dock in self.marine_docks.values() or dock is self.weather_dock
        ) and not dock.isFloating():
            # A pre-show raise can leave this dock last in sibling order while
            # another tab is active. Force a Z-order change so Qt selects it.
            dock.lower()
        dock.raise_()
        if hasattr(self, "workspace"):
            self.workspace._dock_activated(dock)
        if dock in (
            self.routing_dock,
            self.navigation_dock,
            *self.marine_docks.values(),
        ):
            self.views.setCurrentWidget(self.map)

    def _marine_visibility(self, *_):
        if (
            not self.marine_syncing
            and not self._drawer_syncing
            and self.views.currentWidget() is self.map
        ):
            self.marine_open = {
                kind: not dock.isHidden() for kind, dock in self.marine_docks.items()
            }

    def _toggle_marine(self, enabled):
        self.marine_open = {kind: enabled for kind in self.marine_docks}
        self._view_changed()
        if enabled:
            self._show_panel(self.marine_docks["currents"])

    def show_marine_settings(self, tab):
        if self.marine_settings is None:
            self.marine_settings = MarineSettings(self.controller, self)
        self.marine_settings.select_tab(tab)

    def show_marine_library(self, kind):
        self._ensure_library()
        assert self.library_dialog is not None
        self.library_dialog.show_marine(kind)

    def dispose_marine(self):
        self.marine_syncing = True
        if self.settings_dialog is not None:
            self.settings_dialog.currents.dispose()
        for panel in self.marine_panels.values():
            panel.dispose()
        if self.library_dialog is not None:
            self.library_dialog.dispose_marine()
        if self.marine_settings is not None:
            self.marine_settings.dispose()

    def reset_panels(self):
        """Redock every floating panel and restore default sizes/visibility."""
        for dock in (
            self.nmea_dock,
            self.routing_dock,
            self.polar_selector_dock,
            self.performance_dock,
            self.polar_manager_dock,
            self.navigation_dock,
            self.weather_dock,
            *self.marine_docks.values(),
        ):
            dock.setFloating(False)
        self.workspace.reset_layout()

    def _routing_changed(self):
        if self.controller.routing.busy:
            self.views.setCurrentWidget(self.map)
        else:
            self._sync_route_isochrones()

    def _sync_route_isochrones(self):
        if not self.controller.routing.busy:
            route = self.navigation_panel.route
            self.map.set_isochrones(
                route.isochrones if route is not None and route.visible else ()
            )

    def _fit_view(self):
        self.views.setCurrentWidget(self.map)
        if self.routing_dock.isVisible() and self.routing_panel.isVisible():
            self.map.fit_route()
        else:
            self.map.fit_track()

    def _nmea_panel(self):
        panel = QWidget()
        panel.setAutoFillBackground(True)
        panel.setMinimumWidth(250)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(section_label("Instruments"))
        self.instruments_toggle = QPushButton("Instruments")
        self.instruments_toggle.setObjectName("instruments-toggle")
        self.instruments_toggle.setCheckable(True)
        self.instruments_toggle.setAccessibleName("Instrument gauges")
        self.instruments_toggle.setToolTip("Show or hide instrument gauges on the map")
        self.instruments_toggle.toggled.connect(self._toggle_instruments)
        layout.addWidget(self.instruments_toggle)
        self.connection_status = QLabel()
        self.connection_status.setObjectName("connection-status")
        self.connection_status.setTextFormat(Qt.TextFormat.RichText)
        self.connection_status.setWordWrap(True)
        layout.addWidget(self.connection_status)
        self._connection_status_text = None
        self._refresh_connection_status()
        layout.addWidget(section_label("Data source"))
        self.source = QComboBox()
        self.source.addItems(["Live data source", "Loaded log"])
        self.source.currentIndexChanged.connect(self._source_changed)
        layout.addWidget(self.source)
        library = QPushButton("Log library…")
        library.clicked.connect(self.show_log_library)
        layout.addWidget(library)
        self.track_source_group = QWidget()
        track_layout = QVBoxLayout(self.track_source_group)
        track_layout.setContentsMargins(0, 0, 0, 0)
        track_layout.addWidget(QLabel("GPS track source"))
        self.track_source = QComboBox()
        self.track_source.setAccessibleName("GPS track source")
        self.track_source.currentIndexChanged.connect(self._track_source_changed)
        self._track_source_log = None
        track_layout.addWidget(self.track_source)
        layout.addWidget(self.track_source_group)
        self.record = QToolButton()
        self.record.setFixedSize(42, 34)
        self.record.setCheckable(True)
        self.record.setIconSize(QSize(24, 24))
        icon = QPixmap(24, 24)
        icon.fill(Qt.GlobalColor.transparent)
        painter = QPainter(icon)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#d74646"))
        painter.drawEllipse(5, 5, 14, 14)
        painter.end()
        self.record.setIcon(QIcon(icon))
        self.record.setAccessibleName("Record")
        self.record.setObjectName("record")
        self.record.setProperty("transport", True)
        self.record.clicked.connect(self._record)
        layout.addWidget(self.record)
        layout.addStretch()
        return panel

    def refresh(self):
        self.syncing = True
        session = self.session
        self.source.setCurrentIndex(0 if session.source == "live" else 1)
        self.source.model().item(1).setEnabled(session.log is not None)
        self.source.setEnabled(
            not session.loading and not self.controller.connection_task.busy
        )
        self.record.setChecked(session.recording)
        self.record.setVisible(session.source == "live" and session.streaming)
        self.record.setEnabled(
            session.recording
            or (session.source == "live" and session.streaming and not session.loading)
        )
        self.record.setToolTip(
            "Stop recording"
            if session.recording
            else "Start a new timestamped recording"
        )
        if getattr(self.controller, "log_edit_busy", False):
            message = "Deleting selected range…"
        elif getattr(self.controller, "log_edit_error", None):
            message = f"Unable to delete range: {self.controller.log_edit_error}"
        else:
            message = "" if self.cursor_status.text() else self._connection_message()
        self.statusBar().showMessage(message)
        self.syncing = False
        refresh_log_editor = getattr(self, "_refresh_log_editor_controls", None)
        if callable(refresh_log_editor):
            refresh_log_editor()
        self._refresh_data()
        self._refresh_log_position()

    def _refresh_log_editor_controls(self):
        session = self.session
        has_log = session.source == "log" and session.log is not None
        busy = getattr(self.controller, "log_edit_busy", False)
        self.undo_log_edit.setEnabled(
            has_log and bool(session._log_edit_undo) and not busy
        )
        self.save_log_edit.setEnabled(has_log and session.log_edits_dirty and not busy)
        self.log_viewport.setEnabled(has_log and session.log.duration > 0 and not busy)

    def _refresh_log_position(self):
        session = self.session
        position = session.display_position
        self.plots.set_position(position)
        self.map.set_position(position if session.source == "log" else None)
        self.log_map.set_position(position if session.source == "log" else None)
        if session.source == "log" and session.log and session.log.points:
            offsets = session.log.point_offsets
            if len(offsets) == len(session.log.points):
                point_index = max(0, bisect_right(offsets, position) - 1)
                self.log_time.set_time(
                    naive_wall_time(session.log.points[point_index].time)
                )
            else:
                offset = offsets[0] if offsets else 0
                self.log_time.set_time(
                    naive_wall_time(session.log.points[0].time)
                    + timedelta(seconds=position - offset)
                )

    def _connection_message(self):
        task = self.controller.connection_task
        return task.message if self.session.streaming and task.message != "Done" else ""

    def _connection_status_html(self):
        connections = self.controller.connections
        statuses = connections.get_status()
        if not connections.polling_active and not statuses:
            return '<span style="color:#8a8a8a;">●</span> Data off'
        if not statuses:
            return '<span style="color:#8a8a8a;">●</span> No connections'

        connected = sum(1 for status in statuses if status["connected"])
        total = len(statuses)
        packets = sum(status["packets"] for status in statuses)
        color = "#33e566" if connected == total else "#ff5544"
        rows = [
            f'<span style="color:{color};">●</span> {connected}/{total} connected'
            f'&nbsp;&nbsp;<span style="color:#66b3e6;">{packets:,} pkt</span>'
        ]
        for status in statuses:
            if not status["connected"]:
                name = escape(str(status.get("name", "Unknown source")))
                rows.append(f'<span style="color:#ff5544;">●</span> {name}')
        return "<br>".join(rows)

    def _refresh_connection_status(self):
        text = self._connection_status_html()
        if text != self._connection_status_text:
            self._connection_status_text = text
            self.connection_status.setText(text)

    def _refresh_data(self):
        self._refresh_connection_status()
        session = self.session
        log = session.log if session.source == "log" else None
        tracks = log.source_tracks if log else {}
        self.track_source_group.setVisible(bool(tracks))
        self.track_source.setEnabled(bool(tracks) and not session.loading)
        if log is not self._track_source_log:
            self._track_source_log = log
            with QSignalBlocker(self.track_source):
                self.track_source.clear()
                self.track_source.addItem("Auto (most fixes)", None)
                for source, track in tracks.items():
                    self.track_source.addItem(
                        f"{source_label(log.devices, source)} ({len(track.points):,} fixes)",
                        source,
                    )
        selected_source = self.track_source.currentData() if tracks else None
        key = (
            (session.source, id(session.log), selected_source)
            if session.source == "log"
            else ("live", self.controller.point_revision)
        )
        if key == self.data_key:
            return
        previous = self.data_key
        self.data_key = key
        points = self.controller.points
        offsets = (
            session.log.point_offsets
            if session.source == "log" and session.log
            else elapsed_offsets(points)
        )
        if selected_source in tracks:
            track = tracks[selected_source]
            points, offsets = track.points, track.point_offsets
        if session.source == "log" and session.log:
            reset_view = (
                previous is None
                or previous[0] != "log"
                or self._plot_log_generation != session._generation
            )
            self._plot_log_generation = session._generation
            self.plots.set_logged_fields(
                session.log.numeric_series, reset=reset_view, redraw=False
            )
        else:
            reset_view = previous is None or previous[0] != session.source
            self.plots.set_logged_fields((), reset=True, redraw=False)
        self.plots.set_points(points, offsets, session.source == "log")
        if session.source == "log" and session.log:
            self.plots.set_time_bounds(0.0, session.log.duration, reset=reset_view)
        else:
            self.plots.set_time_bounds(
                min(offsets, default=0.0),
                max(offsets, default=0.0),
                reset=reset_view,
            )
        if (
            session.source == "log"
            and session.log
            and previous is not None
            and previous[0] != session.source
        ):
            self.views.setCurrentWidget(self.logs_view)
        # Startup restores the exact saved camera. Fitting a preloaded track
        # before workspace restoration briefly sends the viewport to the track
        # bounds (often the placeholder pole), and later refreshes can replace
        # the user's last view. Only a deliberate source switch auto-fits.
        fit = bool(points) and previous is not None and previous[0] != session.source
        self.log_map.set_points(
            points,
            offsets,
            fit=fit,
        )
        self.map.set_points(
            points,
            offsets,
            fit=fit,
        )
        self._refresh_log_position()

    def _track_source_changed(self, _):
        self._refresh_data()

    def _source_changed(self, index):
        if self.syncing:
            return
        if index == 0:
            self.session.select_source("live")
            self.controller.set_live_reading(True)
        else:
            self.controller.set_live_reading(False)
            self.session.select_source("log")

    def _record(self, active):
        try:
            if active:
                self.session.start_recording()
            else:
                self.session.stop()
        except OSError as error:
            self.refresh()
            QMessageBox.critical(self, "Cannot record NMEA", str(error))

    def show_log_library(self):
        if self.log_library_dialog is None:
            self.log_library_dialog = LogLibraryDialog(self.controller, self)
        self.log_library_dialog.show()
        self.log_library_dialog.raise_()
        self.log_library_dialog.activateWindow()

    def dragEnterEvent(self, event):  # noqa: N802
        if not self.session.loading and dropped_log_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):  # noqa: N802
        self.dragEnterEvent(event)

    def dropEvent(self, event):  # noqa: N802
        paths = dropped_log_paths(event.mimeData())
        if paths and self.controller.load_paths(paths):
            event.acceptProposedAction()
            self.views.setCurrentWidget(self.logs_view)
            self._show_panel(self.nmea_dock)
        else:
            event.ignore()

    def _boat_changed(self, boat):
        if boat.is_valid():
            self.instruments.setText(
                f"{boat.latitude:.5f}°, {boat.longitude:.5f}°"
                + (f" · SOG {boat.sog:.1f} kn" if boat.sog is not None else "")
            )
        else:
            self.instruments.clear()

    def show_settings(self):
        if self.settings_dialog is None:
            self.settings_dialog = SettingsDialog(self.controller, self)
        elif (
            not self.settings_dialog.isVisible()
            and not self.controller.connection_task.busy
        ):
            self.settings_dialog.draft = [
                dict(c) for c in self.controller.connections.connections
            ]
            self.settings_dialog.reload()
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def _ensure_library(self, chart_section=None):
        if self.library_dialog is None:
            self.library_dialog = DataLibrary(
                self.controller,
                self,
                capture_region=self._capture_analysis_region,
            )
        if chart_section is not None:
            self.library_dialog.show_charts(chart_section)
        self.library_dialog.show()
        self.library_dialog.raise_()
        self.library_dialog.activateWindow()

    def show_library(self):
        self._ensure_library()

    def show_polars(self):
        self.show_polar_workspace()
        self._show_panel(self.polar_manager_dock)

    def _use_polar(self, name):
        index = self.routing_panel.polar.findText(name)
        if index >= 0:
            self.routing_panel.polar.setCurrentIndex(index)
            self.routing_panel.pages.setCurrentIndex(0)
            self._show_panel(self.routing_dock)
            if self.library_dialog is not None:
                self.library_dialog.hide()

    def closeEvent(self, event):  # noqa: N802 - Qt virtual method name
        for side in ("left", "right"):
            self._expand_drawer(side)
        self.workspace.close()
        if self.map.timeline is not None:
            self.map.timeline.dispose()
        self.dispose_marine()
        if hasattr(self, "weather_panel"):
            self.weather_panel.dispose()
        self.navigation_panel.dispose()
        self.polar_workspace.dispose()
        self.live_timer.stop()
        self.controller.close()
        event.accept()
