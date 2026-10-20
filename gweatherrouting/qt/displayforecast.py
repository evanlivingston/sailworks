"""Controller-owned, display-only forecast choice and asynchronous metadata."""

from pathlib import Path
from threading import Thread

from PySide6.QtCore import QObject, Signal

from gweatherrouting.rendering.displayforecast import inspect_forecasts


class DisplayForecastSelection(QObject):
    changed = Signal()
    inspected = Signal(object, object, str)

    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.selected_key = controller.settings.get("displayForecast", "")
        self.choices = ()
        self.error = ""
        self.loading = False
        self.closed = False
        self._signature = None
        self._running = False
        self.inspected.connect(self._ready)
        controller.data.changed.connect(self.refresh)
        self.refresh()

    def sources(self):
        """The only display-source accessor; absent choices never substitute."""
        return tuple(
            choice for choice in self.choices if choice.key == self.selected_key
        )

    @property
    def status(self):
        sources = self.sources()
        if sources:
            return sources[0].label
        if self.loading:
            return "Forecast metadata loading…"
        return (
            "Selected forecast unavailable"
            if self.selected_key
            else "No display forecast selected"
        )

    def choose(self, key):
        if key and not any(choice.key == key for choice in self.choices):
            raise ValueError("Display forecast is unavailable")
        self.controller.settings["displayForecast"] = key
        self.selected_key = key
        self.changed.emit()

    def refresh(self):
        if self.closed:
            return
        manager = self.controller.core.grib_manager
        entries = {}
        for source in (*manager.local_gribs, *manager.gribs):
            try:
                stat = Path(source.path).stat()
                entries[str(source.path)] = (
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                )
            except OSError:
                continue
        signature = tuple(sorted(entries.items()))
        if signature == self._signature:
            return
        self._signature = signature
        # Immediately retire old publications on removal/replacement, even while
        # an earlier metadata request is still finishing.
        self.choices = tuple(
            c for c in self.choices if entries.get(c.path) == c.version
        )
        self.loading = True
        self.changed.emit()
        self._start()

    def _start(self):
        if self.closed or self._running:
            return
        self._running = True
        signature = self._signature

        def worker():
            choices, error = inspect_forecasts(signature)
            if not self.closed:
                self.inspected.emit(signature, choices, error)

        Thread(target=worker, name="display-forecast-metadata", daemon=True).start()

    def _ready(self, signature, choices, error):
        self._running = False
        if self.closed:
            return
        if signature != self._signature:
            self._start()
            return
        self.choices, self.error, self.loading = choices, error, False
        self.changed.emit()

    def close(self):
        self.closed = True
        self.choices = ()
