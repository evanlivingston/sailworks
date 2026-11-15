"""A reusable progress bar that tweens and shows it is still working.

Qt's ``QProgressBar`` jumps straight to a new value, so a slow job looks
frozen between updates. This widget animates the fill towards the new value
and adds a faint moving highlight while the job is active, which reads as
"working" without inventing progress.

Use it anywhere a determinate count is shown:

    bar = TweenedProgressBar()
    bar.set_count(done, total)
    bar.set_active(True)
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QTimer, QVariantAnimation, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import QProgressBar, QSizePolicy

#: How long a value change takes to travel, in milliseconds.
TWEEN_MS = 220
#: Highlight sweep period, in milliseconds.
PULSE_MS = 1400
#: Progress bar height that fits a count drawn inside it.
BAR_HEIGHT = 16


class TweenedProgressBar(QProgressBar):
    """Determinate progress with a tween and an idle "still working" sweep."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRange(0, 100)
        self.setValue(0)
        self.setTextVisible(True)
        self.setFormat("")
        self.setFixedHeight(BAR_HEIGHT)
        self.setMinimumWidth(60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._tween = QVariantAnimation(self)
        self._tween.setDuration(TWEEN_MS)
        self._tween.valueChanged.connect(self._tween_step)

        # 0..1 position of the highlight sweep; only painted while active.
        self._phase = 0.0
        self._active = False
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    # -- value ------------------------------------------------------------
    def set_count(self, done, total, *, animate=True):
        """Show ``done`` of ``total``; an unknown total spins instead."""
        done = max(0, int(done or 0))
        total = max(0, int(total or 0))
        if total <= 0:
            self._tween.stop()
            self.setRange(0, 0)
            self.setFormat("")
            return
        self.setRange(0, total)
        self.setFormat("%v / %m")
        self.set_value(min(done, total), animate=animate)

    def set_value(self, value, *, animate=True):
        """Move the fill to ``value``, tweening unless told otherwise."""
        value = int(value)
        # Off-screen widgets (and the first paint) snap: animating something
        # nobody can see would only make state harder to inspect.
        if not animate or not self.isVisible() or self.minimum() == self.maximum():
            self._tween.stop()
            super().setValue(value)
            return
        start = self.value()
        if value == start:
            return
        self._tween.stop()
        self._tween.setStartValue(start)
        self._tween.setEndValue(value)
        self._tween.start()

    def _tween_step(self, value):
        super().setValue(int(value))

    # -- activity ---------------------------------------------------------
    def set_active(self, active):
        """Run the highlight sweep while the job is still working."""
        active = bool(active)
        if active == self._active:
            return
        self._active = active
        if active:
            self._timer.start()
        else:
            self._timer.stop()
            self._phase = 0.0
            self.update()

    def is_active(self):
        return self._active

    def _tick(self):
        self._phase = (self._phase + 33.0 / PULSE_MS) % 1.0
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._active or self.minimum() == self.maximum():
            return
        ratio = 0.0
        span = self.maximum() - self.minimum()
        if span > 0:
            ratio = (self.value() - self.minimum()) / span
        if ratio <= 0.0:
            return
        width = self.width() - 2
        filled = max(2, int(width * ratio))
        # A soft band travelling across the filled part. It is decoration, not
        # a second progress signal, so it never implies more completion.
        painter = QPainter(self)
        try:
            painter.setClipRect(1, 1, filled, self.height() - 2)
            band = max(24, filled // 3)
            centre = -band + self._phase * (filled + 2 * band)
            gradient = QLinearGradient(
                QPointF(centre - band, 0), QPointF(centre + band, 0)
            )
            gradient.setColorAt(0.0, QColor(255, 255, 255, 0))
            gradient.setColorAt(0.5, QColor(255, 255, 255, 38))
            gradient.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.fillRect(1, 1, filled, self.height() - 2, gradient)
        finally:
            painter.end()
