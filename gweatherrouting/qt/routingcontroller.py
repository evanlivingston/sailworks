"""Qt lifecycle around a cancellable worker using the shared routing engine."""

import time
from threading import Event, Thread

from PySide6.QtCore import QObject, Signal, Slot
from weatherrouting import RoutingNoWindError

from gweatherrouting.core.geo.routing import Routing
from gweatherrouting.core.routingjob import (
    RoutingCancelledError,
    calculate_route,
    freeze_wind_sources,
)


class RoutingController(QObject):
    changed = Signal()
    preview_changed = Signal(object)
    completed = Signal(object)
    _progress = Signal(int, float, object)
    _finished = Signal(int, object, object)

    def __init__(self, session_controller):
        super().__init__(session_controller)
        self.session_controller = session_controller
        self.core = session_controller.core
        self.busy = False
        self.closed = False
        self.cancel_event = Event()
        self.generation = 0
        self.percent = 0.0
        self.message = "Wind-only routing"
        self.error = ""
        self.request = None
        self.worker_thread = None
        self._progress.connect(self._apply_progress)
        self._finished.connect(self._finish)

    def start(self, request):
        if self.busy or self.closed:
            return False
        if (
            self.session_controller.data.task.busy
            or self.session_controller.polars.task.busy
        ):
            self.error = (
                "Wait for the Data Library operation to finish before calculating."
            )
            self.message = self.error
            self.changed.emit()
            return False
        sources = freeze_wind_sources(self.core.grib_manager.gribs)
        if not sources:
            self.error = "Open a wind forecast before calculating a route."
            self.message = self.error
            self.changed.emit()
            return False
        self.generation += 1
        token = self.generation
        self.request = request
        self.cancel_event = Event()
        cancel = self.cancel_event
        self.busy, self.error, self.percent = True, "", 0.0
        self.message = "Calculating…"
        providers = tuple(
            self.session_controller.charts.get_line_point_validity_providers()
        )
        self.changed.emit()
        self.preview_changed.emit(())

        def worker():
            last_update = 0.0

            def progress(percent, points):
                nonlocal last_update
                now = time.monotonic()
                if not self.closed and (now - last_update >= 0.15 or percent >= 100):
                    last_update = now
                    self._progress.emit(token, percent, points)

            result, error = None, None
            try:
                result = calculate_route(
                    self.core, request, sources, providers, cancel, progress
                )
            except Exception as exc:
                error = exc
            if not self.closed:
                self._finished.emit(token, result, error)

        self.worker_thread = Thread(target=worker, name="qt-routing", daemon=True)
        self.worker_thread.start()
        return True

    def cancel(self):
        if self.busy:
            self.cancel_event.set()
            self.message = "Cancelling…"
            self.changed.emit()

    @Slot(int, float, object)
    def _apply_progress(self, token, percent, points):
        if (
            self.closed
            or not self.busy
            or token != self.generation
            or self.cancel_event.is_set()
        ):
            return
        self.percent = percent
        self.message = f"Calculating: approximately {percent:.0f}%"
        self.preview_changed.emit(points)
        self.changed.emit()

    @Slot(int, object, object)
    def _finish(self, token, result, error):
        if self.closed or token != self.generation:
            return
        self.busy = False
        self.preview_changed.emit(())
        if self.cancel_event.is_set() or isinstance(error, RoutingCancelledError):
            self.message = "Routing cancelled"
        elif error is not None:
            self.error = str(error) or (
                "Choose a wind forecast covering the route area and time."
                if isinstance(error, RoutingNoWindError)
                else type(error).__name__
            )
            self.message = "Routing failed: " + self.error
        else:
            assert self.request is not None
            collection = self.core.routingManager
            route = Routing(
                collection.get_unique_name(self.request.name),
                list(result.points),
                list(result.isochrones),
                collection=collection,
                provenance=result.provenance,
            )
            try:
                collection.append(route)
                collection.set_active(route)
            except OSError as exc:
                self.error = f"Route calculated, but could not be saved: {exc}"
                self.message = self.error
            else:
                self.percent = 100
                self.message = "Route calculated and saved · wind only"
            self.completed.emit(route)
        self.changed.emit()

    def close(self):
        self.closed = True
        self.cancel_event.set()
        self.generation += 1
