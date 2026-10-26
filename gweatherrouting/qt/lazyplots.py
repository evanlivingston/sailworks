"""Logs plot state without importing Matplotlib until that workspace is visible."""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class LazyLogPlots(QWidget):
    def __init__(self, parent=None, factory=None):
        super().__init__(parent)
        self.factory = factory
        self.plot = None
        self.points, self.offsets, self.playback = (), (), False
        self.position = 0.0
        self.series = {}
        self.logged_fields = ()
        self.selected_logged_fields = set()
        self.time_bounds = None
        self.viewport_fraction = 0.0
        self.viewport_changed = None
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.notice = QLabel("Plots load when the Logs workspace is opened", self)
        self.layout_.addWidget(self.notice)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.materialize)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if self.plot is None:
            self.timer.start(0)

    def hideEvent(self, event):  # noqa: N802
        self.timer.stop()
        super().hideEvent(event)

    def materialize(self):
        if self.plot is not None or not self.isVisible():
            return
        plot = None
        try:
            factory = self.factory
            if factory is None:
                from .plots import LogPlots

                factory = LogPlots
            plot = factory(self)
            for key, enabled in self.series.items():
                plot.set_series(key, enabled)
            plot.set_logged_fields(self.logged_fields, reset=True, redraw=False)
            plot.selected_logged_fields.update(self.selected_logged_fields)
            plot.set_points(self.points, self.offsets, self.playback)
            if self.time_bounds is not None:
                plot.set_time_bounds(*self.time_bounds, reset=True)
            plot.set_viewport_callback(self.viewport_changed)
            if self.viewport_fraction:
                plot.set_viewport_fraction(self.viewport_fraction)
            plot.set_position(self.position)
            self.plot = plot
            self.notice.hide()
            self.layout_.addWidget(plot)
        except Exception as exc:
            if plot is not None:
                plot.deleteLater()
            self.notice.setText(f"Unable to load plots: {exc}. Reopen Logs to retry.")

    def set_points(self, points, offsets, playback):
        self.points, self.offsets, self.playback = (
            tuple(points),
            tuple(offsets),
            playback,
        )
        if self.plot is not None:
            self.plot.set_points(self.points, self.offsets, playback)

    def set_position(self, position):
        self.position = position
        if self.plot is not None:
            self.plot.set_position(position)

    def set_series(self, key, enabled):
        self.series[key] = enabled
        if self.plot is not None:
            self.plot.set_series(key, enabled)

    def set_logged_fields(self, fields, *, reset=False, redraw=True):
        if self.plot is not None:
            self.selected_logged_fields = set(self.plot.selected_logged_fields)
        self.logged_fields = tuple(fields)
        available = {field.key for field in self.logged_fields}
        if reset:
            self.selected_logged_fields.clear()
        else:
            self.selected_logged_fields.intersection_update(available)
        if self.plot is not None:
            self.plot.set_logged_fields(self.logged_fields, reset=reset, redraw=False)
            self.plot.selected_logged_fields = set(self.selected_logged_fields)
            if redraw:
                self.plot.set_points(self.points, self.offsets, self.playback)

    def set_time_bounds(self, start, end, *, reset=False):
        self.time_bounds = (float(start), float(end))
        if self.plot is not None:
            self.plot.set_time_bounds(start, end, reset=reset)

    def set_viewport_fraction(self, fraction):
        self.viewport_fraction = min(1.0, max(0.0, float(fraction)))
        if self.plot is not None:
            self.plot.set_viewport_fraction(self.viewport_fraction)

    def set_viewport_callback(self, callback):
        self.viewport_changed = callback
        if self.plot is not None:
            self.plot.set_viewport_callback(callback)

    def __getattr__(self, name):
        # Delegate to the materialized plot so existing callers keep working
        # once Logs is opened. Before that, unknown attributes stay undefined.
        plot = self.__dict__.get("plot")
        if plot is None:
            raise AttributeError(name)
        return getattr(plot, name)
