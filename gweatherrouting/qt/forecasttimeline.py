"""GTK-style forecast transport and scrubber over the shared wall-time clock."""

import math
from datetime import datetime

from PySide6.QtCore import QDateTime, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gweatherrouting.core.forecasttimeline import (
    TIME_UNITS,
    forecast_bounds,
    forecast_intervals,
    step_forecast_time,
)
from .theme import symbol_icon


class ForecastTimeline(QWidget):
    step_changed = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.clock = controller.time_control
        self.marine = controller.core.marine_manager
        self.bounds = None
        self._signature = None
        self._syncing = False
        self._pending = None
        self.closed = False
        self.seconds = 900
        self.setMinimumWidth(370)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setStyleSheet("QToolButton { padding: 2px; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(2)
        row = QHBoxLayout()
        row.setSpacing(2)
        self.backward_button = self._button(
            row,
            "media-seek-backward-symbolic",
            "Step forecast backward",
            lambda: self.step(-self.seconds),
        )
        self.pause_button = self._button(
            row, "media-playback-pause-symbolic", "Pause forecast", self.pause
        )
        self.play_button = self._button(
            row, "media-playback-start-symbolic", "Play forecast", self.play
        )
        self.play_button.setCheckable(True)
        self.forward_button = self._button(
            row,
            "media-seek-forward-symbolic",
            "Step forecast forward",
            lambda: self.step(self.seconds),
        )
        self.now_button = self._button(
            row, "go-bottom-symbolic", "Select now", self.now
        )
        self.select_button = self._button(
            row,
            "preferences-system-time-symbolic",
            "Set forecast date and time",
            self.select_time,
        )
        self.step_combo = QComboBox()
        self.step_combo.addItems([label for label in TIME_UNITS if label != "1d"])
        self.step_combo.setCurrentText("15m")
        self.step_combo.setAccessibleName("Forecast step interval")
        self.step_combo.setToolTip(
            "Forecast time advanced by each step; playback takes one step per second"
        )
        self.step_combo.currentTextChanged.connect(self._step_changed)
        row.addWidget(self.step_combo)
        self.time_label = QLabel()
        self.time_label.setMinimumWidth(0)
        self.time_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(self.time_label, 1)
        layout.addLayout(row)
        self.caption = QLabel()
        self.caption.setProperty("secondary", True)
        self.caption.setFixedHeight(16)
        self.caption.setMinimumWidth(0)
        self.caption.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.caption)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1)
        self.slider.setAccessibleName("Forecast time")
        self.slider.setToolTip("Scrub forecast time; dragging pauses forecast playback")
        self.slider.valueChanged.connect(self._slide)
        self.slider.sliderPressed.connect(self.pause)
        self.slider.sliderReleased.connect(self.commit)
        layout.addWidget(self.slider)
        endpoints = QHBoxLayout()
        self.start_label = QLabel()
        self.end_label = QLabel()
        for label in (self.start_label, self.end_label):
            label.setProperty("secondary", True)
            label.setMinimumWidth(0)
            label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
        self.end_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        endpoints.addWidget(self.start_label, 1)
        endpoints.addWidget(self.end_label, 1)
        layout.addLayout(endpoints)
        self.play_timer = QTimer(self)
        self.play_timer.setInterval(1000)
        self.play_timer.timeout.connect(self.tick)
        self.commit_timer = QTimer(self)
        self.commit_timer.setSingleShot(True)
        self.commit_timer.setInterval(16)
        self.commit_timer.timeout.connect(self.commit)
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(500)
        self.poll_timer.timeout.connect(self.refresh)
        self.clock.connect("time_change", self._time_changed)
        self.marine.connect("changed", self._marine_changed)
        self.marine.fields.connect("changed", self._marine_changed)
        self.poll_timer.start()
        self.pause()
        self.refresh()

    @staticmethod
    def _button(row, icon, title, callback):
        button = QToolButton()
        button.setIcon(symbol_icon(icon))
        button.setAccessibleName(title)
        button.setToolTip(title)
        button.setAutoRaise(False)
        button.setFixedSize(28, 28)
        button.clicked.connect(callback)
        row.addWidget(button)
        return button

    @property
    def playing(self):
        return self.play_timer.isActive()

    def _step_changed(self, label):
        if label in TIME_UNITS:
            self.seconds = TIME_UNITS[label]
            self.step_changed.emit()

    def _marine_changed(self, *_):
        self.controller.scheduler.submit(self.refresh)

    def refresh(self, *_):
        if self.closed:
            return
        intervals = forecast_intervals(self.controller.core.grib_manager, self.marine)
        if intervals == self._signature:
            return
        self._signature = intervals
        self.bounds = forecast_bounds(intervals)
        self.cancel_commit()
        self._syncing = True
        if self.bounds:
            start, end = self.bounds
            # Relative seconds avoid Qt's signed-32-bit epoch limit after 2038.
            self.slider.setRange(0, max(1, min(2**31 - 1, math.ceil(end - start))))
            self.slider.setSingleStep(60)
            self.slider.setPageStep(3600)
            self.slider.setEnabled(end > start)
            for label, stamp in ((self.start_label, start), (self.end_label, end)):
                local = datetime.fromtimestamp(stamp).astimezone()
                label.setText(local.strftime("%d %b %H:%M %Z"))
                label.setToolTip(local.isoformat())
        else:
            self.slider.setEnabled(False)
            self.start_label.clear()
            self.end_label.clear()
        self._syncing = False
        self._time_changed(self.clock.time)

    def _time_changed(self, instant):
        if self.closed:
            return
        self.cancel_commit()
        self.time_label.setText(
            "<b>%s</b>  %s"
            % (instant.strftime("%H:%M"), instant.astimezone().strftime("%a %d %b %Z"))
        )
        self.time_label.setToolTip(instant.astimezone().isoformat())
        self._syncing = True
        if self.bounds:
            start, end = self.bounds
            span = end - start
            value = (
                0
                if not span
                else round((instant.timestamp() - start) * self.slider.maximum() / span)
            )
            self.slider.setValue(max(0, min(self.slider.maximum(), value)))
            self.caption.setText(
                "Selected time is outside the loaded forecast range"
                if not start <= instant.timestamp() <= end
                else ""
            )
        else:
            self.caption.setText(
                "Load wind or download tides / currents to enable scrubbing"
            )
        self.caption.setToolTip(self.caption.text())
        self._syncing = False

    def _slide(self, value):
        if self._syncing or not self.bounds or not self.slider.isEnabled():
            return
        self.pause()
        start, end = self.bounds
        self._pending = start + value * (end - start) / self.slider.maximum()
        if not self.commit_timer.isActive():
            self.commit_timer.start()

    def cancel_commit(self):
        self.commit_timer.stop()
        self._pending = None

    def commit(self):
        stamp = self._pending
        self.cancel_commit()
        if stamp is not None and not self.closed:
            self.pause()
            self.clock.set_time(datetime.fromtimestamp(stamp))

    def step(self, seconds):
        if self.closed:
            return
        self.cancel_commit()
        step_forecast_time(self.clock, seconds, self.bounds)

    def play(self):
        if self.closed:
            return
        self.cancel_commit()
        if not self.playing:
            self.play_timer.start()
        self.play_button.setChecked(True)
        self.pause_button.setEnabled(True)

    def pause(self):
        self.play_timer.stop()
        self.play_button.setChecked(False)
        self.pause_button.setEnabled(False)

    def tick(self):
        if self.closed:
            return False
        self.step(self.seconds)
        if self.bounds and self.clock.time.timestamp() >= self.bounds[1]:
            self.pause()
        return self.playing

    def now(self):
        if self.closed:
            return
        self.pause()
        self.cancel_commit()
        self.clock.now()

    def select_time(self):
        self.pause()
        self.cancel_commit()
        dialog = QDialog(self)
        dialog.setWindowTitle("Time Picker")
        layout = QVBoxLayout(dialog)
        editor = QDateTimeEdit(QDateTime(self.clock.time), dialog)
        editor.setCalendarPopup(True)
        editor.setDisplayFormat("yyyy-MM-dd HH:mm")
        editor.setAccessibleName("Forecast date and time (local)")
        layout.addWidget(editor)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.addButton("Set", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted and not self.closed:
            self.clock.set_time(editor.dateTime().toPython())
        dialog.deleteLater()

    def hideEvent(self, event):  # noqa: N802 - Qt virtual method name
        # QWidgetAction can hide its widget again while the toolbar is being
        # destroyed, after Qt has already deleted the transport's children.
        if not self.closed:
            self.pause()
            self.cancel_commit()
        super().hideEvent(event)

    def dispose(self):
        if self.closed:
            return
        self.closed = True
        self.pause()
        self.cancel_commit()
        self.poll_timer.stop()
        self.clock.disconnect("time_change", self._time_changed)
        self.marine.disconnect("changed", self._marine_changed)
        self.marine.fields.disconnect("changed", self._marine_changed)
