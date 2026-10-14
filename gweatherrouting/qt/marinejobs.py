"""Cancellable marine acquisition, with UI-thread commits and late-result guards."""

from threading import Event, Thread
from typing import Callable, Optional
from PySide6.QtCore import QObject, Signal, Slot
from gweatherrouting.core.marine.chs import DownloadCancelledError


class MarineJobs(QObject):
    changed = Signal()
    completed = Signal(object, object, object)
    progress = Signal(object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.busy = False
        self.closed = False
        self.message = ""
        self.cancel = Event()
        self.complete: Optional[Callable] = None
        self.completed.connect(self._finished)
        self.progress.connect(self._progress)

    def start(self, message, operation, complete):
        if self.closed or self.busy:
            return False
        self.cancel = Event()
        cancel = self.cancel
        self.busy, self.message, self.complete = True, message, complete
        self.changed.emit()

        def report(message):
            if not self.closed:
                self.progress.emit(cancel, message)

        def run():
            result, error = None, None
            try:
                result = operation(cancel, report)
            except Exception as exc:
                error = exc
            if not self.closed:
                self.completed.emit(cancel, result, error)

        Thread(target=run, name="qt-marine-download", daemon=True).start()
        return True

    @Slot(object, str)
    def _progress(self, cancel, message):
        if (
            not self.closed
            and self.busy
            and self.cancel is cancel
            and not cancel.is_set()
        ):
            self.message = message
            self.changed.emit()

    @Slot(object, object, object)
    def _finished(self, cancel, result, error):
        if self.closed or self.cancel is not cancel:
            return
        self.busy = False
        complete, self.complete = self.complete, None
        if cancel.is_set() or isinstance(error, DownloadCancelledError):
            self.message = "Cancelled. No download was added."
        elif error is not None:
            self.message = "Request failed: " + str(error)
        else:
            try:
                assert complete is not None
                self.message = complete(result)
            except (OSError, ValueError) as exc:
                self.message = "Could not save: " + str(exc)
        self.changed.emit()

    def cancel_download(self):
        if self.busy:
            self.cancel.set()
            self.message = "Cancelling request…"
            self.changed.emit()

    def close(self):
        self.closed = True
        self.cancel.set()
        self.complete = None
