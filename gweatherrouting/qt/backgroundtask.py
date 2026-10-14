"""Single background operation with UI-thread completion and shutdown guards."""

from threading import Thread

from PySide6.QtCore import QObject, Signal, Slot


class BackgroundTask(QObject):
    changed = Signal()
    finished = Signal(object, object)
    _completed = Signal(object, object)
    _progress = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.busy = False
        self.closed = False
        self.message = ""
        self.percent = -1
        self.worker_thread = None
        self._completed.connect(self._finish)
        self._progress.connect(self._set_progress)

    def start(self, message, operation):
        if self.busy or self.closed:
            return False
        self.busy, self.message, self.percent = True, message, -1
        self.changed.emit()

        def worker():
            result, error = None, None
            try:
                result = operation(self.progress)
            except Exception as exc:
                error = exc
            if not self.closed:
                self._completed.emit(result, error)

        self.worker_thread = Thread(target=worker, name="qt-data", daemon=True)
        self.worker_thread.start()
        return True

    def progress(self, value):
        if not self.closed:
            self._progress.emit(round(value))

    @Slot(int)
    def _set_progress(self, value):
        if not self.closed:
            self.percent = max(0, min(100, value))
            self.changed.emit()

    @Slot(object, object)
    def _finish(self, result, error):
        if self.closed:
            return
        self.busy = False
        self.message = str(error) if error is not None else "Done"
        self.finished.emit(result, error)
        self.changed.emit()

    def close(self, wait=False):
        self.closed = True
        if wait and self.worker_thread is not None:
            self.worker_thread.join()
