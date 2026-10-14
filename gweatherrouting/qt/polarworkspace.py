"""GTK-shaped Polars workspace using the shared Cairo renderer and NMEA history."""

import math
from threading import Thread
from types import SimpleNamespace

import cairo
from PySide6.QtCore import Qt, Signal, Slot, QTimer
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QComboBox,
    QCheckBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QAbstractItemView,
)

from gweatherrouting.core.polarfiles import read_polar
from gweatherrouting.core.polarperformance import wind_band
from gweatherrouting.rendering.polar import PolarRenderer, POLAR_COLORS


class PolarCanvas(QWidget, PolarRenderer):
    def __init__(self, controller, parent=None):
        QWidget.__init__(self, parent)
        self.init_polar_renderer(controller.core.polar_manager, full_circle=True)
        self.setMouseTracking(True)
        self.setMinimumSize(180, 180)

    queue_draw = QWidget.update

    def paintEvent(self, event):  # noqa: N802 - Qt virtual method name
        factor = math.ceil(self.devicePixelRatioF())
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32, self.width() * factor, self.height() * factor
        )
        surface.set_device_scale(factor, factor)
        self.draw_polar(cairo.Context(surface), self.width(), self.height())
        surface.flush()
        image = QImage(
            surface.get_data(),
            surface.get_width(),
            surface.get_height(),
            surface.get_stride(),
            QImage.Format.Format_ARGB32_Premultiplied,
        ).copy()
        image.setDevicePixelRatio(factor)
        painter = QPainter(self)
        painter.drawImage(0, 0, image)
        painter.end()

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.on_mouse_move(
            self, SimpleNamespace(x=event.position().x(), y=event.position().y())
        )

    def leaveEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.on_mouse_leave(self)

    def hideEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.on_mouse_leave(self)
        super().hideEvent(event)


class PerformanceControls(QWidget):
    changed = Signal()

    def __init__(self, controller, canvas, parent=None):
        super().__init__(parent)
        self.controller, self.canvas = controller, canvas
        self.pending_wind = None
        layout = QVBoxLayout(self)
        self.show_points = QCheckBox("Show actual performance")
        self.show_points.setChecked(True)
        layout.addWidget(self.show_points)
        layout.addWidget(QLabel("Boat speed"))
        self.basis = QComboBox()
        self.basis.addItems(["Speed through water", "Speed over ground"])
        self.basis.setToolTip("GPS speed over ground includes the effect of current.")
        layout.addWidget(self.basis)
        layout.addWidget(QLabel("Wind speed"))
        self.wind = QComboBox()
        self.wind.addItem("All wind speeds")
        self.wind.setToolTip(
            "Nearest polar wind speed with midpoint boundaries; out-of-range samples are excluded."
        )
        layout.addWidget(self.wind)
        self.clear = QPushButton("Clear samples")
        self.clear.setToolTip(
            "Clear performance observations only; retain NMEA data and plots."
        )
        self.clear.clicked.connect(self.clear_samples)
        layout.addWidget(self.clear)
        self.count = QLabel()
        self.count.setWordWrap(True)
        layout.addWidget(self.count)
        layout.addStretch()
        self.show_points.toggled.connect(self.control_changed)
        self.basis.currentIndexChanged.connect(self.control_changed)
        self.wind.currentIndexChanged.connect(self.control_changed)
        self.revision = None
        self.refresh()

    def polar_changed(self):
        self.wind.blockSignals(True)
        self.wind.clear()
        self.wind.addItem("All wind speeds")
        if self.canvas.polar:
            for speed in self.canvas.polar.tws:
                self.wind.addItem(f"TWS {speed:g} kt", speed)
        if self.pending_wind is not None and self.canvas.polar is not None:
            self.wind.setCurrentIndex(max(0, self.wind.findData(self.pending_wind)))
            self.pending_wind = None
        self.wind.blockSignals(False)
        self.refresh()

    def refresh(self, *_):
        revision, samples = self.controller.session.active_history.snapshot()
        self.revision = revision
        basis = "stw" if self.basis.currentIndex() == 0 else "sog"
        selected = self.wind.currentIndex() - 1
        wind_filter = selected if selected >= 0 else None
        samples = tuple(p for p in samples if p.basis == basis)
        winds = self.canvas.polar.tws if self.canvas.polar else []
        visible = tuple(
            p
            for p in samples
            if wind_band(p.tws, winds) is not None
            and (wind_filter is None or wind_band(p.tws, winds) == wind_filter)
        )
        self.count.setText(
            f"{len(visible):,} / {len(samples):,} {basis.upper()} samples in range"
        )
        self.canvas.set_performance(
            visible if self.show_points.isChecked() else (), wind_filter
        )

    def control_changed(self, *_):
        self.refresh()
        self.changed.emit()

    def tick(self):
        revision, _ = self.controller.session.active_history.snapshot()
        if revision != self.revision:
            self.refresh()

    def clear_samples(self):
        self.controller.session.active_history.clear()
        self.refresh()

    def save_state(self):
        return {
            "show_points": self.show_points.isChecked(),
            "basis": "sog" if self.basis.currentIndex() == 1 else "stw",
            "wind": self.wind.currentData(),
        }

    def restore_state(self, state):
        if not isinstance(state, dict):
            return
        if isinstance(state.get("show_points"), bool):
            self.show_points.setChecked(state["show_points"])
        if state.get("basis") in ("stw", "sog"):
            self.basis.setCurrentIndex(1 if state["basis"] == "sog" else 0)
        self.pending_wind = state.get("wind")
        if self.canvas.polar is not None:
            self.polar_changed()


class PolarWorkspace(QWidget):
    loaded = Signal(int, str, object, object)
    selection_changed = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.closed = False
        self.generation = 0
        self.name = ""
        self.worker_thread = None
        self.selector = QTableWidget(0, 4)
        self.selector.setHorizontalHeaderLabels(["Filename", "Sail", "Name", "Type"])
        self.selector.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.selector.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.selector.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.selector.horizontalHeader().setStretchLastSection(True)
        self.selector.verticalHeader().hide()
        self.selector.setShowGrid(False)
        self.selector.setColumnWidth(0, 160)
        self.selector.setMinimumWidth(250)
        self.selector.currentCellChanged.connect(self._selected)
        self.chart = PolarCanvas(controller)
        self.performance = PerformanceControls(controller, self.chart)
        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setStyleSheet("QTableWidget { background-color: #303030; }")
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.verticalHeader().hide()
        self.table.setMinimumWidth(100)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.chart)
        self.splitter.setSizes([500, 560])
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, False)
        self.status = QLabel("Select an enabled polar in Polar selector")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.splitter, 1)
        layout.addWidget(self.status)
        self.loaded.connect(self._loaded)
        controller.polars.changed.connect(
            self.refresh_selector, Qt.ConnectionType.QueuedConnection
        )
        controller.changed.connect(self.performance.refresh)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.performance.tick)
        self.timer.start()
        self.names = []
        self.refresh_selector()

    def refresh_selector(self):
        if self.closed:
            return
        manager = self.controller.core.polar_manager
        names = list(manager.polars)
        if names == self.names:
            return
        self.names = names
        selected = self.name if self.name in names else (names[0] if names else "")
        self.selector.blockSignals(True)
        self.selector.setRowCount(len(names))
        for row, name in enumerate(names):
            meta = manager.get_metadata(name)
            for column, text in enumerate(
                (name, meta.get("sail", ""), meta.get("name", ""), meta.get("type", ""))
            ):
                item = QTableWidgetItem(str(text))
                item.setToolTip(str(text))
                self.selector.setItem(row, column, item)
        if selected:
            self.selector.selectRow(names.index(selected))
        self.selector.blockSignals(False)
        if selected != self.name:
            self.select(selected)

    def _selected(self, row, *_):
        if 0 <= row < len(self.names):
            self.select(self.names[row])

    def select(self, name):
        if self.closed:
            return
        self.generation += 1
        token = self.generation
        self.name = name
        if name in self.names:
            self.selector.blockSignals(True)
            self.selector.selectRow(self.names.index(name))
            self.selector.blockSignals(False)
        self.chart.set_polar(None)
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.performance.polar_changed()
        self.selection_changed.emit()
        if not name:
            self.status.setText("Please add/enable a polar file using Polar manager")
            return
        path = self.controller.core.polar_manager.get_path(name)
        self.status.setText("Loading polar: " + name)

        def read():
            polar, error = None, None
            try:
                polar = read_polar(path)
            except Exception as exc:
                error = exc
            if not self.closed:
                self.loaded.emit(token, name, polar, error)

        self.worker_thread = Thread(target=read, name="qt-polar-view", daemon=True)
        self.worker_thread.start()

    @Slot(int, str, object, object)
    def _loaded(self, token, name, polar, error):
        if self.closed or token != self.generation:
            return
        if error is not None:
            self.status.setText("Unable to load polar: " + str(error))
            return
        self.chart.set_polar(polar)
        step = int(len(polar.twa) / 10) if len(polar.twa) > 20 else 1
        indices = list(range(0, len(polar.twa), step))
        self.table.setRowCount(len(indices))
        self.table.setColumnCount(len(polar.tws) + 1)
        self.table.setHorizontalHeaderLabels(
            ["TWA/TWS"] + [str(int(x)) for x in polar.tws]
        )
        for column in range(len(polar.tws)):
            header = self.table.horizontalHeaderItem(column + 1)
            assert header is not None
            header.setForeground(
                QColor.fromRgbF(*POLAR_COLORS[column % len(POLAR_COLORS)])
            )
            self.table.setColumnWidth(column + 1, 48)
        for row, index in enumerate(indices):
            self.table.setItem(
                row, 0, QTableWidgetItem(f"{int(math.degrees(polar.twa[index]))}°")
            )
            for column, speed in enumerate(polar.speed_table[index], 1):
                self.table.setItem(row, column, QTableWidgetItem(str(speed)))
        self.table.resizeColumnToContents(0)
        self.performance.polar_changed()
        self.status.setText("Polar loaded: " + name)

    def dispose(self):
        if self.closed:
            return
        self.closed = True
        self.generation += 1
        self.timer.stop()
        self.controller.changed.disconnect(self.performance.refresh)
