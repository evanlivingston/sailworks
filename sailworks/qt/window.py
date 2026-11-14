"""Small Qt frontend for evaluating NMEA, plotting, panels, and map integration."""

import sys
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from html import escape

from PySide6.QtCore import (
    Qt,
    QTimer,
    QSize,
    QSignalBlocker,
    QVariantAnimation,
)
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

from sailworks import build_label
from sailworks.core.timecontrol import (
    TimeControl,
    naive_wall_time,
    wall_to_utc,
)
from sailworks.core.n2kdevices import source_label
from sailworks.core.places import PlaceIndex, index_chart_files
from sailworks.core.scene import (
    KIND_LOG,
    KIND_POI,
    KIND_ROUTING,
    KIND_SHAPE,
    KIND_TRACK,
)
from sailworks.common import resource_path
from .theme import MenuBuilder, section_label, symbol_icon, ThemedMenu
from .dropdown import Dropdown
from .scrollchrome import style_scrollarea
from .collapsiblepanel import (
    PANEL_ANIMATION_MS,
    CollapsiblePanel,
    blender_panel_ease,
)
from .panelstack import PanelStack
from .viewportmap import ViewportMap
from .backgroundtask import BackgroundTask
from .placesearch import PlaceSearch
from .routingpanel import RoutingPanel
from .routinginspector import RoutingInspectorPanel
from .settingsdialog import SettingsDialog
from .datalibrary import DataLibrary
from .plotdata import elapsed_offsets
from .workspace import WorkspacePreferences
from .polarworkspace import PolarWorkspace
from .polarlibrary import PolarLibrary
from .navigationcommands import MovePointCommand, RemoveNavigationCommand
from . import navigationio
from .scenepanel import ScenePanel
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
from .text import qt_text


class NavigationScroll(QScrollArea):
    def minimumSizeHint(self):  # noqa: N802 - Qt virtual method name
        return QSize(0, 0)


def _routing_owns_route(route, native, active_route):
    """Whether a native overlay was presented for this route's result."""
    if route is None or native is None:
        return False
    summary = getattr(route, "result_summary", None)
    recorded = summary.get("calculation_id") if isinstance(summary, dict) else None
    if recorded is not None:
        return recorded == native.calculation_id
    # A result presented specifically for this route is trusted even when the
    # summary predates calculation ids.
    return active_route is route


def _routing_isochrone_mode(route, native, active_route):
    """Isochrone mode for the canvases, or None to leave them unchanged.

    An independently opened native result has no owning route, so its
    presented mode is preserved. Legacy routes and a missing native overlay
    stay uniform. Pure so partial window doubles need no extra bindings.
    """
    if native is None:
        return "uniform"
    if route is None:
        return None
    if _routing_owns_route(route, native, active_route):
        return getattr(route, "isochrone_colour_by", "uniform")
    return "uniform"


def _routing_presented_route(toggled, route, native, active_route):
    """Route whose isochrone flags govern the presented overlay.

    A Scene Collection toggle names the route it belongs to. That route wins
    whenever the native result was presented for it, because a saved route
    restored from the scene may not be the active selection. A route the
    overlay does not own never takes over the presented flags. Pure so partial
    window doubles need no extra bindings.
    """
    if toggled is None or toggled is route:
        return route
    if _routing_owns_route(toggled, native, active_route):
        return toggled
    return route


class PreviewWindow(QMainWindow):
    def __init__(self, controller, workspace_path=None):
        super().__init__()
        self.setAutoFillBackground(True)
        self.controller = controller
        self.session = controller.session
        self.syncing = False
        self._instruments_syncing = False
        self.drawer_flags = {}
        self.data_key = None
        self._plot_log_generation = None
        self.settings_dialog = None
        self.library_dialog = None
        self.log_library_dialog = None
        try:
            pending_era5 = (
                Path(controller.analysis.state.store.root) / "era5-pending.json"
            )
            if pending_era5.exists():
                QTimer.singleShot(0, self._resume_era5_downloads)
        except Exception:
            pass
        self.setAcceptDrops(True)
        self.marine_settings = None
        label = build_label()
        self.setWindowTitle(f"Sailworks — {label}" if label else "Sailworks")
        self.setWindowIcon(QIcon(resource_path("sailworks", "data/icon.png")))
        self.resize(1380, 800)
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
            self.workspace_tabs.addTab(qt_text(name))
        header.addWidget(self.workspace_tabs)
        self.place_index = PlaceIndex.from_default()
        self.place_search = PlaceSearch(
            self.place_index, near=self._place_near, parent=self
        )
        self.place_search.place_selected.connect(self._go_to_place)
        header.addSeparator()
        header.addWidget(self.place_search)
        self.place_index_task = BackgroundTask(self)
        self.place_index_task.finished.connect(self._chart_places_ready)
        self._indexed_chart_paths = frozenset()
        self._indexing_chart_paths = frozenset()
        self._chart_place_timer = QTimer(self)
        self._chart_place_timer.setSingleShot(True)
        self._chart_place_timer.setInterval(1500)
        self._chart_place_timer.timeout.connect(self._index_chart_places)
        controller.data.changed.connect(self._schedule_chart_places)
        self.plots = self._build_plots(controller)
        self.log_time = TimeControl()
        self.log_map = ViewportMap(controller, self, time_control=self.log_time)
        for canvas in (self.map, self.log_map):
            canvas.drawer_toggle_requested.connect(self._toggle_drawer)
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
        self.views.currentChanged.connect(lambda _: self._position_drawer_flags())
        self.setCentralWidget(self.views)
        self.nmea_scroll = QScrollArea()
        style_scrollarea(self.nmea_scroll)
        self.nmea_scroll.setWidgetResizable(True)
        self.nmea_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.nmea_panel = self._nmea_panel()
        self.nmea_scroll.setWidget(self.nmea_panel)
        self.routing_panel = RoutingPanel(controller)
        controller.polars.changed.connect(self.routing_panel.refresh_polars)
        controller.polars.use_requested.connect(self._use_polar)
        self.undo_stack = QUndoStack(self)
        # Both maps share one history, so a corner edit keeps one Ctrl-Z
        # sequence wherever the user made it.
        for canvas in (self.map, self.log_map):
            attach = getattr(canvas, "set_undo_stack", None)
            if callable(attach):
                attach(self.undo_stack)
        self.scene_panel = ScenePanel(controller, self, undo_stack=self.undo_stack)
        self.scene_panel.changed.connect(self._scene_changed)
        self.scene_panel.routing_visibility_changed.connect(
            self._routing_visibility_changed
        )
        self.scene_panel.selected.connect(self._scene_selected)
        self.scene_panel.open_requested.connect(self._scene_open)
        self.scene_panel.fit_requested.connect(self._scene_fit)
        self.scene_panel.delete_requested.connect(self._scene_delete)
        self.scene_panel.move_point_requested.connect(self._move_scene_point)
        controller.log_library_changed.connect(self.scene_panel.refresh)
        self.scene_panel.edit_track_requested.connect(self._scene_edit_track)
        self.scene_panel.shape_edit_requested.connect(self._scene_edit_shape_vertices)
        # The canvas republishes shapes after a create or a geometry edit, so
        # both the map and the log map keep their Scene rows in step. The
        # viewport ships the signal separately; older canvases just don't have
        # it, which is not an error here.
        for canvas in (self.map, self.log_map):
            shapes_changed = getattr(canvas, "shapes_changed", None)
            if shapes_changed is not None:
                shapes_changed.connect(self.scene_panel.refresh)
        self.map.track_point_move_requested.connect(self._move_scene_mark)
        self.map.track_edit_cancel_requested.connect(self._exit_track_edit)
        self.ais_timer = QTimer(self)
        self.ais_timer.setInterval(3000)
        self.ais_timer.timeout.connect(self._refresh_ais)
        self.ais_timer.start()
        self._refresh_ais()
        self._race_boat_positions = []
        self._race_timeline_bounds = None
        self._race_region_installed = False
        self.race_panel = RacesPanel(controller)
        self.race_panel.tracks_changed.connect(self._race_tracks_changed)
        self.race_panel.tracks_changed.connect(self.scene_panel.refresh)
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
        self.race_scroll = race_scroll
        self.map.navigation_request.connect(self._navigation_request)
        self.log_map.navigation_request.connect(self._navigation_request)
        self.polar_manager_panel = PolarLibrary(controller, self)
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
        file_menu.addAction(
            "Open native routing result…", self.show_native_routing_result
        )
        file_menu.addSeparator()
        file_menu.addAction("Close window", self.close).setShortcut(
            QKeySequence.StandardKey.Close
        )
        self.settings_action = file_menu.addAction("Settings…", self.show_settings)
        self.settings_action.setMenuRole(QAction.MenuRole.PreferencesRole)
        self.settings_action.setShortcut(QKeySequence.StandardKey.Preferences)
        self._create_routing_inspector()
        self._create_marine_panels(controller)
        self.weather_panel = WeatherPanel(
            controller, self._active_map, self.show_library
        )
        self.chart_debug_panel = ChartDebugPanel(
            controller, self._active_map, (self.map, self.log_map)
        )
        self._build_panel_stacks()
        self._create_drawer_flags()
        view_menu = self.menuBar().addMenu("View")
        for action in self.panel_actions.values():
            view_menu.addAction(action)
        view_menu.addAction(header.toggleViewAction())
        view_menu.addSeparator()
        view_menu.addAction(self.routing_inspector_dock.toggleViewAction())
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
        for action in self.panel_actions.values():
            window_menu.addAction(action)
        window_menu.addSeparator()
        window_menu.addAction("Reset Panels", self.reset_panels)
        file_menu.addAction("Import GPX…", lambda: navigationio.import_gpx(self))
        file_menu.addAction("Export navigation…", self._export_navigation)
        fit_menu = self.menuBar().addMenu("Navigate")
        fit_menu.addAction("Fit NMEA track", self._fit_track)
        fit_menu.addAction("Fit route", self._fit_route)
        fit_menu.addSeparator()
        find_place = fit_menu.addAction("Search places…", self._focus_place_search)
        find_place.setShortcut(QKeySequence.StandardKey.Find)
        self.views.currentChanged.connect(self._view_changed)
        self.workspace_toolbar.visibilityChanged.connect(self._sync_timeline)
        self._view_changed()
        self.routing_panel.plan_changed.connect(self.map.set_route_plan)
        controller.routing.completed.connect(self._routing_completed)
        controller.routing.native_diagnostic.connect(self._native_diagnostic)
        controller.routing.native_cleared.connect(self._native_cleared)
        self.map.route_location.connect(self.routing_panel.set_location)
        controller.routing.preview_changed.connect(self.map.set_route_preview)
        controller.routing.isochrones_changed.connect(self.map.set_isochrones)
        controller.routing.changed.connect(self._routing_changed)
        self.routing_panel._plan_changed()
        self._select_saved_route(
            controller.core.routingManager.get_active()
            or next(iter(controller.core.routingManager), None)
        )
        # Restoring a saved result presents it through the shared presenter,
        # which opens the dock. A launch should start with it closed; the
        # content stays loaded for when the user opens it from the View menu.
        self.routing_inspector_dock.hide()
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
        self._index_chart_places()

    def _build_panel_stacks(self):
        """Build the left and right Blender-style panel stacks."""
        left_stack = PanelStack(self)
        right_stack = PanelStack(self)
        for stack in (left_stack, right_stack):
            stack.setMinimumWidth(300)
            stack.setMaximumWidth(560)
        left_stack.set_peer(right_stack)
        right_stack.set_peer(left_stack)
        left_stack.changed.connect(self._panel_layout_changed)
        right_stack.changed.connect(self._panel_layout_changed)

        definitions = (
            ("left", "scene", "Scene", self.scene_panel),
            ("left", "races", "Races", self.race_scroll),
            ("right", "nmea", "NMEA", self.nmea_scroll),
            ("right", "routing", "Routing", self.routing_panel),
            ("right", "weather", "Weather", self.weather_panel),
            (
                "right",
                "polar-selector",
                "Polar selector",
                self.polar_workspace.selector,
            ),
            ("right", "performance", "Performance", self.polar_workspace.performance),
            ("right", "polar-manager", "Polar Manager", self.polar_manager_panel),
            ("right", "chart-debug", "Chart Debug", self.chart_debug_panel),
        )
        definitions += tuple(
            ("right", kind, kind.title(), panel)
            for kind, panel in self.marine_panels.items()
        )
        self.panels = {}
        self.panel_actions = {}
        self._panel_home = {}
        self._panel_titles = {}
        for side, name, title, content in definitions:
            wrapper = CollapsiblePanel(title, content)
            (left_stack if side == "left" else right_stack).add_panel(wrapper)
            self.panels[name] = wrapper
            self._panel_home[name] = side
            self._panel_titles[name] = title
            action = QAction(title, self)
            action.setCheckable(True)
            action.setChecked(True)
            action.toggled.connect(
                lambda checked, name=name: self._set_panel_visible(name, checked)
            )
            self.panel_actions[name] = action

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("panel-stack-splitter")
        splitter.setHandleWidth(3)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(left_stack)
        splitter.addWidget(self.views)
        splitter.addWidget(right_stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([480, 520, 380])
        self.setCentralWidget(splitter)
        self._panel_splitter = splitter
        self._panel_stacks = {"left": left_stack, "right": right_stack}
        self._panel_stack_width = {"left": 480, "right": 380}
        self._panel_stack_open = {"left": True, "right": True}
        self._panel_stack_animation = None
        self.panel_stack = right_stack
        self._default_panel_state = self.panel_state()
        self._position_drawer_flags()
        QTimer.singleShot(500, self._position_drawer_flags)
        QTimer.singleShot(2000, self._position_drawer_flags)

    def _panel_side(self, name):
        wrapper = self.panels.get(name)
        if wrapper is None:
            return None
        for side, stack in getattr(self, "_panel_stacks", {}).items():
            if wrapper in stack.panels():
                return side
        return None

    def _panel_visible(self, name):
        return self._panel_side(name) is not None

    def _set_panel_visible(self, name, visible):
        wrapper = self.panels.get(name)
        if wrapper is None:
            return
        side = self._panel_side(name)
        if visible and side is None:
            side = self._panel_home.get(name, "right")
            self._panel_stacks[side].add_panel(wrapper)
            self._open_panel_stack(side)
        elif not visible and side is not None:
            self._panel_stacks[side].remove_panel(wrapper)
        action = self.panel_actions.get(name)
        if action is not None:
            with QSignalBlocker(action):
                action.setChecked(visible)
        if visible:
            wrapper.set_collapsed(False)
            QTimer.singleShot(
                0,
                lambda stack=self._panel_stacks.get(side), panel=wrapper: (
                    stack.scroll.ensureWidgetVisible(panel)
                    if stack is not None
                    else None
                ),
            )
        self._position_drawer_flags()

    def _open_panel_stack(self, side):
        if side in self._panel_stacks and not self._panel_stack_open[side]:
            self._toggle_panel_stack(side)

    def _panel_layout_changed(self):
        for name, action in getattr(self, "panel_actions", {}).items():
            with QSignalBlocker(action):
                action.setChecked(self._panel_visible(name))
        self._position_drawer_flags()
        if hasattr(self, "workspace"):
            self.workspace.changed()

    def panel_state(self):
        panels = []
        for side, stack in self._panel_stacks.items():
            for wrapper in stack.panels():
                name = next(
                    name
                    for name, candidate in self.panels.items()
                    if candidate is wrapper
                )
                panels.append(
                    {
                        "name": name,
                        "side": side,
                        "visible": True,
                        "collapsed": wrapper.collapsed,
                    }
                )
        for name, wrapper in self.panels.items():
            if not self._panel_visible(name):
                panels.append(
                    {
                        "name": name,
                        "side": self._panel_home.get(name, "right"),
                        "visible": False,
                        "collapsed": wrapper.collapsed,
                    }
                )
        return {
            "splitter": self._panel_splitter.sizes(),
            "open": dict(self._panel_stack_open),
            "width": dict(self._panel_stack_width),
            "panels": panels,
        }

    def restore_panel_state(self, state):
        if not isinstance(state, dict):
            return
        for stack in self._panel_stacks.values():
            for wrapper in list(stack.panels()):
                stack.remove_panel(wrapper)
        placed = set()
        saved = state.get("panels")
        if isinstance(saved, list):
            for entry in saved:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                if name not in self.panels or name in placed:
                    continue
                side = entry.get("side")
                if side not in ("left", "right"):
                    side = self._panel_home.get(name, "right")
                wrapper = self.panels[name]
                if entry.get("visible") is not False:
                    self._panel_stacks[side].add_panel(wrapper)
                wrapper.set_collapsed(
                    bool(entry.get("collapsed", False)), animate=False
                )
                placed.add(name)
        for name, wrapper in self.panels.items():
            if name in placed:
                continue
            side = self._panel_home.get(name, "right")
            self._panel_stacks[side].add_panel(wrapper)
        for name in self.panels:
            action = self.panel_actions.get(name)
            if action is not None:
                with QSignalBlocker(action):
                    action.setChecked(self._panel_visible(name))

        sizes = state.get("splitter")
        if (
            isinstance(sizes, list)
            and len(sizes) == 3
            and all(isinstance(value, int) and value >= 0 for value in sizes)
        ):
            self._panel_splitter.setSizes(sizes)
        open_states = state.get("open")
        if isinstance(open_states, dict):
            self._panel_stack_open.update(
                {side: bool(open_states.get(side, True)) for side in ("left", "right")}
            )
        widths = state.get("width")
        if isinstance(widths, dict):
            for side in ("left", "right"):
                value = widths.get(side)
                if isinstance(value, int) and value > 0:
                    self._panel_stack_width[side] = value
        for side, stack in self._panel_stacks.items():
            opened = self._panel_stack_open[side]
            stack.setMinimumWidth(300 if opened else 0)
            stack.setVisible(opened)
        self._position_drawer_flags()

    def _show_startup_status(self, controller):
        """Report staged chart initialization while it is pending."""
        startup = getattr(controller, "chart_startup", None)
        if startup is None:
            return
        self.startup_status = QLabel(startup.message)
        self.statusBar().addPermanentWidget(self.startup_status)
        startup.changed.connect(lambda: self.startup_status.setText(startup.message))

    def _build_plots(self, controller):
        """Build the log plot canvas, deferring Matplotlib when it is unused."""
        from .lazyplots import LazyLogPlots

        return LazyLogPlots(
            self,
            factory=lambda parent: self._interactive_plots(parent, controller),
        )

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
                self.statusBar().showMessage("Saving edited log…", 3000)
        except Exception as exc:
            QMessageBox.warning(self, "Unable to save log", str(exc))

    def _create_marine_panels(self, controller):
        self.marine_panels = {}
        for kind in ("currents", "tides"):
            self.marine_panels[kind] = MarinePanel(
                controller,
                self.map,
                kind,
                self.show_marine_settings,
                self.show_marine_library,
            )

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

    def _position_drawer_flags(self):
        if not self.drawer_flags:
            return
        stacks = getattr(self, "_panel_stacks", {})
        states = getattr(self, "_panel_stack_open", {})
        left_stack = stacks.get("left")
        right_stack = stacks.get("right")
        left_open = states.get("left", True)
        right_open = states.get("right", True)
        origin = self.views.mapTo(self, self.views.rect().topLeft())
        top = origin.y() + max(0, (self.views.height() - 64) // 2)
        left = self.drawer_flags.get("left")
        if left is not None:
            left.setText("‹" if left_open else "›")
            left.setToolTip(
                "Collapse left panels" if left_open else "Expand left panels"
            )
            if left_stack is not None and left_stack.isVisible():
                x = (
                    left_stack.mapTo(self, left_stack.rect().topLeft()).x()
                    + left_stack.width()
                )
            else:
                x = origin.x()
            left.setGeometry(max(0, min(self.width() - 18, x)), top, 18, 64)
            left.show()
            left.raise_()
        right = self.drawer_flags.get("right")
        if right is not None:
            right.setText("‹" if right_open else "›")
            right.setToolTip(
                "Collapse right panels" if right_open else "Expand right panels"
            )
            if right_stack is not None and right_stack.isVisible():
                x = right_stack.mapTo(self, right_stack.rect().topLeft()).x() - 18
            else:
                x = origin.x() + self.views.width() - 18
            right.setGeometry(max(0, min(self.width() - 18, x)), top, 18, 64)
            right.show()
            right.raise_()
        for canvas in (getattr(self, "map", None), getattr(self, "log_map", None)):
            if canvas is not None:
                canvas.set_drawer_state(False, False, force=True)
                canvas.set_drawer_tabs_visible(False)

    def _toggle_drawer(self, side):
        self._toggle_panel_stack(side)

    def _toggle_panel_stack(self, side):
        stack = self._panel_stacks.get(side)
        splitter = getattr(self, "_panel_splitter", None)
        if stack is None or splitter is None:
            return
        if self._panel_stack_animation is not None:
            self._panel_stack_animation.stop()
            self._panel_stack_animation.deleteLater()
            self._panel_stack_animation = None
        opening = not self._panel_stack_open[side]
        self._panel_stack_open[side] = opening
        sizes = splitter.sizes()
        total = max(1, sum(sizes))
        if opening:
            stack.show()
            stack.setMinimumWidth(0)
            width = max(300, min(self._panel_stack_width.get(side, 300), total - 300))
            if side == "left":
                target = [width, max(0, total - width - sizes[2]), sizes[2]]
            else:
                target = [sizes[0], max(0, total - sizes[0] - width), width]
        else:
            self._panel_stack_width[side] = sizes[0] if side == "left" else sizes[2]
            stack.setMinimumWidth(0)
            if side == "left":
                target = [0, max(0, total - sizes[2]), sizes[2]]
            else:
                target = [sizes[0], max(0, total - sizes[0]), 0]
        animation = QVariantAnimation(self)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setDuration(PANEL_ANIMATION_MS)
        animation.valueChanged.connect(
            lambda progress: self._animate_panel_stack(sizes, target, progress)
        )
        animation.finished.connect(
            lambda: self._finish_panel_stack_toggle(side, opening, animation)
        )
        self._panel_stack_animation = animation
        animation.start()
        self._position_drawer_flags()

    def _animate_panel_stack(self, start, target, progress):
        eased = blender_panel_ease(progress)
        sizes = [
            round(start[index] + (target[index] - start[index]) * eased)
            for index in range(3)
        ]
        self._panel_splitter.setSizes(sizes)
        self._position_drawer_flags()

    def _finish_panel_stack_toggle(self, side, opening, animation):
        if self._panel_stack_animation is animation:
            self._panel_stack_animation = None
        animation.deleteLater()
        stack = self._panel_stacks.get(side)
        if stack is None:
            return
        if opening:
            stack.setMinimumWidth(300)
            stack.show()
        else:
            stack.hide()
            stack.setMinimumWidth(0)
        self._position_drawer_flags()
        QTimer.singleShot(0, self._position_drawer_flags)

    def resizeEvent(self, event):  # noqa: N802 - Qt virtual method name
        super().resizeEvent(event)
        self._position_drawer_flags()

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
        self._sync_timeline()
        self._sync_map_actions()
        if hasattr(self, "cursor_status"):
            self.cursor_status.clear()
        for bar in self.findChildren(QTabBar):
            if bar.parent() is self:
                bar.setElideMode(Qt.TextElideMode.ElideNone)
                bar.setUsesScrollButtons(True)

    def _active_map(self):
        return (
            self.log_map if self.views.currentWidget() is self.logs_view else self.map
        )

    def _place_near(self):
        return (self.map._center_lat, self.map._center_lon)

    def _go_to_place(self, place):
        self.views.setCurrentWidget(self.map)
        self.map.fly_to(place.latitude, place.longitude, place.default_zoom)
        self.statusBar().showMessage(f"{place.name} — {place.kind_label}", 4000)

    def _focus_place_search(self):
        self.views.setCurrentWidget(self.map)
        self.place_search.edit.setFocus()
        self.place_search.edit.selectAll()

    def _index_chart_places(self):
        paths = frozenset(
            chart.path
            for chart in getattr(self.controller.charts, "charts", ())
            if str(chart.path).lower().endswith(".000")
        )
        if (
            not paths
            or self.place_index_task.busy
            or paths == self._indexed_chart_paths
        ):
            return
        self._indexing_chart_paths = paths
        self.place_index_task.start(
            "Indexing chart place names",
            lambda progress: index_chart_files(tuple(paths), progress=progress),
        )

    def _schedule_chart_places(self):
        self._chart_place_timer.start()

    def _chart_places_ready(self, places, error):
        self._indexed_chart_paths = self._indexing_chart_paths
        if error is not None or not places:
            return
        self.place_index.add_places(places, merge_radius_km=5.0)
        self.place_search.refresh()

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

    def fit_race(self):
        race = getattr(self.race_panel, "race", None)
        if race is None:
            return
        coordinates = [
            (position.latitude, position.longitude)
            for boat in race.boats
            for position in boat.positions
        ]
        coordinates.extend(point for course in race.course for point in course.points)
        if coordinates:
            self.views.setCurrentWidget(self.map)
            self.map.fit_coordinates(coordinates)

    def _fit_route(self):
        self.views.setCurrentWidget(self.map)
        self.map.fit_route()

    def show_polar_workspace(self):
        self.views.setCurrentWidget(self.polar_workspace)
        self._show_panel("polar-selector")

    def _select_saved_route(self, route):
        # The routing manager's active route is the window's current route now
        # that the navigation panel is gone; selecting must publish it before
        # the overlay/isochrone sync reads it.
        self.controller.core.routingManager.set_active(route)
        # A successful restore selects the route itself while presenting the
        # saved result. Select once here only when no restore will do so.
        if not getattr(self, "_restoring_saved_routing", False):
            if self._restore_saved_routing_overlay(route):
                return True
        self._navigation_route_selected(route)
        return False

    def _restore_saved_routing_overlay(self, route):
        """Present a saved schema-v1 result without recalculating it.

        Legacy routes keep their inline frontier path. A referenced result is
        loaded once and then reused while that calculation stays active.
        """
        if route is None or getattr(route, "result_ref", None) is None:
            return False
        if self._native_owns_route(
            route, getattr(self.controller.routing, "native_overlay", None)
        ):
            return False
        self._restoring_saved_routing = True
        try:
            return self._show_saved_routing_route(route)
        finally:
            self._restoring_saved_routing = False

    def _routing_completed(self, route):
        self.scene_panel.refresh()
        restored = self._select_saved_route(route)
        self.map.fit_route()
        self._refresh_routing_alternatives(select_route=route)
        if getattr(route, "result_ref", None) is not None and not restored:
            self._show_saved_routing_route(route)

    def _native_cleared(self):
        """Detach a previous native overlay and its diagnostic inspection text."""
        self.map.native_routing = None
        unsubscribe = getattr(self, "_routing_selection_unsubscribe", None)
        if unsubscribe is not None:
            unsubscribe()
            self._routing_selection_unsubscribe = None
        self._routing_selection_target = None
        self._routing_active_route = None
        inspector = getattr(self, "routing_inspector", None)
        if inspector is not None:
            inspector.clear_result("No routing result loaded")
        if not self.controller.routing.busy:
            self._sync_route_isochrones()

    def _native_diagnostic(self, result):
        """Present a failed/incomplete native run without publishing a route."""
        from sailworks.core.marine.routingresult import export_blockers

        try:
            model = self.controller.routing.show_native_result(result)
        except (OSError, ValueError) as exc:
            self.routing_inspector.show_error(f"Native diagnostic unavailable: {exc}")
            self.routing_inspector_dock.show()
            return
        self._present_routing_result(model, route=None)
        blockers = ", ".join(export_blockers(result))
        self.routing_inspector.show_error(
            "Native calculation diagnostic"
            + (f": {blockers}" if blockers else "")
            + ". This result is not a navigable or exportable route."
        )
        self.routing_inspector_dock.show()

    def _navigation_route_selected(self, route):
        points = [
            point
            for point in (route.points if route is not None else [])
            if len(point) >= 6
        ]
        coordinates = tuple((p[0], p[1]) for p in points)
        speeds = tuple(p[5] or 0 for p in points)
        self.map.set_route(coordinates, speeds)
        self.map.navigation.hl_routing = route.name if route is not None else None
        self._sync_route_isochrones()

    def _race_selected(self, race):
        self._race_timeline_bounds = None
        if race is not None:
            position_times = tuple(
                p.timestamp.replace(tzinfo=timezone.utc).timestamp()
                for boat in getattr(race, "boats", ())
                for p in boat.positions
            )
            start = getattr(race, "start_time", None)
            end = getattr(race, "end_time", None)
            # Races without explicit start/end metadata still have usable
            # bounds from their position timestamps.
            if start is None and position_times:
                start = datetime.fromtimestamp(
                    min(position_times), timezone.utc
                ).replace(tzinfo=None)
            if end is None and position_times:
                end = datetime.fromtimestamp(max(position_times), timezone.utc).replace(
                    tzinfo=None
                )
            if start is not None and end is not None:
                self._race_timeline_bounds = (
                    start.replace(tzinfo=timezone.utc).timestamp(),
                    end.replace(tzinfo=timezone.utc).timestamp(),
                    position_times,
                )
        self._apply_race_timeline_bounds(force=True)
        self._apply_race_course(race)

    def _apply_race_course(self, race):
        courses = []
        if race is not None:
            for course in getattr(race, "course", ()) or ():
                points = [[float(point[0]), float(point[1])] for point in course.points]
                if len(points) >= 2:
                    courses.append(
                        {
                            "name": str(course.name or ""),
                            "points": points,
                            "closed": bool(getattr(course, "closed", False)),
                        }
                    )
        for canvas in (self.map, self.log_map):
            try:
                canvas.race_courses = courses
            except Exception:
                pass
            try:
                canvas.refresh_route_overlay()
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

    def _apply_race_timeline_bounds(self, *_args, force=False):
        bounds = getattr(self, "_race_timeline_bounds", None)
        timeline = getattr(self.map, "timeline", None)
        if timeline is None:
            return
        if bounds is None:
            if getattr(self, "_race_region_installed", False):
                timeline.clear_region()
                self._race_region_installed = False
            return
        start, end, _valid_times = bounds
        if not force and not self._race_window_in_forecast(start, end):
            timeline.clear_region()
            self._race_region_installed = False
            return
        timeline.set_region(start, end)
        self._race_region_installed = True

    def _race_window_in_forecast(self, start, end):
        try:
            return any(
                source.first_valid <= end and source.last_valid >= start
                for source in self.controller.display_forecast.sources() or ()
            )
        except Exception:
            return False

    def _race_tracks_changed(self):
        self._publish_race_track_overlay()
        self._navigation_changed()
        self._race_time_changed(self.controller.time_control.time)

    def _publish_race_track_overlay(self):
        panel = getattr(self, "race_panel", None)
        active = getattr(panel, "active_race_track", None)
        loaded = panel.loaded_race_tracks() if panel is not None else {}
        tracks = []
        for race_tracks in loaded.values():
            for track in race_tracks:
                if not track.visible:
                    continue
                tracks.append(
                    {
                        "name": track.name,
                        "points": [
                            [float(point[0]), float(point[1])] for point in track.points
                        ],
                        "color": [float(channel) for channel in track.color],
                        "active": track is active,
                    }
                )
        for canvas in (self.map, self.log_map):
            try:
                canvas.race_tracks_overlay = tracks
            except Exception:
                pass
            try:
                canvas.refresh_route_overlay()
            except Exception:
                pass
            try:
                canvas.update()
            except Exception:
                pass

    def _race_time_changed(self, when):
        panel = getattr(self, "race_panel", None)
        race = getattr(panel, "race", None) if panel is not None else None
        active = getattr(panel, "active_race_track", None)
        if active is None or not active.visible or not active.timestamps:
            for canvas in (self.map, self.log_map):
                try:
                    canvas.offsets = ()
                    canvas.position = None
                except Exception:
                    # Canvas may not have these attributes in all configurations
                    pass
        else:
            try:
                # TimeControl carries local wall time; race tracks use UTC epochs.
                position = when.timestamp()
            except Exception:
                position = None
            if position is not None:
                for canvas in (self.map, self.log_map):
                    canvas.offsets = tuple(active.timestamps)
                    canvas.position = position
        # Keep every visible race boat on the map at the scrubbed time.
        positions = []
        markers = []
        if race is not None and panel is not None:
            try:
                visible_tracks = {
                    track.boat_id: track
                    for track in panel.race_tracks(race)
                    if track.visible
                }
                for boat_id, boat_position in race.positions_at(
                    wall_to_utc(when)
                ).items():
                    track = visible_tracks.get(boat_id)
                    if track is None:
                        continue
                    latitude = float(boat_position.latitude)
                    longitude = float(boat_position.longitude)
                    positions.append([latitude, longitude])
                    markers.append(
                        {
                            "lat": latitude,
                            "lon": longitude,
                            "name": track.name,
                            "color": [float(channel) for channel in track.color],
                            "angle": boat_position.course,
                        }
                    )
            except Exception:
                positions = []
                markers = []
        self._race_boat_positions = positions
        for canvas in (self.map, self.log_map):
            try:
                canvas.race_boat_positions = positions
                canvas.race_positions = positions
                canvas.race_boat_markers = markers
            except Exception:
                # Not all canvases support race boat positions
                pass
            try:
                canvas.refresh_route_overlay()
            except Exception:
                # Canvas may not have route overlay
                pass
            canvas.invalidate_navigation()
            canvas.update()

    def _navigation_changed(self):
        self.routing_panel.refresh_destinations()
        self._navigation_route_selected(
            self.controller.core.routingManager.get_active()
        )
        for canvas in (self.map, self.log_map):
            canvas.invalidate_navigation()
            refresh = getattr(canvas, "refresh_route_overlay", None)
            if callable(refresh):
                refresh()
            canvas.update()

    def _navigation_refresh(self):
        """Repaint the scene tree and navigation overlays after a data change."""
        self.scene_panel.refresh()
        self._navigation_changed()
        if hasattr(self, "workspace"):
            self.workspace.changed()

    def _navigation_request(self, kind, coordinates):
        self._show_panel("scene")
        self.scene_panel.add_navigation_request(kind, coordinates)

    def _scene_changed(self):
        self._sync_isochrone_colour_mode()
        self._publish_race_track_overlay()
        for canvas in (self.map, self.log_map):
            invalidate = getattr(canvas, "invalidate_navigation", None)
            if callable(invalidate):
                invalidate()
            refresh = getattr(canvas, "refresh_route_overlay", None)
            if callable(refresh):
                refresh()
            invalidate_race = getattr(canvas, "invalidate_race_geometry", None)
            if callable(invalidate_race):
                invalidate_race()
            canvas.update()
        self._race_time_changed(self.controller.time_control.time)
        if hasattr(self, "workspace"):
            self.workspace.changed()

    def _scene_selected(self, kind, key):
        if kind == KIND_TRACK:
            if key.startswith("race:"):
                # A race track is not editable here, so selecting one leaves
                # any retained edit mode on a saved track behind.
                self._exit_track_edit()
                _prefix, race_id, boat_id = key.split(":", 2)
                race_tracks = self.race_panel.loaded_race_tracks().get(race_id, ())
                self.race_panel.active_race_track = next(
                    (track for track in race_tracks if str(track.boat_id) == boat_id),
                    None,
                )
                self._race_time_changed(self.controller.time_control.time)
                return
            track = next(
                (item for item in self.controller.core.trackManager if item.uid == key),
                None,
            )
            if track is not None:
                self.controller.core.trackManager.set_active(track)
                self._exit_track_edit()
                self.map.invalidate_navigation()
                self.log_map.invalidate_navigation()
                self.map.update()
        elif kind == KIND_ROUTING:
            route = next(
                (
                    item
                    for item in self.controller.core.routingManager
                    if item.uid == key
                ),
                None,
            )
            if route is not None:
                self._exit_track_edit()
                self.controller.core.routingManager.set_active(route)
                self._select_saved_route(route)
        elif kind == KIND_POI:
            poi = next(
                (item for item in self.controller.core.poiManager if item.uid == key),
                None,
            )
            if poi is not None:
                self._exit_track_edit()
                self.map.invalidate_navigation()
                self.map.update()

    def _set_track_editing(self, track):
        """Publish the renderer's edit track and repaint the retained overlay."""
        navigation = self.map.navigation
        navigation.edit_track = track
        navigation.hl_track_hover = None
        invalidate_race = getattr(self.map, "invalidate_race_geometry", None)
        if callable(invalidate_race):
            invalidate_race()
        # The retained QML overlay keys its track payload on the editing flag,
        # so publish while the cache still describes the previous mode.
        refresh = getattr(self.map, "refresh_route_overlay", None)
        if callable(refresh):
            refresh()
        self.map.invalidate_navigation()
        self.map.update()

    def _exit_track_edit(self):
        """Leave track edit mode while keeping the active track selected."""
        if self.map.navigation.edit_track is None:
            return
        self._set_track_editing(None)

    def _focus_map_surface(self):
        """Route keyboard input to the map so Escape leaves edit mode.

        Entering edit mode from the Scene Collection tree leaves the tree (or
        the context menu it opened) holding focus, so the viewport never sees
        the key.  The desktop map hosts its mouse and key surface in a
        ``QQuickView``, so focus both it and the containing widget.
        """
        target = getattr(self.map, "map_pointer_target", None)
        surface = target() if callable(target) else None
        if surface is not None and surface is not self.map:
            focus = getattr(surface, "setFocus", None)
            if callable(focus):
                focus()
        focus = getattr(self.map, "setFocus", None)
        if callable(focus):
            focus()

    def keyPressEvent(self, event):  # noqa: N802 - Qt virtual method name
        # Unhandled keys from the Scene Collection tree propagate here; Escape
        # must still leave track edit mode when the map does not own focus.
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Same fallback for a shape being edited: the tree, not the map,
            # may own focus, and the visible map is the one being edited.
            canvas = self._active_map()
            editing = getattr(canvas, "is_shape_editing", None)
            if editing is not None and editing():
                if event.key() == Qt.Key.Key_Escape:
                    canvas.cancel_shape_edit()
                    event.accept()
                    return
                finish = getattr(canvas, "finish_shape_edit", None)
                if finish is not None:
                    finish()
                event.accept()
                return
        if (
            event.key() == Qt.Key.Key_Escape
            and getattr(getattr(self.map, "navigation", None), "edit_track", None)
            is not None
        ):
            self._exit_track_edit()
            event.accept()
            return
        super().keyPressEvent(event)

    def _scene_edit_track(self, key):
        """Activate a Scene Collection track and enter its edit mode."""
        track = next(
            (item for item in self.controller.core.trackManager if item.uid == key),
            None,
        )
        if track is None:
            return
        self.controller.core.trackManager.set_active(track)
        self.scene_panel.refresh()
        self._set_track_editing(track)
        self.views.setCurrentWidget(self.map)
        self._focus_map_surface()

    def _scene_edit_shape_vertices(self, uid):
        """Route a Scene shape to the visible map's vertex edit mode.

        The log map only receives the edit while it is the visible workspace;
        otherwise the shape belongs to the chart map the user can see.
        """
        if self.views.currentWidget() is self.polar_workspace:
            self.views.setCurrentWidget(self.map)
        canvas = self._active_map()
        begin = getattr(canvas, "begin_shape_edit", None)
        if begin is None or not begin(uid):
            return
        self._focus_map_surface()

    def _scene_open(self, kind, key):
        if kind != KIND_LOG:
            return
        try:
            path = self.controller.log_library.path(key)
        except (KeyError, ValueError):
            return
        # Opening a log changes what the map shows, so a retained edit mode on
        # a saved track would act on marks that are no longer drawn.
        self._exit_track_edit()
        self.controller.load_paths([path])

    def _scene_fit(self, nodes):
        coordinates = []
        for node in nodes:
            entry = self.scene_panel.scene.live_entry(node[1], node[2])
            element = entry.element if entry is not None else None
            if element is None:
                continue
            if node[0] == "point":
                index = int(node[3])
                if 0 <= index < len(element.points):
                    coordinates.append(tuple(element.points[index][:2]))
            elif getattr(element, "position", None) is not None:
                coordinates.append(tuple(element.position[:2]))
            elif getattr(element, "vertices", None):
                coordinates.extend(
                    (vertex[0], vertex[1]) for vertex in element.vertices
                )
            elif hasattr(element, "to_list"):
                coordinates.extend(element.to_list())
            else:
                coordinates.extend((point[0], point[1]) for point in element.points)
        if coordinates:
            self.views.setCurrentWidget(self.map)
            self.map.fit_coordinates(coordinates)

    def _scene_delete(self, nodes):
        item_keys = {(node[1], node[2]) for node in nodes if node[0] == "item"}
        tracks = []
        routes = []
        shapes = []
        points = {}
        for node in nodes:
            entry = self.scene_panel.scene.live_entry(node[1], node[2])
            element = entry.element if entry is not None else None
            if element is None:
                continue
            if node[0] == "item":
                if node[1] == KIND_ROUTING:
                    routes.append(element)
                elif node[1] == KIND_TRACK and not node[2].startswith("race:"):
                    tracks.append(element)
                elif node[1] == KIND_SHAPE:
                    shapes.append(element)
            elif (node[1], node[2]) not in item_keys:
                points.setdefault(id(element), (element, set()))[1].add(int(node[3]))

        removals = []
        for manager, elements, current, kind in (
            (
                self.controller.core.trackManager,
                tracks,
                self.controller.core.trackManager.get_active(),
                "track",
            ),
            (
                self.controller.core.routingManager,
                routes,
                self.controller.core.routingManager.get_active(),
                "route",
            ),
        ):
            wanted = {id(element) for element in elements}
            removed = [
                (index, element)
                for index, element in enumerate(manager)
                if id(element) in wanted
            ]
            if removed:
                removals.append((manager, removed, current, kind))
        edited = getattr(getattr(self.map, "navigation", None), "edit_track", None)
        if edited is not None and any(
            element is edited
            for _manager, removed, _current, _kind in removals
            for _index, element in removed
        ):
            # The track being edited is about to disappear.  Leave edit mode
            # before the removal refresh runs, otherwise the overlay keeps a
            # stale track whose marks can still be hit-tested and dragged.
            self._exit_track_edit()
        if removals:
            self.undo_stack.beginMacro("Delete scene items")
            try:
                for manager, removed, current, kind in removals:
                    self.undo_stack.push(
                        RemoveNavigationCommand(
                            self._navigation_refresh,
                            manager,
                            removed,
                            current,
                            kind,
                        )
                    )
            finally:
                self.undo_stack.endMacro()

        # Shapes are not navigation elements, so they are removed directly from
        # their collection instead of going through the navigation undo command.
        removed_shapes = False
        if shapes:
            manager = getattr(self.controller.core, "shapeManager", None)
            if manager is not None:
                remove_many = getattr(manager, "remove_many", None)
                if callable(remove_many):
                    remove_many(shapes)
                else:
                    for element in shapes:
                        manager.remove(element)
                removed_shapes = True

        changed = removed_shapes
        for element, indices in points.values():
            for index in sorted(indices, reverse=True):
                if 0 <= index < len(element):
                    element.remove(index)
                    changed = True
        if changed:
            self._navigation_refresh()

    def _move_scene_mark(self, element, index):
        """Drag a mark that the retained scene already draws and previews.

        The scene publishes the mark under the pointer itself, so the preview
        and commit have to project with the scene camera.  This is the only
        path that opts into that projection.
        """
        self._move_scene_point(element, index, direct_point=True)

    def _move_scene_point(self, element, index, direct_point=False):
        """Move one point of a scene element after a click or drag.

        An explicit Move from the scene list and a calculated route relocation
        both pick a new position with the widget camera and repaint the canvas.
        Only a direct drag of a retained mark sets ``direct_point``.
        """
        if not 0 <= index < len(element):
            return
        before = (element.points[index][0], element.points[index][1])

        def preview(coordinates):
            self.map.preview_move_point(element, index, coordinates[0], coordinates[1])

        def restore():
            self.map.clear_move_preview()

        def move(coordinates):
            # A caller may invoke the commit callback directly (as tests and
            # the retained map API do); dropping the pending preview first
            # keeps a later cancel from restoring the old position.
            self.map._end_move(restore=False)
            if 0 <= index < len(element):
                self.undo_stack.push(
                    MovePointCommand(
                        self._refresh_scene_point,
                        element,
                        index,
                        before,
                        coordinates,
                    )
                )

        self.views.setCurrentWidget(self.map)
        self.map.begin_move(
            move,
            preview=preview,
            restore=restore,
            direct_point=bool(direct_point),
        )

    def _refresh_scene_point(self):
        self.scene_panel.refresh()
        self._scene_changed()

    def _toggle_ais(self, visible):
        for canvas in (self.map, self.log_map):
            canvas.ais_visible = visible
            canvas.update()

    def _refresh_ais(self):
        core = self.controller.core
        targets = tuple(
            target
            for target in core.ais_manager.get_active_targets()
            if target.has_valid_position()
        )
        for canvas in (self.map, self.log_map):
            canvas.ais_targets = targets
        self.map.update()
        self._ais_updated(targets)

    def _ais_updated(self, targets):
        self.log_map.ais_targets = targets
        self.log_map.update()

    def _export_navigation(self):
        menu = ThemedMenu(self)
        builder = MenuBuilder(menu)
        builder.add("Export GPX…", lambda: navigationio.export_all(self), key="G")
        menu.exec(self.map.mapToGlobal(self.map.rect().bottomLeft()))

    def _show_panel(self, panel):
        name = panel if isinstance(panel, str) else None
        if name is None:
            name = next(
                (
                    candidate
                    for candidate, wrapper in self.panels.items()
                    if wrapper is panel
                ),
                None,
            )
        if name is None or name not in self.panels:
            return
        side = self._panel_side(name) or self._panel_home.get(name, "right")
        if not self._panel_visible(name):
            self._panel_stacks[side].add_panel(self.panels[name])
        self._open_panel_stack(side)
        action = self.panel_actions.get(name)
        if action is not None:
            with QSignalBlocker(action):
                action.setChecked(True)
        wrapper = self.panels[name]
        wrapper.set_collapsed(False, animate=False)
        self._panel_stacks[side].scroll.ensureWidgetVisible(wrapper)
        if name in ("weather", "routing", "currents", "tides"):
            self.views.setCurrentWidget(self.map)

    def show_marine_settings(self, tab):
        if self.marine_settings is None:
            self.marine_settings = MarineSettings(self.controller, self)
        self.marine_settings.select_tab(tab)

    def show_marine_library(self, kind):
        self._ensure_library()
        assert self.library_dialog is not None
        self.library_dialog.show_marine(kind)

    def dispose_marine(self):
        if self.settings_dialog is not None:
            self.settings_dialog.currents.dispose()
        for panel in self.marine_panels.values():
            panel.dispose()
        if self.library_dialog is not None:
            self.library_dialog.dispose_marine()
        if self.marine_settings is not None:
            self.marine_settings.dispose()

    def reset_panels(self):
        """Restore the default panel order, visibility and stack widths."""
        self.restore_panel_state(self._default_panel_state)

    def _routing_changed(self):
        if self.controller.routing.busy:
            self.views.setCurrentWidget(self.map)
        else:
            self._sync_route_isochrones()

    def _routing_selection_changed(self):
        model = getattr(self, "_routing_selection_target", None)
        selection = self.controller.routing.native_selection
        if model is not None and selection is not None:
            model.set_selected_layer(selection.layer_id)
            apply_highlight = getattr(self.map, "_apply_retained_highlight", None)
            if apply_highlight is not None:
                apply_highlight()
            # Persist the inspection context with the referenced result so a
            # reload restores it without touching a forecast.
            route = getattr(self, "_routing_active_route", None)
            if route is not None:
                from sailworks.core.marine.routinginspection import InspectionContext

                summary = getattr(route, "result_summary", None)
                recorded = (
                    summary.get("calculation_id") if isinstance(summary, dict) else None
                )
                if recorded is not None and recorded != selection.calculation_id:
                    # A selection that is not this route's result must never
                    # overwrite the context saved beside it.
                    return
                context = InspectionContext.from_selection(selection).to_dict()
                if route.inspection_context != context:
                    route.inspection_context = context
                    collection = getattr(route, "collection", None)
                    if collection is not None:
                        collection.save()

    def _routing_visibility_changed(self, element=None):
        """Refresh isochrones when a scene toggle changes routing visibility.

        This is deliberately separate from ``_scene_changed``: unrelated scene
        edits must not hydrate a referenced result artifact. ``element`` is the
        scene route the toggle belongs to, so the flag is resolved from the
        row the user just changed instead of only from the active route
        selection (a saved route restored from the scene may have none).
        """
        self._sync_route_isochrones(element)
        for canvas in (self.map, self.log_map):
            invalidate = getattr(canvas, "invalidate_navigation", None)
            if callable(invalidate):
                invalidate()
            refresh = getattr(canvas, "refresh_route_overlay", None)
            if callable(refresh):
                refresh()
            else:
                publish = getattr(canvas, "publish_route_overlay", None)
                if callable(publish):
                    publish(force=True)
            canvas.update()

    def _sync_isochrone_colour_mode(self):
        """Apply the active route's mode on scene changes without artifact I/O."""
        routing = getattr(self.controller, "routing", None)
        native = getattr(routing, "native_overlay", None)
        route = self.controller.core.routingManager.get_active()
        mode = _routing_isochrone_mode(
            route, native, getattr(self, "_routing_active_route", None)
        )
        if mode is None:
            return
        # With no route selected the fill has no owner here. Leave it alone
        # instead of writing False, which would undo a toggle driven from the
        # Scene Collection; ``_routing_visibility_changed`` applies that
        # route's own flag once the row names it.
        fill_visible = None
        if route is not None:
            fill_visible = bool(
                native is not None
                and self._native_owns_route(route, native)
                and getattr(route, "isochrones_visible", True)
                and getattr(route, "isochrone_fill_visible", False)
            )
            # The overlay model owns the fill flag; the canvas attribute below
            # only mirrors it, exactly like the contour and trace flags.
            setter = getattr(native, "set_fill_visible", None)
            if callable(setter):
                setter(fill_visible)
            # Hull outlines are independent of the fill and follow the same
            # ownership rule: only this route's presented result may set them.
            outlines_visible = bool(
                native is not None
                and self._native_owns_route(route, native)
                and getattr(route, "isochrones_visible", True)
                and getattr(route, "isochrone_outlines_visible", False)
            )
            outlines_setter = getattr(native, "set_outlines_visible", None)
            if callable(outlines_setter):
                outlines_setter(outlines_visible)
            for canvas in (getattr(self, "map", None), getattr(self, "log_map", None)):
                if canvas is not None:
                    canvas.isochrone_fill_visible = fill_visible
                    canvas.isochrone_outlines_visible = outlines_visible
        for canvas in (getattr(self, "map", None), getattr(self, "log_map", None)):
            if canvas is not None:
                canvas.isochrone_colour_by = mode

    def _native_owns_route(self, route, native):
        """Whether a native overlay was presented for this route's result."""
        return _routing_owns_route(
            route, native, getattr(self, "_routing_active_route", None)
        )

    def _sync_route_isochrones(self, toggled=None):
        if getattr(self, "_syncing_route_isochrones", False):
            # A row update requested by this sync must not re-enter it.
            return
        if not self.controller.routing.busy:
            native = getattr(
                getattr(getattr(self, "controller", None), "routing", None),
                "native_overlay",
                None,
            )
            route = _routing_presented_route(
                toggled,
                self.controller.core.routingManager.get_active(),
                native,
                getattr(self, "_routing_active_route", None),
            )
            show = (
                route is not None
                and bool(getattr(route, "visible", True))
                and bool(getattr(route, "isochrones_visible", True))
            )
            # The row shows contour availability, so remember what it said
            # before this sync hydrates the artifact and record a transition.
            error_before = getattr(route, "contour_error", None)
            # Do not hydrate a referenced artifact while the layer is hidden.
            if native is not None and route is not None:
                active = getattr(self, "_routing_active_route", None)
                if self._native_owns_route(route, native):
                    # Gate only the isochrone layers; each layer keeps its own
                    # remembered visibility and the trace/path geometry stays.
                    native.set_contours_visible(show)
                elif active is not None:
                    # A stale result presented for another route must never be
                    # shown as if it were this route's isochrones.
                    native = None
                # When nothing was presented for a route, the overlay was opened
                # as an independent result file and this route's toggle does not
                # govern it.
            self.map.set_isochrones(route.isochrones if show else ())
            # The route records why its artifact could not be read; surface it
            # through the map caption instead of leaving an empty caption that
            # looks like a healthy result with no contours.
            self.map.isochrone_error = getattr(route, "contour_error", None)
            mode = _routing_isochrone_mode(
                route, native, getattr(self, "_routing_active_route", None)
            )
            # The fill belongs to this route's presented result only: a stale
            # or independently opened overlay must never inherit it. It also
            # follows the isochrone eye, so hiding the contours never leaves a
            # filled band with no frontier.
            fill_visible = bool(
                show
                and route is not None
                and native is not None
                and self._native_owns_route(route, native)
                and getattr(route, "isochrone_fill_visible", False)
            )
            # Outlines share the fill's ownership gate but not its value: the
            # user can stroke hulls without filling them, or fill without a
            # boundary.
            outlines_visible = bool(
                show
                and route is not None
                and native is not None
                and self._native_owns_route(route, native)
                and getattr(route, "isochrone_outlines_visible", False)
            )
            for canvas in (
                getattr(self, "map", None),
                getattr(self, "log_map", None),
            ):
                if canvas is None:
                    continue
                if mode is not None:
                    # Both canvases share the mode attribute so switching
                    # between the map and log views never shows a stale colour
                    # mode. Only the primary map presents the routing
                    # isochrones.
                    canvas.isochrone_colour_by = mode
                canvas.isochrone_fill_visible = fill_visible
                canvas.isochrone_outlines_visible = outlines_visible
            # The model owns the fill flag, so the host attribute is only a
            # mirror. An independent or stale overlay is set to False here,
            # which is what keeps it from inheriting another route's fill.
            fill_setter = getattr(native, "set_fill_visible", None)
            if callable(fill_setter):
                fill_setter(fill_visible)
            outlines_setter = getattr(native, "set_outlines_visible", None)
            if callable(outlines_setter):
                outlines_setter(outlines_visible)
            self.map.native_routing = native
            # Traces use their own flag, independent of the contour gate: a
            # hidden isochrone layer must not hide the trace web. Only the
            # primary map hosts the native routing overlay (the log map is a
            # separate log view and never receives ``native_routing``), so the
            # flag is applied there and nowhere else.
            traces_visible = bool(getattr(route, "traces_visible", False))
            canvas_native = getattr(self.map, "native_routing", None)
            if canvas_native is not None and self._native_owns_route(
                route, canvas_native
            ):
                setter = getattr(canvas_native, "set_traces_visible", None)
                if callable(setter):
                    setter(traces_visible)
            selection = self.controller.routing.native_selection
            if selection is not None and native is not None:
                native.attach_selection(selection)
            if getattr(route, "contour_error", None) != error_before:
                # Contour availability changed during this sync, so the Scene
                # Collection row would otherwise stay one refresh behind and
                # offer a checkbox for a result it no longer has.
                self._refresh_route_isochrone_row()

    def _refresh_route_isochrone_row(self):
        """Rebuild the scene rows that show contour availability.

        Called only when a sync changed ``contour_error``, so it stays one
        explicit request instead of a full panel refresh on every sync. The
        re-entry guard means a panel emitting its own change signal during the
        rebuild cannot drive another sync and loop.
        """
        scene_panel = getattr(self, "scene_panel", None)
        if scene_panel is None:
            return
        if getattr(self, "_syncing_route_isochrones", False):
            return
        self._syncing_route_isochrones = True
        try:
            scene_panel.refresh()
        finally:
            self._syncing_route_isochrones = False

    def _create_routing_inspector(self):
        self.routing_inspector = RoutingInspectorPanel(self)
        self.routing_inspector.alternative_selected.connect(
            self._routing_alternative_selected
        )
        self._routing_alternative_routes = ()
        self._routing_active_route = None
        self.routing_inspector_dock = QDockWidget("Routing inspector", self)
        self.routing_inspector_dock.setObjectName("routing-inspector-dock")
        self.routing_inspector_dock.setWidget(self.routing_inspector)
        self.addDockWidget(
            Qt.DockWidgetArea.RightDockWidgetArea, self.routing_inspector_dock
        )
        self.routing_inspector_dock.hide()
        self.routing_inspector_dock.visibilityChanged.connect(
            self._routing_inspector_visibility_changed
        )
        self._refresh_routing_alternatives()

    def _routing_inspector_visibility_changed(self, visible):
        if visible:
            self._refresh_routing_alternatives()

    def _routing_alternatives(self):
        manager = getattr(
            getattr(getattr(self, "controller", None), "core", None),
            "routingManager",
            None,
        )
        return tuple(
            route
            for route in getattr(manager, "elements", ())
            if getattr(route, "result_ref", None) is not None
        )

    def _refresh_routing_alternatives(self, select_route=None):
        routes = self._routing_alternatives()
        self._routing_alternative_routes = routes
        labels = []
        for route in routes:
            summary = (
                route.result_summary if isinstance(route.result_summary, dict) else {}
            )
            calculation = str(summary.get("calculation_id", ""))[:8]
            labels.append(
                f"{route.name} · {calculation}" if calculation else str(route.name)
            )
        try:
            current = routes.index(select_route)
        except ValueError:
            current = None
        self.routing_inspector.set_alternatives(labels, current)

    def _show_saved_routing_route(self, route):
        try:
            manager = getattr(
                getattr(getattr(self, "controller", None), "core", None),
                "routingManager",
                None,
            )
            if manager is None:
                raise ValueError("no routing collection is loaded")
            # Selecting a saved route is the explicit retry point for a contour
            # artifact that was missing or corrupt on an earlier visit. This
            # only clears the remembered failure; the single read happens in
            # the sync below, so repeated frames still never retry.
            route.refresh_isochrones()
            result = manager.load_result(route)
            model = self.controller.routing.show_native_result(result)
        except (OSError, KeyError, ValueError) as exc:
            self.routing_inspector.show_error(f"Routing result unavailable: {exc}")
            self.routing_inspector_dock.show()
            return False
        self._select_saved_route(route)
        from sailworks.core.marine.routinginspection import InspectionContext

        try:
            context = InspectionContext.from_dict(route.inspection_context)
        except ValueError as exc:
            context = None
            context_error = str(exc)
        else:
            context_error = None
            recorded = getattr(context, "calculation_id", None)
            if recorded is not None and recorded != result.calculation_id:
                # Preflight before presenting so a stored context from another
                # calculation cannot half-switch the inspector.
                context_error = (
                    "stored inspection context belongs to calculation "
                    f"{recorded}; this result is {result.calculation_id}"
                )
                context = None
        self._present_routing_result(
            model, route=route, context=context, context_error=context_error
        )
        # The active route is recorded by presentation, so reapply the saved
        # isochrone toggle to the restored native layers now.
        self._sync_route_isochrones()
        return True

    def _present_routing_result(
        self, model, route=None, context=None, context_error=None
    ):
        previous_unsubscribe = getattr(self, "_routing_selection_unsubscribe", None)
        if previous_unsubscribe is not None:
            previous_unsubscribe()
        selection = self.controller.routing.native_selection
        self.routing_inspector.set_result(
            model.result,
            selection,
            context=context,
            context_error=context_error,
            layer_visibility=getattr(model, "is_layer_visible", None),
        )
        self._routing_selection_target = model
        self._routing_active_route = route
        self._routing_selection_unsubscribe = selection.subscribe(
            self._routing_selection_changed
        )
        self.routing_inspector_dock.show()
        self._sync_route_isochrones()

    def _routing_alternative_selected(self, index):
        routes = getattr(self, "_routing_alternative_routes", ())
        if index < 0 or index >= len(routes):
            return
        self._show_saved_routing_route(routes[index])

    def show_native_routing_result(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open native routing result", "", "Routing result (*.json)"
        )
        if not path:
            return
        try:
            model = self.controller.routing.show_native_result_file(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Native routing result", str(exc))
            return
        self._present_routing_result(model)
        self._refresh_routing_alternatives()
        self.routing_inspector.select_nearest_layer(
            model.result.layers[0].time_epoch_s if model.result.layers else 0
        )

    def _nmea_panel(self):
        panel = QWidget()
        panel.setAutoFillBackground(True)
        panel.setMinimumWidth(250)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(section_label("Data source"))
        self.source = Dropdown("Source")
        self.source.addItems(["Live data source", "Loaded log", "None"])
        self.source.currentIndexChanged.connect(self._source_changed)
        layout.addWidget(self.source)
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
        self.log_library_button = QPushButton("Log library…")
        self.log_library_button.clicked.connect(self.show_log_library)
        layout.addWidget(self.log_library_button)
        self.track_source_group = QWidget()
        track_layout = QVBoxLayout(self.track_source_group)
        track_layout.setContentsMargins(0, 0, 0, 0)
        self.track_source = Dropdown("GPS track source")
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
        if session.source == "live":
            self.source.setCurrentIndex(0)
        elif session.source == "none":
            self.source.setCurrentIndex(2)
        else:
            self.source.setCurrentIndex(1)
        self.source.model().item(1).setEnabled(True)
        self.source.model().item(2).setEnabled(True)
        self.source.setEnabled(
            not session.loading and not self.controller.connection_task.busy
        )
        self.record.setChecked(session.recording)
        self.record.setVisible(session.source == "live" and session.streaming)
        self.record.setEnabled(
            session.recording
            or (session.source == "live" and session.streaming and not session.loading)
        )
        none_source = session.source == "none"
        self.instruments_toggle.setVisible(not none_source)
        self.connection_status.setVisible(not none_source)
        self.log_library_button.setVisible(session.source == "log")
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
        scene_panel = getattr(self, "scene_panel", None)
        if scene_panel is not None:
            scene_panel.refresh()
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
                error = status.get("error")
                if error:
                    name += (
                        f' <span style="color:#8a8a8a;">— {escape(str(error))}</span>'
                    )
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
        self.track_source_group.setVisible(session.source != "none" and bool(tracks))
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
        elif index == 2:
            self.controller.set_live_reading(False)
            self.session.select_source("none")
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
            self._show_panel("nmea")
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

    def _resume_era5_downloads(self):
        """Resume ERA5 requests persisted before the last shutdown."""
        if self.library_dialog is not None:
            return
        try:
            self.library_dialog = DataLibrary(
                self.controller,
                self,
                capture_region=self._capture_analysis_region,
            )
        except Exception:
            self.library_dialog = None

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

    def _use_polar(self, name):
        index = self.routing_panel.polar.findText(name)
        if index >= 0:
            self.routing_panel.polar.setCurrentIndex(index)
            self.routing_panel.pages.setCurrentIndex(0)
            self._show_panel("routing")
            if self.library_dialog is not None:
                self.library_dialog.hide()

    def closeEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.workspace.close()
        if self.map.timeline is not None:
            self.map.timeline.dispose()
        self.dispose_marine()
        if hasattr(self, "weather_panel"):
            self.weather_panel.dispose()
        navigationio.close(self)
        self.ais_timer.stop()
        self.polar_workspace.dispose()
        self.live_timer.stop()
        self._chart_place_timer.stop()
        self.place_index_task.close()
        self.controller.close()
        event.accept()
