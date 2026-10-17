"""Windowless map canvas shared by map interaction and overlay tests."""

from unittest.mock import Mock

from gweatherrouting.core.mapviewport import MercatorProjection


class MapCanvas(MercatorProjection):
    __test__ = False

    def __init__(self):
        self._center_lat = 47.6
        self._center_lon = -122.4
        self._zoom = 12
        self.width = 400
        self.height = 300
        self.queue_draw = Mock()
        self.add_tick_callback = Mock(return_value=1)
        self.remove_tick_callback = Mock()
        self.factor = 1

    def get_allocated_width(self):
        return self.width

    def get_allocated_height(self):
        return self.height

    def get_scale_factor(self):
        return self.factor


class QueuedScheduler:
    """Deliver worker completions on the test thread without a GUI toolkit."""

    def __init__(self):
        from queue import Queue

        self.callbacks = Queue()

    def __call__(self, callback, *args):
        self.callbacks.put((callback, args))

    def drain(self):
        from queue import Empty

        while True:
            try:
                callback, args = self.callbacks.get_nowait()
            except Empty:
                return
            callback(*args)
