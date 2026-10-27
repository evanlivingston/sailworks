"""Small asynchronous state holder for recent ICOADS observations."""

from PySide6.QtCore import QObject, QThread, Signal

_ORPHANED_THREADS = set()


def _release_thread(thread):
    _ORPHANED_THREADS.discard(thread)


class _FetchThread(QThread):
    completed = Signal(int, object, object)
    progress = Signal(int, str)

    def __init__(self, generation, days, parent=None):
        super().__init__(parent)
        self.generation = generation
        self.days = days

    def run(self):
        try:
            from gweatherrouting.core.marine.icoads import fetch_recent

            reports = fetch_recent(
                days=self.days,
                progress=lambda message: self.progress.emit(self.generation, message),
            )
            self.completed.emit(self.generation, reports, None)
        except Exception as error:
            self.completed.emit(self.generation, (), str(error))


class IcoadsState(QObject):
    """Fetches only on initial enable or explicit refresh; never polls."""

    changed = Signal()

    def __init__(self, parent=None, days=3):
        super().__init__(parent)
        self.days = max(1, min(int(days), 7))
        self.visible = False
        self.reports = ()
        self.status = ""
        self.error = ""
        self.loading = False
        self._generation = 0
        self._threads = set()

    def set_visible(self, visible):
        visible = bool(visible)
        if visible and not self.visible and not self.reports and not self.status:
            self.refresh()
        self.visible = visible
        self.changed.emit()

    def refresh(self):
        if self.loading:
            return
        self._generation += 1
        generation = self._generation
        self.loading = True
        self.status = "Loading recent ship observations…"
        self.error = ""
        thread = _FetchThread(generation, self.days, self)
        self._threads.add(thread)
        thread.completed.connect(self._finished)
        thread.progress.connect(self._progress)
        thread.finished.connect(lambda thread=thread: self._discard(thread))
        self.changed.emit()
        thread.start()

    def _progress(self, generation, message):
        if generation != self._generation or not self.loading:
            return
        self.status = str(message)
        self.changed.emit()

    def _discard(self, thread):
        self._threads.discard(thread)
        thread.deleteLater()

    def _finished(self, generation, reports, error):
        if generation != self._generation:
            return
        self.loading = False
        self.reports = tuple(reports or ())
        self.error = error or ""
        if self.error:
            self.status = "Could not load NOAA ICOADS observations: " + self.error
        elif self.reports:
            self.status = (
                f"{len(self.reports)} reports loaded. Observations may be delayed."
            )
        else:
            self.status = "No ship observations found in the recent data window."
        self.changed.emit()

    def close(self):
        self._generation += 1
        self.loading = False
        for thread in tuple(self._threads):
            thread.requestInterruption()
            # A blocking URL read cannot be interrupted safely. Detach it so
            # closing the map cannot destroy a running QThread; its own
            # finished connection owns final cleanup.
            thread.setParent(None)
            _ORPHANED_THREADS.add(thread)
            thread.finished.connect(lambda thread=thread: _release_thread(thread))
            thread.finished.connect(thread.deleteLater)
