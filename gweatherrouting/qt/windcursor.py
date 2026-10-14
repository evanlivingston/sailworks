"""Qt timer adapter for the shared private-GRIB cursor sampler."""

from PySide6.QtCore import QTimer
from gweatherrouting.rendering.windcursor import WindCursorSampler as SharedSampler
from gweatherrouting.rendering.windcursor import CursorGrib as DisplayGrib


class WindCursorSampler(SharedSampler):
    def __init__(self, parent, dispatch):
        def call_later(milliseconds, callback):
            timer = QTimer(parent)
            timer.setSingleShot(True)

            def fire():
                timer.deleteLater()
                callback()

            timer.timeout.connect(fire)
            timer.start(milliseconds)
            return timer

        def cancel_later(timer):
            timer.stop()
            timer.deleteLater()

        super().__init__(
            call_later, cancel_later, dispatch, lambda source: DisplayGrib(source)
        )
