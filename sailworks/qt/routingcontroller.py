"""Qt lifecycle around the shared engines, scenarios and current-coverage workflow."""

import multiprocessing
import os
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from queue import Empty
from threading import Event, Thread

from PySide6.QtCore import QObject, Signal, Slot
from weatherrouting import RoutingNoWindError

from sailworks.core import scenariopool
from sailworks.core.geo.routing import Routing
from sailworks.core.routingjob import (
    RoutingCancelledError,
)
from sailworks.core.tranchestore import ResolvedForecast
from sailworks.core.marine.chs import DownloadCancelledError
from sailworks.core.marine.routingselection import RoutingSelection
from sailworks.rendering.routingoverlay import RoutingOverlayModel


class RoutingController(QObject):
    changed = Signal()
    preview_changed = Signal(object)
    isochrones_changed = Signal(object)
    completed = Signal(object)
    dismiss_preflight = Signal()
    _progress = Signal(int, float, object, object)
    _message = Signal(int, str)
    _finished = Signal(int, object, object)

    def __init__(self, session_controller):
        super().__init__(session_controller)
        self.session_controller = session_controller
        self.core = session_controller.core
        self.busy = False
        self.closed = False
        self.native_overlay = None
        self.native_selection = None
        # One independent selection per calculation, kept while the window
        # switches between alternatives.
        self._native_selections = {}
        self.cancel_event = Event()
        self.generation = 0
        self.percent = 0.0
        self.message = "Wind-only routing"
        self.error = ""
        self.request = None
        self.worker_thread = None
        self.scenario_index, self.scenario_count, self.saved_count = 0, 1, 0
        self._parallel_mode = False
        self._parallel_progress = {}
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
        analysis = getattr(self.session_controller, "analysis", None)
        if analysis is None:
            return self.fail("No published model forecast analysis is available.")
        try:
            sources = analysis.snapshot(required_fields=("wind",))
        except Exception as exc:
            return self.fail(str(exc))
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
            self.core.marine_manager.fields.snapshot(request.current_priority)
            if request.use_currents
            else None
        )
        self.changed.emit()
        self.preview_changed.emit(())
        self.isochrones_changed.emit(())

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
        jobs, descriptors = self._scenario_jobs(
            request, scenarios, sources, providers, selection
        )
        try:
            if self._parallel_eligible(jobs, descriptors):
                self._run_parallel(token, jobs, sources, providers, cancel, selection)
            else:
                self._run_serial(token, jobs, sources, providers, cancel, selection)
        except Exception as exc:
            error = exc
        if not self.closed:
            self._finished.emit(token, None, error)

    def _scenario_jobs(self, request, scenarios, sources, providers, selection):
        """Freeze every serializable scenario input before any work starts."""
        items = tuple(scenarios or (request,))
        descriptors = scenariopool.provider_descriptors(providers)
        marine_directory = (
            str(self.core.marine_manager.directory)
            if getattr(self.core, "marine_manager", None) is not None
            else None
        )
        current_field_ids = (
            tuple(field.id for field in selection.fields)
            if selection is not None
            else ()
        )
        priority = getattr(selection, "priority", None) or request.current_priority
        jobs = tuple(
            scenariopool.ScenarioJob(
                job_id=index,
                request=item,
                binding=sources,
                providers=descriptors or (),
                current_field_ids=current_field_ids,
                current_priority=priority,
                marine_directory=marine_directory,
            )
            for index, item in enumerate(items)
        )
        return jobs, descriptors

    def _parallel_eligible(self, jobs, descriptors):
        if len(jobs) < 2 or descriptors is None:
            return False
        if os.environ.get("SAILWORKS_SCENARIO_POOL", "1").strip().lower() in {
            "0",
            "false",
            "no",
            "off",
        }:
            return False
        if not isinstance(jobs[0].binding, ResolvedForecast):
            return False
        # A current scenario needs its local field store in the worker.  A
        # missing directory is a configuration problem, so keep it serial.
        for job in jobs:
            if job.request.use_currents and not job.marine_directory:
                return False
        return True

    def _run_serial(self, token, jobs, sources, providers, cancel, selection):
        from sailworks.core.forecastboundrouting import calculate_bound_route

        if not isinstance(sources, ResolvedForecast):
            raise TypeError("routing requires a published model forecast")
        self._parallel_mode = False
        for index, job in enumerate(jobs):
            if index:
                self._ui_call(token, self._begin_scenario, job.job_id)
            state = {"last": 0.0}

            def progress(percent, points, index=index, state=state):
                now = time.monotonic()
                if not self.closed and (now - state["last"] >= 0.15 or percent >= 100):
                    state["last"] = now
                    self._progress.emit(
                        token,
                        (index * 100 + percent) / self.scenario_count,
                        points,
                        (
                            getattr(points, "isochrones", ())
                            if points is not None
                            else None
                        ),
                    )

            options = {}
            if job.request.use_currents:
                options = dict(
                    report=lambda message: self._message.emit(token, message),
                    confirm=lambda report: self._ui_call(token, self.confirm, report),
                    save_fields=lambda fields: self._ui_call(
                        token, self._save_fields, fields
                    ),
                    current_selection=selection,
                )
            result = calculate_bound_route(
                self.core,
                job.request,
                sources,
                providers,
                cancel,
                progress,
                **options,
            )
            self._ui_call(token, self._save_result, job.request, result)

    def _run_parallel(self, token, jobs, sources, providers, cancel, selection):
        """Run independent scenarios in spawned processes, then save in order.

        Any job that reaches a user confirmation, an unsupported provider or a
        pool-level failure re-runs the whole group on the serial path.  No
        result is saved before the group decision, so a fallback cannot leave
        duplicate routes behind.
        """
        manager = None
        progress_queue = None
        worker_cancel = None
        outcomes = {}
        first_error = None
        failed_index = None
        fallback = False
        infrastructure_error = None
        self._parallel_mode = True
        self._parallel_progress = {}
        self._message.emit(token, f"Calculating {len(jobs)} scenarios…")
        try:
            ctx = multiprocessing.get_context("spawn")
            manager = ctx.Manager()
            progress_queue = manager.Queue()
            worker_cancel = manager.Event()
            with ProcessPoolExecutor(
                max_workers=min(scenariopool.default_workers(), len(jobs)),
                mp_context=ctx,
            ) as pool:
                futures = {
                    pool.submit(
                        scenariopool.run_scenario,
                        job,
                        progress_queue,
                        worker_cancel,
                    ): job
                    for job in jobs
                }
                pending = set(futures)
                while pending:
                    self._drain_parallel_progress(progress_queue, token)
                    if self.closed or cancel.is_set() or token != self.generation:
                        worker_cancel.set()
                        for future in pending:
                            future.cancel()
                        break
                    done, pending = wait(
                        pending, timeout=0.05, return_when=FIRST_COMPLETED
                    )
                    for future in done:
                        job = futures[future]
                        if future.cancelled():
                            continue
                        try:
                            outcome = future.result()
                        except Exception as exc:
                            infrastructure_error = exc
                            worker_cancel.set()
                            break
                        self._parallel_progress[job.job_id] = 100.0
                        if outcome.needs_confirmation or outcome.fallback:
                            fallback = True
                            worker_cancel.set()
                            break
                        if outcome.failure is not None:
                            if job.request.use_currents and job.request.auto_download:
                                # A worker has no UI to confirm a current
                                # download and no credential store; let the
                                # serial path own the prompt and the download.
                                fallback = True
                                worker_cancel.set()
                                break
                            if failed_index is None or job.job_id < failed_index:
                                first_error = scenariopool.error_from_failure(
                                    outcome.failure
                                )
                                failed_index = job.job_id
                                for other in pending:
                                    if futures[other].job_id > failed_index:
                                        other.cancel()
                            continue
                        if first_error is not None and job.job_id > failed_index:
                            continue
                        outcomes[job.job_id] = outcome
                    if fallback or infrastructure_error:
                        break
                    if first_error is not None and not any(
                        futures[future].job_id < failed_index for future in pending
                    ):
                        break
        except Exception as exc:
            infrastructure_error = exc
        finally:
            if worker_cancel is not None:
                worker_cancel.set()
            if manager is not None:
                manager.shutdown()

        if progress_queue is not None:
            self._drain_parallel_progress(progress_queue, token)
        if self.closed or cancel.is_set() or token != self.generation:
            raise RoutingCancelledError()
        if fallback or infrastructure_error is not None:
            # The serial path prompts on the UI thread and owns the process
            # handles.  Recompute every job so the result order matches serial.
            self._parallel_progress = {}
            self.scenario_index = 0
            self._run_serial(token, jobs, sources, providers, cancel, selection)
            return
        if first_error is not None:
            for job in jobs:
                if job.job_id >= failed_index:
                    break
                outcome = outcomes.get(job.job_id)
                if outcome is not None and outcome.result is not None:
                    self._ui_call(token, self._save_result, job.request, outcome.result)
            raise first_error
        for job in jobs:
            outcome = outcomes.get(job.job_id)
            if outcome is None or outcome.result is None:
                raise ValueError("A scenario worker returned no route")
            if outcome.downloaded_fields:
                self._ui_call(token, self._save_fields, outcome.downloaded_fields)
            self._ui_call(token, self._save_result, job.request, outcome.result)

    def _drain_parallel_progress(self, progress_queue, token):
        while True:
            try:
                message = progress_queue.get_nowait()
            except (Empty, EOFError, OSError):
                return
            if not message:
                continue
            kind, job_id, *values = message
            if kind == "progress":
                percent, points, isochrones = values
                self._parallel_progress[job_id] = min(100.0, float(percent))
                active = sum(self._parallel_progress.values()) / 100.0
                overall = (
                    (self.saved_count + active) / max(1, self.scenario_count) * 100
                )
                self._progress.emit(token, min(100.0, overall), points, isochrones)
            elif kind == "message":
                self._message.emit(token, values[0])

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
            else f"Scenario {index + 1}/{self.scenario_count}: calculating…"
        )
        self.preview_changed.emit(())
        self.isochrones_changed.emit(())
        self.changed.emit()

    def _save_fields(self, fields):
        for field in fields:
            self.core.marine_manager.fields.add(field)

    def _save_result(self, request, result):
        collection = self.core.routingManager
        from sailworks.core.marine.routingrecord import RoutingResultError
        from sailworks.core.marine.routingresult import RoutingResult

        route = None
        try:
            if isinstance(result, RoutingResult):
                # Schema-v1 results are stored once by digest; the route keeps
                # only its polyline, a small summary and the artifact reference.
                route = collection.save_result(request.name, result)
            else:
                route = Routing(
                    collection.get_unique_name(request.name),
                    list(result.points),
                    list(result.isochrones),
                    collection=collection,
                    provenance=result.provenance,
                )
            collection.append(route)
            collection.set_active(route)
        except (OSError, RoutingResultError) as exc:
            self.error = f"Route calculated, but could not be saved: {exc}"
            if route is not None:
                self.completed.emit(route)
            raise ValueError(self.error) from exc
        self.saved_count += 1
        self.completed.emit(route)

    def selection_for(self, result):
        """Independent selection for one calculation, reused when revisited."""
        from sailworks.core.marine.routinginspection import InspectionContext
        from sailworks.core.marine.routingresult import RoutingResult

        if not isinstance(result, RoutingResult):
            raise TypeError("result must be a RoutingResult")
        selection = self._native_selections.get(result.calculation_id)
        if selection is not None and selection._result is result:
            return selection
        fresh = RoutingSelection(result)
        if selection is not None:
            # Same calculation, newly loaded artifact: keep the identifiers the
            # fresh result still records instead of pinning the stale geometry.
            InspectionContext.from_selection(selection).apply(fresh)
        self._native_selections[result.calculation_id] = fresh
        return fresh

    def show_native_result(self, result, require_validated=False):
        """Present a schema-v1 result for display and offline inspection.

        Saved alternatives may be incomplete or failed; those results are
        inspectable with explicit diagnostics but cannot be exported.
        """
        from sailworks.core.marine.routingresult import export_blockers

        model = RoutingOverlayModel(result)
        if require_validated:
            blockers = export_blockers(model.result)
            if blockers:
                raise ValueError(
                    "native routing result is not an accepted validated route: "
                    + ", ".join(blockers)
                )
        self.native_overlay = model
        self.native_selection = self.selection_for(model.result)
        self.changed.emit()
        return model

    def show_native_result_file(self, path):
        """Retain a validated native result for display and inspection.

        The overlay only accepts schema-v1 records, so provisional preview
        data cannot be presented as a calculated route.
        """
        model = RoutingOverlayModel.from_result_file(path)
        return self.show_native_result(model.result, require_validated=True)

    def clear_native_result(self):
        self.native_overlay = None
        self.native_selection = None
        self._native_selections.clear()
        self.changed.emit()

    def cancel(self):
        if self.busy:
            self.cancel_event.set()
            self.dismiss_preflight.emit()
            self.isochrones_changed.emit(())
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

    @Slot(int, float, object, object)
    def _apply_progress(self, token, percent, points, isochrones=None):
        if (
            self.closed
            or not self.busy
            or token != self.generation
            or self.cancel_event.is_set()
        ):
            return
        self.percent = percent
        prefix = (
            f"Scenario {self.scenario_index + 1}/{self.scenario_count} · "
            if self.scenario_count > 1 and not self._parallel_mode
            else ""
        )
        self.message = prefix + f"Calculating: approximately {percent:.0f}%"
        if points is not None:
            self.preview_changed.emit(points)
        if isochrones is not None:
            self.isochrones_changed.emit(isochrones)
        self.changed.emit()

    @Slot(int, object, object)
    def _finish(self, token, result, error):
        if self.closed or token != self.generation:
            return
        self.busy = False
        self._parallel_mode = False
        self._parallel_progress = {}
        self.preview_changed.emit(())
        self.isochrones_changed.emit(())
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
        self.isochrones_changed.emit(())
        self.closed = True
        self.cancel_event.set()
        self.generation += 1
        self.dismiss_preflight.emit()
