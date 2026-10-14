"""Small Qt frontend for evaluating NMEA, plotting, docking, and map integration."""

import os
import sys

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
    QStyle,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
)

from gweatherrouting.rendering.style import Style
from gweatherrouting.common import resource_path
from .theme import section_label, symbol_icon, transport_icon
from .mapcanvas import TrackMap
from .routingpanel import RoutingPanel
from .settingsdialog import SettingsDialog
from .datalibrary import DataLibrary, FORECAST_FILTER
from .plots import LogPlots, elapsed_offsets
from .workspace import WorkspacePreferences


class PreviewWindow(QMainWindow):
    def __init__(self, controller, workspace_path=None):
        super().__init__()
        self.setAutoFillBackground(True)
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
        toolbar = QToolBar("Navigation", self)
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
        self.views = QTabWidget()
        self.views.setDocumentMode(True)
        self.views.addTab(self.map, "Charts")
        self.views.addTab(self.plots, "Logs")
        self.setCentralWidget(self.views)
        self.nmea_dock = QDockWidget("NMEA", self)
        self.nmea_dock.setObjectName("nmea")
        self.nmea_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.nmea_dock.setWidget(self._nmea_panel())
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.nmea_dock)
        self.routing_panel = RoutingPanel(controller)
        self.routing_dock = QDockWidget("Routing", self)
        self.routing_dock.setObjectName("routing")
        self.routing_dock.setWidget(self.routing_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.routing_dock)
        self.tabifyDockWidget(self.nmea_dock, self.routing_dock)
        self.setTabPosition(
            Qt.DockWidgetArea.AllDockWidgetAreas, QTabWidget.TabPosition.North
        )
        self.nmea_dock.raise_()
        self.resizeDocks(
            [self.nmea_dock, self.routing_dock], [350, 350], Qt.Orientation.Horizontal
        )
        file_menu = self.menuBar().addMenu("File")
        open_log = file_menu.addAction(
            symbol_icon("document-open-symbolic"), "Open NMEA log…", self.load_log
        )
        open_log.setShortcut(QKeySequence.StandardKey.Open)
        file_menu.addAction(open_forecast)
        file_menu.addAction("Data Library…", self.show_library)
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
        view_menu.addAction(toolbar.toggleViewAction())
        toolbar.addSeparator()
        toolbar.addAction("NMEA", lambda: self._show_panel(self.nmea_dock))
        toolbar.addAction("Routing", lambda: self._show_panel(self.routing_dock))
        fit_menu = self.menuBar().addMenu("Navigate")
        fit_menu.addAction("Fit NMEA track", self.map.fit_track)
        fit_menu.addAction("Fit route", self.map.fit_route)
        toolbar.addAction("Fit view", self._fit_view)
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

    def _route_selected(self, coordinates):
        route = self.routing_panel.routes.currentData()
        speeds = (
            tuple(point[5] or 0 for point in route.points) if route is not None else ()
        )
        self.map.set_route(coordinates, speeds)

    def _show_panel(self, dock):
        dock.show()
        dock.raise_()
        if hasattr(self, "workspace"):
            self.workspace._dock_activated(dock)
        if dock is self.routing_dock:
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
            self.views.setCurrentWidget(self.plots)
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
        self.live_timer.stop()
        self.controller.close()
        event.accept()
