"""Elapsed-time log transport; preview never consumes replay packets."""

import math

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QLabel, QSlider, QVBoxLayout, QWidget


class LogScrubber(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.session = controller.session
        self._gesture = None
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setAccessibleName("Log playback position")
        self.slider.setToolTip("Drag to preview; release to seek. Arrow keys seek.")
        self.label = QLabel()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.slider)
        layout.addWidget(self.label)
        self.slider.sliderPressed.connect(self._pressed)
        self.slider.valueChanged.connect(self._value_changed)
        self.slider.sliderReleased.connect(self._released)
        controller.changed.connect(self.refresh)
        self.refresh()

    def _identity(self):
        return (self.session.source, id(self.session.log), self.session._generation)

    def _position(self):
        log = self.session.log
        return log.duration * self.slider.value() / self.slider.maximum() if log else 0

    def _pressed(self):
        self._gesture = self._identity()
        self.controller.preview_log_position(self._position())

    def _value_changed(self, _):
        if self.slider.isSliderDown():
            if self._gesture == self._identity():
                self.controller.preview_log_position(self._position())
        elif self.slider.isEnabled():
            # valueChanged also covers keyboard, wheel and groove clicks.
            self.controller.seek_log_position(self._position())

    def _released(self):
        valid = self._gesture == self._identity()
        self._gesture = None
        if valid and self.slider.isEnabled():
            self.controller.seek_log_position(self._position())

    def refresh(self):
        session = self.session
        enabled = bool(
            session.source == "log"
            and session.log
            and session.log.entries
            and not session.loading
            and not self.controller.closed
        )
        with QSignalBlocker(self.slider):
            if not enabled or (
                self._gesture is not None and self._gesture != self._identity()
            ):
                self._gesture = None
                self.slider.setSliderDown(False)
            self.slider.setEnabled(enabled)
            duration = session.log.duration if session.log else 0
            maximum = max(1, min(2147483647, math.ceil(duration * 1000)))
            self.slider.setRange(0, maximum)
            self.slider.setSingleStep(max(1, round(maximum / max(duration, 1))))
            self.slider.setPageStep(max(1, maximum // 100))
            if not self.slider.isSliderDown():
                self.slider.setValue(
                    round(maximum * session.display_position / duration)
                    if duration
                    else 0
                )
        self.label.setText(
            f"{session.display_position:.1f} / {duration:.1f} s"
            + (" · Seeking…" if session.seeking else "")
            if session.log
            else "No log loaded"
        )
