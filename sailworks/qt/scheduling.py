"""Deliver worker callbacks on the Qt application thread."""

from PySide6.QtCore import QObject, Qt, Signal, Slot


class UiScheduler(QObject):
    requested = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.active = True
        self.requested.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def submit(self, callback, *args):
        if self.active:
            self.requested.emit(callback, args)

    @Slot(object, object)
    def _deliver(self, callback, args):
        if self.active:
            callback(*args)

    def close(self):
        self.active = False
