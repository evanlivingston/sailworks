"""Debounced, private-reader coverage screening for the visible weather models."""

from collections import OrderedDict
from pathlib import Path
from threading import Thread

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from gweatherrouting.core.forecastselection import forecast_source
from gweatherrouting.core.gribsampling import WeatherSampler
from gweatherrouting.core.routingjob import freeze_wind_sources
from gweatherrouting.rendering.windrenderer import DisplayGrib


def coverage_text(models, fields, instant, points, readers):
    """Screen a 3×3 view sample; report missing or partial per-model fields."""
    if not fields:
        return ""
    if not models:
        return "No weather models selected"
    lats, lons = np.asarray(points).T
    messages = []
    for model in models:
        sources = readers.get(model, ())
        sampler = WeatherSampler(sources) if sources else None
        missing = []
        for field in fields:
            values = sampler.sample(field, instant, lats, lons) if sampler else None
            valid = (
                np.isfinite(values).all(axis=1)
                if values is not None
                else np.zeros(len(points), dtype=bool)
            )
            if not valid.all():
                missing.append(field + (" partial" if valid.any() else " unavailable"))
        if missing:
            name = model.removeprefix("NOAA ").removeprefix("CMC ")
            messages.append(name + ": " + ", ".join(missing))
    return "\n".join(messages)


class WeatherCoverage(QObject):
    ready = Signal(object, str)

    def __init__(self, parent, callback):
        super().__init__(parent)
        self.callback = callback
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._start)
        self.ready.connect(self._ready)
        self.key = None
        self.pending = None
        self.running = False
        self.closed = False
        self.readers = OrderedDict()

    def request(self, models, fields, instant, points, sources):
        key = (models, fields, instant, points, tuple(sources))
        if key == self.key or self.closed:
            return
        self.key = key
        self.pending = (key, freeze_wind_sources(sources))
        self.callback("")
        self.timer.start(250)

    def _start(self):
        if self.running or self.pending is None or self.closed:
            return
        (key, sources), self.pending = self.pending, None
        self.running = True

        def worker():
            grouped = self._readers_for(sources)
            try:
                text = coverage_text(key[0], key[1], key[2], key[3], grouped)
            except Exception:
                text = "Weather coverage unavailable"
            while len(self.readers) > 12:
                self.readers.popitem(last=False)[1].close()
            if self.closed:
                self._clear()
            else:
                self.ready.emit(key, text)

        Thread(target=worker, name="weather-coverage", daemon=True).start()

    def _readers_for(self, sources):
        grouped: dict = {}
        for source in sources:
            if self.closed:
                break
            try:
                stat = Path(source.path).stat()
                identity = (source.path, stat.st_mtime_ns, stat.st_size)
                reader = self.readers.get(identity)
                if reader is None:
                    reader = DisplayGrib(source)
                    self.readers[identity] = reader
                self.readers.move_to_end(identity)
                grouped.setdefault(forecast_source(source), []).append(reader)
            except Exception:
                continue
        return grouped

    def _ready(self, key, text):
        self.running = False
        if not self.closed and key == self.key:
            self.callback(text)
        if self.closed:
            self._clear()
        elif self.pending is not None:
            self.timer.start(100)

    def _clear(self):
        for reader in self.readers.values():
            reader.close()
        self.readers.clear()

    def close(self):
        self.closed = True
        self.timer.stop()
        self.pending = None
        if not self.running:
            self._clear()
