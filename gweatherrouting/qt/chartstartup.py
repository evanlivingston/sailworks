"""Opt-in GUI-owned chart initialization after initial weather preparation.

No resource handles cross threads. Existing loaders run one per event turn, with
their resulting chart list staged until atomic publication. This does not make
an individual loader nonblocking; stage durations are recorded for evaluation.
"""

import time
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal


class ChartStartup(QObject):
    changed = Signal()
    finished = Signal()

    def __init__(self, controller):
        super().__init__(controller)
        self.owner = controller
        self.state = "waiting"
        self.message = "Weather loading • charts pending"
        self.canvas: Any = None
        self.began: float | None = None
        self.weather_current_before_charts = False
        self.jobs = None
        self.staged = []
        self.stages = []
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.advance)
        # Existing guards prevent routing against an incomplete shoreline list,
        # and prevent conflicting library/map-data mutation during initialization.
        self.owner.map_data.task.busy = True
        self.owner.map_data.task.message = "Loading saved charts…"
        self.owner.map_data.message = "Loading saved charts…"

    def start(self, canvas):
        if self.began is None and self.state == "waiting":
            self.canvas = canvas
            self.began = time.monotonic()
            self.timer.start(0)

    def advance(self):
        if self.state in ("closed", "ready") or self.owner.closed:
            return
        if self.jobs is None:
            if self.canvas is None or self.began is None:
                return
            wind = self.canvas.wind
            current = (
                wind.prepared_view_status(self.canvas)["current"]
                if wind is not None
                else None
            )
            weather_requested = getattr(
                self.canvas, "wind_visible", True
            ) or self.owner.settings.get("gribIsobarsVisible", True)
            visible = getattr(self.canvas, "isVisible", lambda: True)()
            # Empty/unavailable forecasts must not keep charts hidden indefinitely.
            # 'current' is preparation identity/geometry, NOT data availability.
            if (
                not current
                and weather_requested
                and visible
                and self.owner.display_forecast.sources()
                and time.monotonic() - self.began < 5
            ):
                self.timer.start(25)
                return
            self.weather_current_before_charts = current
            self.state = "loading"
            self.message = (
                "Weather prepared • charts loading"
                if current
                else "Charts loading • no prepared weather frame"
            )
            self.jobs = [("base", self.owner.charts.load_base_chart, None)]
            for kind in ("vector", "raster", "mbtiles"):
                loader = getattr(self.owner.charts, "load_" + kind + "_layer")
                self.jobs.extend(
                    (entry["path"], loader, dict(entry))
                    for entry in self.owner.settings[kind + "Charts"]
                )
            self.changed.emit()
            # Allow the already-queued weather paint before loading any chart.
            self.timer.start(100)
            return
        if not self.jobs:
            self.owner.charts.charts = self.staged
            self.state = "ready"
            self.message = (
                "Charts initialized"
                if not self.owner.chart_errors
                else "Charts initialized with errors"
            )
            self.owner.map_data.task.busy = False
            self.owner.map_data.task.message = self.message
            self.owner.map_data.message = self.message
            self.owner.map_data.task.changed.emit()
            self.owner.data.redraw()
            self.owner.data.changed.emit()
            self.changed.emit()
            self.finished.emit()
            return
        name, loader, entry = self.jobs.pop(0)
        visible = self.owner.charts.charts
        self.owner.charts.charts = self.staged
        began = time.monotonic()
        error = None
        try:
            if entry is None:
                loader()
            else:
                loader(entry["path"], entry.get("metadata"), entry.get("enabled", True))
        except Exception as exc:
            error = str(exc)
            self.owner.chart_errors.append(f"{name}: {error}")
        finally:
            self.staged = self.owner.charts.charts
            self.owner.charts.charts = visible
        self.stages.append(
            dict(source=name, seconds=time.monotonic() - began, error=error)
        )
        self.message = (
            f"Charts loading • {len(self.stages)} initialized, {len(self.jobs)} pending"
        )
        self.changed.emit()
        self.timer.start(10)

    def close(self):
        if self.state == "closed":
            return
        self.timer.stop()
        self.state = "closed"
        self.jobs = []
        self.canvas = None
