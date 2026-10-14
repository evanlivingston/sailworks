"""Small Qt frontend for evaluating NMEA, plotting, docking, and map integration."""

import os
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStyle,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
)

from .mapcanvas import TrackMap
from .plots import LogPlots, elapsed_offsets


class PreviewWindow(QMainWindow):
    def __init__(self, controller, coastline_path=None):
        super().__init__()
        self.setAutoFillBackground(True)
        self.controller = controller
        self.session = controller.session
        self.syncing = False
        self.data_key = None
        self.setWindowTitle("GWeatherRouting — Qt preview")
        self.resize(1200, 800)
        if sys.platform == "darwin":
            self.setUnifiedTitleAndToolBarOnMac(True)
        self.map = TrackMap(self, coastline_path)
        toolbar = QToolBar("Navigation", self)
        toolbar.setMovable(False)
        toolbar.setAutoFillBackground(True)
        toolbar.setMinimumHeight(32)
        self.addToolBar(toolbar)
        toolbar.addAction("Load NMEA log…", self.load_log)
        toolbar.addSeparator()
        toolbar.addAction("Fit track", lambda: self.map.fit_track())
        self.plots = LogPlots(self)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.map)
        splitter.addWidget(self.plots)
        splitter.setSizes([430, 330])
        self.setCentralWidget(splitter)
        self.nmea_dock = QDockWidget("NMEA", self)
        self.nmea_dock.setObjectName("nmea")
        self.nmea_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.nmea_dock.setWidget(self._nmea_panel())
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.nmea_dock)
        self.menuBar().addMenu("View").addAction(self.nmea_dock.toggleViewAction())
        controller.changed.connect(self.refresh)
        controller.model_changed.connect(self._refresh_data)
        self.live_timer = QTimer(self)
        self.live_timer.setInterval(500)
        self.live_timer.timeout.connect(self._refresh_data)
        self.live_timer.start()
        self.refresh()

    def _nmea_panel(self):
        panel = QWidget()
        panel.setAutoFillBackground(True)
        panel.setMinimumWidth(250)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)
        layout.addWidget(QLabel("Data source"))
        self.source = QComboBox()
        self.source.addItems(["Live connections", "Loaded log"])
        self.source.currentIndexChanged.connect(self._source_changed)
        layout.addWidget(self.source)
        self.streaming = QCheckBox("Stream live NMEA")
        self.streaming.toggled.connect(self._stream_changed)
        layout.addWidget(self.streaming)
        load = QPushButton("Load NMEA log…")
        load.clicked.connect(self.load_log)
        self.load_button = load
        layout.addWidget(load)
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
        self.record.setFixedSize(46, 36)
        self.record.setCheckable(True)
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
        self.record.setStyleSheet(
            "QToolButton:checked { background: #f5c9c9; border: 1px solid #c33; }"
        )
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
        self.recording_file.setWordWrap(True)
        self.recording_file.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.recording_file)
        layout.addStretch()
        return panel

    def _button(self, icon, label, checkable=False):
        button = QToolButton()
        button.setIcon(self.style().standardIcon(icon))
        button.setToolTip(label)
        button.setAccessibleName(label)
        button.setCheckable(checkable)
        button.setFixedSize(46, 36)
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
        self.play.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_MediaPause
                if session.playing
                else QStyle.StandardPixmap.SP_MediaPlay
            )
        )
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
            "Offline preview"
            if self.controller.offline
            else "Using saved NMEA connections"
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

    def closeEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.live_timer.stop()
        self.controller.close()
        event.accept()
