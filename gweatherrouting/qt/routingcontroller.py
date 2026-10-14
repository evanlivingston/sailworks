"""Qt lifecycle around the shared engines, scenarios and current-coverage workflow."""

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
from gweatherrouting.core.marine.chs import DownloadCancelledError


class RoutingController(QObject):
    changed = Signal()
    preview_changed = Signal(object)
    completed = Signal(object)
    dismiss_preflight = Signal()
    _progress = Signal(int, float, object)
    _message = Signal(int, str)
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
        self.scenario_index, self.scenario_count, self.saved_count = 0, 1, 0
        self._progress.connect(self._apply_progress)
        self._message.connect(self._apply_message)
        self._finished.connect(self._finish)
        self.confirm = lambda report: False

    def start(self, request, scenarios=()):
        if self.busy or self.closed:
            return False
        if (
            self.session_controller.data.task.busy
            or self.session_controller.polars.task.busy
            or self.session_controller.map_data.task.busy
        ):
            return self.fail(
                "Wait for map-data and Data Library operations to finish before calculating."
            )
        sources = freeze_wind_sources(self.core.grib_manager.gribs)
        if not sources and not (request.use_currents and request.auto_download):
            return self.fail("Open a wind forecast before calculating a route.")
        self.generation += 1
        token = self.generation
        self.request = request
        self.cancel_event = Event()
        cancel = self.cancel_event
        self.busy, self.error, self.percent = True, "", 0.0
        self.message = "Calculating…"
        self.scenario_index, self.scenario_count, self.saved_count = (
            0,
            len(scenarios) or 1,
            0,
        )
        providers = tuple(
            self.session_controller.charts.get_line_point_validity_providers()
        )
        selection = (
            self.core.marine_manager.fields.snapshot() if request.use_currents else None
        )
        self.changed.emit()
        self.preview_changed.emit(())

        def worker():
            self._run_requests(
                token, request, scenarios, sources, providers, cancel, selection
            )

        self.worker_thread = Thread(target=worker, name="qt-routing", daemon=True)
        self.worker_thread.start()
        return True

    def _run_requests(
        self, token, request, scenarios, sources, providers, cancel, selection
    ):
        error = None
        try:
            for index, item in enumerate(scenarios or (request,)):
                if index:
                    self._ui_call(token, self._begin_scenario, index)
                last_update = 0.0

                def progress(percent, points):
                    nonlocal last_update
                    now = time.monotonic()
                    if not self.closed and (
                        now - last_update >= 0.15 or percent >= 100
                    ):
                        last_update = now
                        self._progress.emit(
                            token,
                            (index * 100 + percent) / self.scenario_count,
                            points,
                        )

                options = {}
                if item.use_currents:
                    options = dict(
                        report=lambda message: self._message.emit(token, message),
                        confirm=lambda report: self._ui_call(
                            token, self.confirm, report
                        ),
                        save_fields=lambda fields: self._ui_call(
                            token, self._save_fields, fields
                        ),
                        save_gribs=lambda gribs: self._ui_call(
                            token, self._save_gribs, gribs
                        ),
                        current_selection=selection,
                    )
                result = calculate_route(
                    self.core, item, sources, providers, cancel, progress, **options
                )
                self._ui_call(token, self._save_result, item, result)
        except Exception as exc:
            error = exc
        if not self.closed:
            self._finished.emit(token, None, error)

    def fail(self, message):
        self.error = self.message = message
        self.changed.emit()
        return False

    def _ui_call(self, token, callback, *args):
        done, values, errors = Event(), [], []

        def invoke():
            try:
                self._check(token)
                values.append(callback(*args))
            except Exception as error:
                errors.append(error)
            finally:
                done.set()

        self._check(token)
        self.session_controller.scheduler.submit(invoke)
        while not done.wait(0.05):
            self._check(token)
        if errors:
            raise errors[0]
        self._check(token)
        return values[0]

    def _check(self, token):
        if self.closed or self.cancel_event.is_set() or token != self.generation:
            raise RoutingCancelledError()

    def _begin_scenario(self, index):
        self.scenario_index = index
        self.message = (
            "Calculating…"
            if self.scenario_count == 1
            else f"Scenario {index+1}/{self.scenario_count}: calculating…"
        )
        self.preview_changed.emit(())
        self.changed.emit()

    def _save_fields(self, fields):
        for field in fields:
            self.core.marine_manager.fields.add(field)

    def _save_gribs(self, gribs):
        manager = self.core.grib_manager
        for grib in gribs:
            if not manager.is_enabled(grib.name):
                manager.load(grib.path)
        manager.store_opened_gribs()
        manager.refresh_local_gribs()
        self.session_controller.data.redraw()

    def _save_result(self, request, result):
        collection = self.core.routingManager
        route = Routing(
            collection.get_unique_name(request.name),
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
            self.completed.emit(route)
            raise ValueError(self.error) from exc
        self.saved_count += 1
        self.completed.emit(route)

    def cancel(self):
        if self.busy:
            self.cancel_event.set()
            self.dismiss_preflight.emit()
            self.message = "Cancelling…"
            self.changed.emit()

    @Slot(int, str)
    def _apply_message(self, token, message):
        if (
            not self.closed
            and self.busy
            and token == self.generation
            and not self.cancel_event.is_set()
        ):
            self.message = message
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
        prefix = (
            f"Scenario {self.scenario_index+1}/{self.scenario_count} · "
            if self.scenario_count > 1
            else ""
        )
        self.message = prefix + f"Calculating: approximately {percent:.0f}%"
        if points is not None:
            self.preview_changed.emit(points)
        self.changed.emit()

    @Slot(int, object, object)
    def _finish(self, token, result, error):
        if self.closed or token != self.generation:
            return
        self.busy = False
        self.preview_changed.emit(())
        if self.cancel_event.is_set() or isinstance(
            error, (RoutingCancelledError, DownloadCancelledError)
        ):
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
            self.percent = 100
            self.message = (
                f"{self.saved_count}/{self.scenario_count} scenarios complete"
                if self.scenario_count > 1
                else "Route calculated and saved · "
                + ("currents included" if self.request.use_currents else "wind only")
            )
        if self.scenario_count > 1 and error is not None:
            self.message += (
                f" · {self.saved_count}/{self.scenario_count} scenarios saved"
            )
        self.changed.emit()

    def close(self):
        self.closed = True
        self.cancel_event.set()
        self.generation += 1
        self.dismiss_preflight.emit()
