"""Spawn-safe scenario inputs and worker outcomes."""

from concurrent.futures import Future
from datetime import datetime, timezone
from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch
import pickle
import unittest

import weatherrouting
from PySide6.QtCore import QObject

from sailworks.core import scenariopool
from sailworks.core.routingjob import (
    CalculatedRoute,
    RouteRequest,
    RoutingCancelledError,
    RoutingPreview,
)
from sailworks.core.routingoptions import RouteScenario, scenario_request
from sailworks.core.scenariopool import (
    ProviderDescriptor,
    ScenarioJob,
    ScenarioOutcome,
    error_from_failure,
    provider_descriptors,
    run_scenario,
)
from sailworks.qt.routingcontroller import RoutingController


class FakeProvider:
    def __init__(self, descriptor):
        self.descriptor = descriptor

    def validity_descriptor(self):
        return self.descriptor


class TestScenarioPool(unittest.TestCase):
    def setUp(self):
        self.algorithm = weatherrouting.list_routing_algorithms()[0]["class"]
        self.request = RouteRequest(
            "route",
            (48.0, -123.5),
            (48.6, -122.8),
            datetime(2026, 11, 2, 18, tzinfo=timezone.utc),
            "first317.pol",
            self.algorithm,
        )
        self.binding = SimpleNamespace(name="binding")

    def _job(self, **kwargs):
        values = dict(
            job_id=0,
            request=scenario_request(self.request, RouteScenario()),
            binding=self.binding,
        )
        values.update(kwargs)
        return ScenarioJob(**values)

    def test_job_and_descriptors_are_picklable(self):
        job = self._job(providers=(ProviderDescriptor("gshhs", "/tmp/gshhs"),))
        copy = pickle.loads(pickle.dumps(job))
        self.assertEqual(copy.providers, job.providers)
        self.assertEqual(copy.request.name, job.request.name)

    def test_unknown_provider_keeps_the_group_serial(self):
        self.assertIsNone(provider_descriptors((SimpleNamespace(),)))
        descriptors = provider_descriptors((FakeProvider(("gshhs", "/tmp/g")),))
        self.assertEqual(descriptors, (ProviderDescriptor("gshhs", "/tmp/g"),))

    def test_worker_progress_and_result_preserve_the_job_id(self):
        messages = Queue()

        def calculate(core, request, binding, providers, cancel, progress, **options):
            progress(50, RoutingPreview(((1.0, 2.0),), ()))
            options["report"]("checking")
            return CalculatedRoute(((1.0, 2.0),), (), {"schema_version": 1})

        outcome = run_scenario(
            self._job(job_id=7),
            messages,
            Event(),
            calculate=calculate,
        )
        self.assertIsInstance(outcome, ScenarioOutcome)
        self.assertEqual(outcome.job_id, 7)
        self.assertIsNotNone(outcome.result)
        kinds = [messages.get_nowait()[0] for _ in range(messages.qsize())]
        self.assertIn("progress", kinds)
        self.assertIn("message", kinds)

    def test_worker_confirmation_is_reported_to_the_parent(self):
        def calculate(core, request, binding, providers, cancel, progress, **options):
            options["confirm"](object())
            return CalculatedRoute((), (), {})

        outcome = run_scenario(self._job(), calculate=calculate)
        self.assertTrue(outcome.needs_confirmation)
        self.assertIsNone(outcome.result)

    def test_worker_failure_rebuilds_a_known_exception(self):
        def calculate(core, request, binding, providers, cancel, progress, **options):
            raise RoutingCancelledError("stop")

        outcome = run_scenario(self._job(), calculate=calculate)
        self.assertEqual(outcome.failure.kind, "RoutingCancelledError")
        self.assertIsInstance(
            error_from_failure(outcome.failure), RoutingCancelledError
        )

    def test_worker_turns_shared_cancellation_into_a_local_event(self):
        started = Event()
        shared = Event()
        outcome = []

        def calculate(core, request, binding, providers, cancel, progress, **options):
            started.set()
            while not cancel.is_set():
                Event().wait(0.01)
            raise RoutingCancelledError("stop")

        worker = Thread(
            target=lambda: outcome.append(
                run_scenario(self._job(), cancel_event=shared, calculate=calculate)
            ),
            daemon=True,
        )
        worker.start()
        self.assertTrue(started.wait(2.0))
        shared.set()
        worker.join(2.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome[0].failure.kind, "RoutingCancelledError")

    def test_missing_frozen_current_fields_requests_serial_fallback(self):
        outcome = run_scenario(
            self._job(
                request=scenario_request(self.request, RouteScenario()),
                current_field_ids=("missing",),
            )
        )
        self.assertTrue(outcome.fallback)
        self.assertIsNone(outcome.result)


class FakeBinding:
    pass


class FakeManager:
    def Queue(self):
        return Queue()

    def Event(self):
        return Event()

    def shutdown(self):
        pass


class FakeContext:
    def Manager(self):
        return FakeManager()


class FakeExecutor:
    def __init__(self, max_workers, mp_context=None, **_kwargs):
        self.max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def submit(self, function, *args):
        future = Future()

        def run():
            try:
                future.set_result(function(*args))
            except BaseException as error:  # mirror a failed worker future
                future.set_exception(error)

        Thread(target=run, daemon=True).start()
        return future


class FakeSession(QObject):
    def __init__(self):
        super().__init__()
        self.core = SimpleNamespace(marine_manager=None)


class TestParallelController(unittest.TestCase):
    def setUp(self):
        self.algorithm = weatherrouting.list_routing_algorithms()[0]["class"]
        self.base = RouteRequest(
            "route",
            (48.0, -123.5),
            (48.6, -122.8),
            datetime(2026, 11, 2, 18, tzinfo=timezone.utc),
            "first317.pol",
            self.algorithm,
        )
        self.items = tuple(
            scenario_request(self.base, RouteScenario(time_offset_hours=hours))
            for hours in (0, 3, 6)
        )
        self.binding = FakeBinding()
        self.provider = FakeProvider(("gshhs", "/tmp/gshhs"))
        self.saved = []
        self.controller = RoutingController(FakeSession())
        self.controller.scenario_count = len(self.items)
        self.controller.scenario_index = 0
        self.controller.saved_count = 0
        self.controller.generation = 1
        self.controller.closed = False
        self.controller.cancel_event = Event()
        self.controller._ui_call = lambda _token, callback, *args: callback(*args)
        self.controller._save_result = lambda request, result: self.saved.append(
            request.name
        )

    def _jobs(self):
        with patch("sailworks.qt.routingcontroller.ResolvedForecast", FakeBinding):
            jobs, descriptors = self.controller._scenario_jobs(
                self.base,
                self.items,
                self.binding,
                (),
                None,
            )
            self.assertTrue(self.controller._parallel_eligible(jobs, descriptors))
        return jobs

    def test_parallel_saves_each_job_once_in_order(self):
        def calculate(core, request, binding, providers, cancel, progress, **options):
            progress(100, RoutingPreview((), ()))
            return CalculatedRoute(((1.0, 2.0),), (), {"scenario": request.name})

        with (
            patch.object(scenariopool, "calculate_bound_route", calculate),
            patch("sailworks.qt.routingcontroller.ProcessPoolExecutor", FakeExecutor),
            patch(
                "sailworks.qt.routingcontroller.multiprocessing.get_context",
                lambda _name: FakeContext(),
            ),
        ):
            self.controller._run_parallel(
                1,
                self._jobs(),
                self.binding,
                (),
                Event(),
                None,
            )
        self.assertEqual(self.saved, [item.name for item in self.items])

    def test_confirmation_falls_back_to_serial_without_partial_saves(self):
        def confirm(core, request, binding, providers, cancel, progress, **options):
            options["confirm"](object())
            return CalculatedRoute((), (), {})

        serial_saved = []

        def serial(core, request, binding, providers, cancel, progress, **options):
            progress(100, RoutingPreview((), ()))
            return CalculatedRoute(((1.0, 2.0),), (), {"scenario": request.name})

        with (
            patch.object(scenariopool, "calculate_bound_route", confirm),
            patch("sailworks.qt.routingcontroller.ProcessPoolExecutor", FakeExecutor),
            patch(
                "sailworks.qt.routingcontroller.multiprocessing.get_context",
                lambda _name: FakeContext(),
            ),
            patch("sailworks.core.forecastboundrouting.calculate_bound_route", serial),
            patch("sailworks.qt.routingcontroller.ResolvedForecast", FakeBinding),
        ):
            self.controller._save_result = lambda request, result: serial_saved.append(
                request.name
            )
            self.controller._run_parallel(
                1,
                self._jobs(),
                self.binding,
                (),
                Event(),
                None,
            )
        self.assertEqual(serial_saved, [item.name for item in self.items])

    def test_worker_failure_saves_only_preceding_results(self):
        def calculate(core, request, binding, providers, cancel, progress, **options):
            if request.name.endswith("t+6h"):
                raise ValueError("scenario failed")
            progress(100, RoutingPreview((), ()))
            return CalculatedRoute(((1.0, 2.0),), (), {"scenario": request.name})

        with (
            patch.object(scenariopool, "calculate_bound_route", calculate),
            patch("sailworks.qt.routingcontroller.ProcessPoolExecutor", FakeExecutor),
            patch(
                "sailworks.qt.routingcontroller.multiprocessing.get_context",
                lambda _name: FakeContext(),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "scenario failed"):
                self.controller._run_parallel(
                    1,
                    self._jobs(),
                    self.binding,
                    (),
                    Event(),
                    None,
                )
        self.assertEqual(self.saved, [self.items[0].name, self.items[1].name])
