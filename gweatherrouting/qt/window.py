"""Small Qt frontend for evaluating NMEA, plotting, docking, and map integration."""

import os
import sys
from datetime import timedelta

from PySide6.QtCore import Qt, QTimer, QDateTime, QSize
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDateTimeEdit,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QTabBar,
    QSplitter,
    QScrollArea,
    QFrame,
    QSizePolicy,
    QStyle,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
)

from gweatherrouting.core.timecontrol import TimeControl, naive_wall_time
from gweatherrouting.rendering.style import Style
from gweatherrouting.common import resource_path
from .theme import section_label, symbol_icon, transport_icon
from .mapcanvas import TrackMap
from .routingpanel import RoutingPanel
from .settingsdialog import SettingsDialog
from .datalibrary import DataLibrary, FORECAST_FILTER
from .plots import LogPlots, elapsed_offsets
from .workspace import WorkspacePreferences
from .polarworkspace import PolarWorkspace
from .polarlibrary import PolarLibrary
from .navigationpanel import NavigationPanel


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
        self.data_key = None
        self.settings_dialog = None
        self.library_dialog = None
        self.setWindowTitle("GWeatherRouting")
        self.setWindowIcon(QIcon(resource_path("gweatherrouting", "data/icon.png")))
        self.resize(1200, 800)
        if sys.platform == "darwin":
            self.setUnifiedTitleAndToolBarOnMac(True)
        self.map = TrackMap(controller, self)
        header = QToolBar("Workspaces", self)
        header.setObjectName("workspaces")
        self.workspace_toolbar = header
        header.setMovable(False)
        self.addToolBar(header)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        header.addWidget(spacer)
        self.workspace_tabs = QTabBar()
        self.workspace_tabs.setExpanding(False)
        for name in ("Charts", "Logs", "Polars"):
            self.workspace_tabs.addTab(name)
        header.addWidget(self.workspace_tabs)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        header.addWidget(spacer)
        header.addAction(
            symbol_icon("weather-windy-symbolic"), "Forecasts", self.show_forecasts
        )
        header.addAction("Settings", self.show_settings)
        self.addToolBarBreak()
        toolbar = QToolBar("Navigation", self)
        self.navigation_toolbar = toolbar
        toolbar.setObjectName("navigation")
        toolbar.setMovable(False)
        toolbar.setAutoFillBackground(True)
        toolbar.setMinimumHeight(32)
        self.addToolBar(toolbar)
        open_forecast = toolbar.addAction(
            symbol_icon("weather-windy-symbolic"), "Open forecast…", self.load_grib
        )
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.addSeparator()
        toolbar.addWidget(QLabel("Forecast "))
        self.forecast = QDateTimeEdit(QDateTime(controller.time_control.time))
        self.forecast.setCalendarPopup(True)
        self.forecast.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.forecast.setToolTip("Time displayed by the wind forecast")
        self.forecast.dateTimeChanged.connect(
            lambda value: controller.time_control.set_time(value.toPython())
        )
        toolbar.addWidget(self.forecast)
        self.palette_combo = QComboBox()
        self.palette_combo.addItems(list(Style.chart_palettes))
        self.palette_combo.setCurrentText(controller.settings.chartPalette)
        self.palette_combo.setAccessibleName("Chart palette")
        self.palette_combo.setToolTip(
            "Chart colors; independent of the workspace theme"
        )
        self.palette_combo.currentTextChanged.connect(self._palette_changed)
        toolbar.addWidget(self.palette_combo)
        self.plots = LogPlots(self)
        self.log_time = TimeControl()
        self.log_map = TrackMap(controller, self, time_control=self.log_time)
        self.logs_view = QSplitter(Qt.Orientation.Vertical)
        self.logs_view.addWidget(self.log_map)
        self.logs_view.addWidget(self.plots)
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
        self.nmea_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        nmea_scroll = QScrollArea()
        nmea_scroll.setWidgetResizable(True)
        nmea_scroll.setFrameShape(QFrame.Shape.NoFrame)
        nmea_scroll.setWidget(self._nmea_panel())
        self.nmea_dock.setWidget(nmea_scroll)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.nmea_dock)
        self.routing_panel = RoutingPanel(controller)
        controller.polars.changed.connect(self.routing_panel.refresh_polars)
        controller.polars.use_requested.connect(self._use_polar)
        self.routing_panel.manage_polars.clicked.connect(self.show_polars)
        self.routing_dock = QDockWidget("Routing", self)
        self.routing_dock.setObjectName("routing")
        self.routing_dock.setWidget(self.routing_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.routing_dock)
        self.tabifyDockWidget(self.nmea_dock, self.routing_dock)
        self.navigation_panel = NavigationPanel(controller, self.map, self)
        navigation_scroll = NavigationScroll()
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
        open_log = file_menu.addAction(
            symbol_icon("document-open-symbolic"), "Open NMEA log…", self.load_log
        )
        open_log.setShortcut(QKeySequence.StandardKey.Open)
        file_menu.addAction(open_forecast.icon(), open_forecast.text(), self.load_grib)
        file_menu.addAction("Data Library…", self.show_library)
        file_menu.addAction("Polars", self.show_polar_workspace)
        file_menu.addSeparator()
        file_menu.addAction("Close window", self.close).setShortcut(
            QKeySequence.StandardKey.Close
        )
        settings_action = file_menu.addAction("Settings…", self.show_settings)
        settings_action.setShortcut(QKeySequence.StandardKey.Preferences)
        toolbar.addAction("Data Library", self.show_library)
        controller.data.forecast_loaded.connect(self._forecast_loaded)
        view_menu = self.menuBar().addMenu("View")
        view_menu.addAction(self.nmea_dock.toggleViewAction())
        view_menu.addAction(self.routing_dock.toggleViewAction())
        view_menu.addAction(self.polar_selector_dock.toggleViewAction())
        view_menu.addAction(self.performance_dock.toggleViewAction())
        view_menu.addAction(self.polar_manager_dock.toggleViewAction())
        view_menu.addAction(self.navigation_dock.toggleViewAction())
        view_menu.addAction(toolbar.toggleViewAction())
        view_menu.addAction(header.toggleViewAction())
        self.chart_toolbar_actions = list(toolbar.actions())
        for name, callback in (
            ("Import…", self.navigation_panel.import_gpx),
            ("New", self._new_track),
            ("Export…", self._export_navigation),
        ):
            self.chart_toolbar_actions.append(toolbar.addAction(name, callback))
        self.navigation_action = toolbar.addAction("Tabs")
        self.navigation_action.setCheckable(True)
        self.navigation_action.setChecked(True)
        self.navigation_action.setToolTip("Show / Hide Track / POI tabs")
        self.navigation_action.triggered.connect(self._toggle_navigation)
        self.chart_toolbar_actions.append(self.navigation_action)
        self.ais_action = toolbar.addAction("AIS")
        self.ais_action.setCheckable(True)
        self.ais_action.setChecked(True)
        self.ais_action.toggled.connect(self._toggle_ais)
        self.chart_toolbar_actions.append(self.ais_action)
        for action in self.chart_toolbar_actions[-5:]:
            toolbar.insertAction(open_forecast, action)
        file_menu.addAction("Import GPX…", self.navigation_panel.import_gpx)
        file_menu.addAction("Export navigation…", self._export_navigation)
        toolbar.addSeparator()
        self.nmea_action = self.nmea_dock.toggleViewAction()
        toolbar.addAction(self.nmea_action)
        self.routing_action = toolbar.addAction(
            "Routing", lambda: self._show_panel(self.routing_dock)
        )
        self.chart_toolbar_actions.append(self.routing_action)
        fit_menu = self.menuBar().addMenu("Navigate")
        fit_menu.addAction("Fit NMEA track", self._fit_track)
        fit_menu.addAction("Fit route", self._fit_route)
        self.chart_toolbar_actions.append(toolbar.addAction("Fit view", self._fit_view))
        self.polar_toolbar_actions = [
            toolbar.addAction("Polar manager", self.show_polars)
        ]
        for action in (
            self.polar_selector_dock.toggleViewAction(),
            self.performance_dock.toggleViewAction(),
        ):
            button = QToolButton()
            button.setDefaultAction(action)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setAutoRaise(True)
            self.polar_toolbar_actions.append(toolbar.addWidget(button))
        self.log_toolbar_actions = []
        self.log_toggles = {}
        for key, label, enabled in (
            ("speed", "Speed", True),
            ("aws", "Wind (A)", False),
            ("tws", "Wind (T)", True),
            ("depth", "Depth", False),
            ("hdg", "HDG", True),
            ("twa", "RW", False),
        ):
            action = toolbar.addAction(label)
            action.setCheckable(True)
            action.setChecked(enabled)
            action.toggled.connect(
                lambda checked, key=key: self._toggle_log(key, checked)
            )
            self.log_toggles[key] = action
            self.log_toolbar_actions.append(action)
        self.views.currentChanged.connect(self._view_changed)
        self._view_changed()
        self.routing_panel.plan_changed.connect(self.map.set_route_plan)
        self.routing_panel.route_selected.connect(self._route_selected)
        self.routing_panel.fit_requested.connect(self.map.fit_route)
        self.routing_panel.waypoints.currentCellChanged.connect(
            lambda row, *_: self.map.select_route_point(row)
        )
        self.map.route_location.connect(self.routing_panel.set_location)
        controller.routing.preview_changed.connect(self.map.set_route_preview)
        controller.routing.changed.connect(self._routing_changed)
        self.routing_panel._plan_changed()
        self.routing_panel._select_route(0)
        self.instruments = QLabel("Waiting for position")
        self.statusBar().addPermanentWidget(self.instruments)
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
        view_menu.addSeparator()
        view_menu.addAction("Reset layout", self.workspace.reset_layout)

    def _dock(self, title, name, content):
        dock = QDockWidget(title, self)
        dock.setObjectName(name)
        dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        dock.setWidget(content)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        return dock

    def _order_toolbars(self):
        hidden = self.workspace_toolbar.isHidden()
        self.removeToolBarBreak(self.navigation_toolbar)
        self.removeToolBar(self.workspace_toolbar)
        self.insertToolBar(self.navigation_toolbar, self.workspace_toolbar)
        self.insertToolBarBreak(self.navigation_toolbar)
        self.workspace_toolbar.setVisible(not hidden)

    def _view_changed(self, *_):
        current = self.views.currentWidget()
        self.navigation_syncing = True
        self.navigation_dock.setVisible(current is self.map and self.navigation_open)
        self.navigation_syncing = False
        self.navigation_action.setChecked(self.navigation_open)
        before = (
            self.polar_toolbar_actions[-1]
            if current is self.polar_workspace
            else (
                self.log_toolbar_actions[0]
                if current is self.logs_view
                else self.routing_action
            )
        )
        self.navigation_toolbar.insertAction(before, self.nmea_action)
        for bar in self.findChildren(QTabBar):
            if bar.parent() is self:
                bar.setElideMode(Qt.TextElideMode.ElideNone)
                bar.setUsesScrollButtons(True)
        for action in self.chart_toolbar_actions:
            action.setVisible(current is self.map)
        for action in self.log_toolbar_actions:
            action.setVisible(current is self.logs_view)
        for action in self.polar_toolbar_actions:
            action.setVisible(current is self.polar_workspace)
        if current is self.polar_workspace and not self.polars_visited:
            self.polars_visited = True
            self.polar_selector_dock.show()
            self.performance_dock.show()
            self.polar_selector_dock.raise_()

    def _toggle_log(self, key, checked):
        self.plots.set_series(key, checked)

    def _fit_track(self):
        canvas = (
            self.log_map if self.views.currentWidget() is self.logs_view else self.map
        )
        canvas.fit_track()

    def _fit_route(self):
        self.views.setCurrentWidget(self.map)
        self.map.fit_route()

    def show_forecasts(self):
        self.show_library()
        assert self.library_dialog is not None
        self.library_dialog.tabs.setCurrentIndex(0)

    def show_polar_workspace(self):
        self.views.setCurrentWidget(self.polar_workspace)
        self._show_panel(self.polar_selector_dock)

    def _route_selected(self, coordinates):
        route = self.routing_panel.routes.currentData()
        speeds = (
            tuple(point[5] or 0 for point in route.points) if route is not None else ()
        )
        self.map.set_route(coordinates, speeds)
        self.map.navigation.hl_routing = route.name if route is not None else None
        self.navigation_panel.select_route(route)

    def _navigation_route_selected(self, route):
        self.routing_panel.refresh_routes(route)

    def _navigation_visibility(self, _):
        if not self.navigation_syncing and self.views.currentWidget() is self.map:
            self.navigation_open = not self.navigation_dock.isHidden()
            if hasattr(self, "navigation_action"):
                self.navigation_action.setChecked(self.navigation_open)

    def _toggle_navigation(self, checked):
        self.navigation_open = checked
        if checked:
            self.views.setCurrentWidget(self.map)
        self.navigation_dock.setVisible(checked)
        self.navigation_action.setChecked(checked)

    def _navigation_changed(self):
        self.routing_panel.refresh_destinations()
        self.routing_panel.refresh_routes(self.navigation_panel.route)
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
            self.navigation_toolbar.mapToGlobal(
                self.navigation_toolbar.rect().bottomLeft()
            )
        )

    def _show_panel(self, dock):
        if dock is self.navigation_dock:
            self.navigation_open = True
            self.views.setCurrentWidget(self.map)
        dock.show()
        dock.raise_()
        if hasattr(self, "workspace"):
            self.workspace._dock_activated(dock)
        if dock in (self.routing_dock, self.navigation_dock):
            self.views.setCurrentWidget(self.map)

    def _routing_changed(self):
        if self.controller.routing.busy:
            self.views.setCurrentWidget(self.map)

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
        layout.addWidget(section_label("Data source"))
        self.source = QComboBox()
        self.source.addItems(["Live connections", "Loaded log"])
        self.source.currentIndexChanged.connect(self._source_changed)
        layout.addWidget(self.source)
        self.streaming = QCheckBox("Stream live NMEA")
        self.streaming.toggled.connect(self._stream_changed)
        layout.addWidget(self.streaming)
        connections = QPushButton("Configure connections…")
        connections.clicked.connect(self.show_settings)
        layout.addWidget(connections)
        load = QPushButton("Load NMEA log…")
        load.clicked.connect(self.load_log)
        self.load_button = load
        layout.addWidget(load)
        layout.addWidget(section_label("Transport"))
        transport = QHBoxLayout()
        transport.setSpacing(0)
        self.restart = self._button(
            QStyle.StandardPixmap.SP_MediaSkipBackward, "Restart playback"
        )
        self.restart.clicked.connect(lambda: self.session.restart())
        self.play = self._button(
            QStyle.StandardPixmap.SP_MediaPlay, "Play", checkable=True
        )
        self.play.clicked.connect(self._play)
        self.stop = self._button(QStyle.StandardPixmap.SP_MediaStop, "Stop")
        self.stop.clicked.connect(lambda: self.session.stop())
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
        for button in (self.restart, self.play, self.stop, self.record):
            transport.addWidget(button)
        transport.addStretch()
        layout.addLayout(transport)
        self.speed_group = QWidget()
        speed_layout = QVBoxLayout(self.speed_group)
        speed_layout.setContentsMargins(0, 0, 0, 0)
        speed_layout.addWidget(QLabel("Playback speed"))
        self.speed = QComboBox()
        for value in (0.5, 1, 2, 5, 10):
            self.speed.addItem(f"{value:g}×", value)
        self.speed.currentIndexChanged.connect(self._speed_changed)
        speed_layout.addWidget(self.speed)
        layout.addWidget(self.speed_group)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        layout.addWidget(self.progress)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.recording_file = QLabel()
        self.recording_file.setProperty("secondary", True)
        self.recording_file.setWordWrap(True)
        self.recording_file.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.recording_file)
        layout.addStretch()
        return panel

    def _button(self, icon, label, checkable=False):
        button = QToolButton()
        names = {
            QStyle.StandardPixmap.SP_MediaSkipBackward: "restart",
            QStyle.StandardPixmap.SP_MediaPlay: "play",
            QStyle.StandardPixmap.SP_MediaStop: "stop",
        }
        button.setIcon(transport_icon(names[icon]))
        button.setIconSize(QSize(20, 20))
        button.setToolTip(label)
        button.setAccessibleName(label)
        button.setCheckable(checkable)
        button.setFixedSize(42, 34)
        button.setProperty("transport", True)
        return button

    def refresh(self):
        self.syncing = True
        session = self.session
        self.source.setCurrentIndex(0 if session.source == "live" else 1)
        self.source.setEnabled(not session.loading)
        self.streaming.setChecked(session.streaming)
        self.streaming.setEnabled(session.source == "live" and not session.loading)
        self.load_button.setEnabled(not session.loading)
        self.record.setChecked(session.recording)
        self.record.setEnabled(
            session.recording
            or (session.source == "live" and session.streaming and not session.loading)
        )
        self.record.setToolTip(
            "Stop recording"
            if session.recording
            else "Start a new timestamped recording"
        )
        self.play.setChecked(session.playing)
        self.play.setIcon(transport_icon("pause" if session.playing else "play"))
        self.play.setToolTip("Pause" if session.playing else "Play")
        enabled = (
            session.source == "log" and session.log is not None and not session.loading
        )
        self.play.setEnabled(enabled)
        self.restart.setEnabled(enabled)
        self.stop.setEnabled(session.recording or session.playing)
        self.speed_group.setVisible(session.source == "log")
        self.speed.setCurrentIndex(self.speed.findData(session.speed))
        total = len(session.log.entries) if session.log else 0
        self.progress.setValue(round(1000 * session.cursor / total) if total else 0)
        self.progress.setFormat(
            f"{session.position:.1f} / {session.log.duration:.1f} s"
            if session.log
            else "No log loaded"
        )
        if session.loading:
            text = "Loading NMEA log…"
        elif session.error:
            text = f"Could not load log: {session.error}"
        elif session.recording:
            text = "Recording · saving automatically…"
        elif session.source == "log" and session.log:
            state = "Playing" if session.playing else "Paused"
            text = (
                f"{state} · {os.path.basename(session.log.path)}\n{total:,} sentences"
            )
        elif session.recording_path:
            text = "Recording stopped · saved automatically."
        else:
            text = (
                "Streaming live connections"
                if session.streaming
                else "Live stream paused"
            )
        self.status.setText(text)
        path = session.recording_path
        self.recording_file.setText(
            f"Recording file:\n{path}"
            if path
            else f"Recordings folder:\n{session.recording_directory}"
        )
        self.recording_file.setToolTip(str(path or session.recording_directory))
        self.statusBar().showMessage(
            "NMEA connections closed"
            if self.controller.offline
            else (
                self.controller.connection_task.message
                or "Using saved NMEA connections"
            )
        )
        self.syncing = False
        self._refresh_data()
        self.plots.set_position(session.position)
        self.map.set_position(session.position if session.source == "log" else None)
        self.log_map.set_position(session.position if session.source == "log" else None)
        if session.source == "log" and session.log and session.log.points:
            offset = session.log.point_offsets[0] if session.log.point_offsets else 0
            self.log_time.set_time(
                naive_wall_time(session.log.points[0].time)
                + timedelta(seconds=session.position - offset)
            )

    def _refresh_data(self):
        session = self.session
        key = (
            (session.source, id(session.log))
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
        self.plots.set_points(points, offsets, session.source == "log")
        if (
            session.source == "log"
            and session.log
            and (previous is None or previous != key)
        ):
            self.views.setCurrentWidget(self.logs_view)
        self.log_map.set_points(
            points,
            offsets,
            fit=bool(points)
            and (
                previous is None
                or previous[0] != session.source
                or session.source == "log"
            ),
        )
        self.map.set_points(
            points,
            offsets,
            fit=bool(points)
            and (
                previous is None
                or previous[0] != session.source
                or session.source == "log"
            ),
        )

    def _source_changed(self, index):
        if not self.syncing:
            self.session.select_source("live" if index == 0 else "log")

    def _stream_changed(self, active):
        if not self.syncing:
            self.session.set_streaming(active)

    def _play(self, active):
        if not self.syncing:
            self.session.set_playing(active)

    def _speed_changed(self, index):
        if not self.syncing:
            self.session.speed = self.speed.itemData(index)
            self.session.dispatch("changed", None)

    def _record(self, active):
        try:
            if active:
                self.session.start_recording()
            else:
                self.session.stop()
        except OSError as error:
            self.refresh()
            QMessageBox.critical(self, "Cannot record NMEA", str(error))

    def load_log(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load NMEA log",
            str(self.session.recording_directory),
            "NMEA logs (*.log *.nmea *.txt);;All files (*)",
        )
        if path:
            self.controller.load_path(path)

    def _palette_changed(self, palette):
        self.controller.settings.chartPalette = palette
        self.map.queue_draw()

    def _boat_changed(self, boat):
        if boat.is_valid():
            self.instruments.setText(
                f"{boat.latitude:.5f}°, {boat.longitude:.5f}°"
                + (f" · SOG {boat.sog:.1f} kn" if boat.sog is not None else "")
            )
        else:
            self.instruments.setText("Waiting for position")

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

    def _ensure_library(self):
        if self.library_dialog is None:
            self.library_dialog = DataLibrary(self.controller, self)
        self.library_dialog.show()
        self.library_dialog.raise_()
        self.library_dialog.activateWindow()

    def show_library(self):
        self._ensure_library()
        if not self.controller.data.task.busy:
            self.controller.data.scan_forecasts()

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

    def _forecast_loaded(self, grib):
        self.forecast.setDateTime(QDateTime(grib.start_time))
        self.map.queue_draw()
        self.statusBar().showMessage("Forecast imported and enabled", 5000)

    def load_grib(self):
        if self.controller.data.task.busy or self.controller.routing.busy:
            self._ensure_library()
            self.statusBar().showMessage(
                "Wait for the current operation before importing a forecast.", 5000
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import wind forecast", "", FORECAST_FILTER
        )
        if path:
            self._ensure_library()
            self.controller.data.import_forecast(path)

    def closeEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.workspace.close()
        self.navigation_panel.dispose()
        self.polar_workspace.dispose()
        self.live_timer.stop()
        self.controller.close()
        event.accept()
