"""App-level Legacy/Native backend choice, parameter seam and result safety."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from types import MethodType, SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from sailworks.core.marine.nativeworker import NativeJobStatus
from sailworks.core.routingjob import RouteRequest
from sailworks.qt.routingcontroller import RoutingController
from sailworks.qt.routingpanel import RoutingPanel
from sailworks.qt.window import PreviewWindow
from test.test_routing_persistence import _result as _routing_result

ROOT = Path(__file__).resolve().parents[1]
POLAR = ROOT / "sailworks/data/polars/test.pol"
BUILD_PYTHON = Path(
    os.environ.get(
        "SAILWORKS_NATIVE_ROUTING_BUILD",
        ROOT / "build/native-routing-312/python",
    )
)


class LegacyAlgorithm:
    pass


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class FakeRouting(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.busy = False
        self.message = "Idle"
        self.percent = 0.0
        self.cancel_event = Event()
        self.calls = []

    def start(self, request, scenarios=(), backend="legacy"):
        self.calls.append((request, scenarios, backend))
        return True

    def cancel(self):
        self.cancel_event.set()


class FakePolarManager:
    polars = ["test.pol"]

    def get_path(self, _name):
        return str(POLAR)


class FakeController(QObject):
    boat_changed = Signal()

    def __init__(self):
        super().__init__()
        self.routing = FakeRouting()
        self.core = SimpleNamespace(
            polar_manager=FakePolarManager(),
            trackManager=(),
            poiManager=(),
            boat_info=SimpleNamespace(is_valid=lambda: False),
        )
        self.time_control = SimpleNamespace(time=datetime(2026, 1, 1, 12, 0))
        self.analysis = SimpleNamespace(snapshot=lambda required_fields=(): None)
        self.data = SimpleNamespace(task=SimpleNamespace(busy=False))
        self.polars = SimpleNamespace(task=SimpleNamespace(busy=False))
        self.map_data = SimpleNamespace(task=SimpleNamespace(busy=False))
        self.charts = SimpleNamespace(get_line_point_validity_providers=lambda: ())


class FakeSession(QObject):
    def __init__(self):
        super().__init__()
        self.core = SimpleNamespace(marine_manager=None)
        self.analysis = SimpleNamespace(snapshot=lambda required_fields=(): object())
        self.data = SimpleNamespace(task=SimpleNamespace(busy=False))
        self.polars = SimpleNamespace(task=SimpleNamespace(busy=False))
        self.map_data = SimpleNamespace(task=SimpleNamespace(busy=False))
        self.charts = SimpleNamespace(get_line_point_validity_providers=lambda: ())


def _request(**overrides):
    values = dict(
        name="routing-test",
        start=(48.0, -123.5),
        destination=(48.4, -123.1),
        departure=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
        polar_file=str(POLAR),
        algorithm=LegacyAlgorithm,
        coastline_checks=False,
        parameters=(),
    )
    values.update(overrides)
    return RouteRequest(**values)


def test_panel_defaults_to_legacy_and_discloses_native_limits(app):
    panel = RoutingPanel(FakeController())
    assert panel.backend.currentData() == "legacy"
    assert "Legacy (default)" in panel.backend_note.text()

    panel.backend.setCurrentIndex(panel.backend.findData("native"))
    note = panel.backend_note.text()
    assert "experimental" in note
    assert "not applied" in note
    assert "does not download" in note
    saved = panel.saved_options()
    assert saved["backend"] == "native"
    panel.backend.setCurrentIndex(panel.backend.findData("legacy"))
    panel.restore_options(saved)
    assert panel.backend.currentData() == "native"
    panel.deleteLater()


def test_panel_native_refuses_changed_legacy_parameters_before_start(app):
    controller = FakeController()
    panel = RoutingPanel(controller)
    panel.set_location("start", (48.0, -123.5))
    panel.set_location("destination", (48.4, -123.1))
    panel.refresh_destinations()
    panel.backend.setCurrentIndex(panel.backend.findData("native"))

    algorithm = panel.algorithm.currentData()
    parameter = algorithm.PARAMS["min_increase"]
    panel.parameter_widgets["min_increase"].setValue(parameter.default + 1)
    panel._calculate()

    assert controller.routing.calls == []
    assert "min_increase" in panel.status.text()
    assert "legacy backend" in panel.status.text()

    panel.parameter_widgets["min_increase"].setValue(parameter.default)
    panel._calculate()
    assert len(controller.routing.calls) == 1
    request, scenarios, backend = controller.routing.calls[0]
    assert backend == "native"
    assert scenarios == ()
    assert request.start == (48.0, -123.5)
    assert request.destination == (48.4, -123.1)
    panel.deleteLater()


def test_panel_keeps_legacy_backend_and_unchanged_parameters(app):
    controller = FakeController()
    panel = RoutingPanel(controller)
    panel.set_location("start", (48.0, -123.5))
    panel.set_location("destination", (48.4, -123.1))
    panel.refresh_destinations()
    panel._calculate()

    assert controller.routing.calls
    request, _scenarios, backend = controller.routing.calls[0]
    assert backend == "legacy"
    assert dict(request.parameters)  # unchanged PARAMS are still sent
    panel.deleteLater()


def test_controller_start_native_uses_analysis_snapshot_and_finishes(app):
    session = FakeSession()
    binding = object()
    session.analysis = SimpleNamespace(snapshot=lambda required_fields=(): binding)
    controller = RoutingController(session)
    seen = {}

    def run_native(token, request, scenarios, sources, providers, cancel, selection):
        seen.update(
            token=token,
            request=request,
            sources=sources,
            providers=providers,
            selection=selection,
        )
        controller._finished.emit(token, None, None)

    controller._run_native_requests = run_native
    assert controller.start(_request(), backend="native")

    deadline = time.monotonic() + 5
    while controller.busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert not controller.busy
    assert seen["sources"] is binding
    assert seen["selection"] is None
    assert controller.backend == "native"
    assert controller.message == "Native route calculated and saved · validated"


def test_controller_start_native_refuses_changed_scenario_parameters(app):
    import weatherrouting

    algorithm = next(
        entry["class"]
        for entry in weatherrouting.list_routing_algorithms()
        if entry["name"] == "LinearBestIsoRouter"
    )
    defaults = tuple((key, param.value) for key, param in algorithm.PARAMS.items())
    changed = tuple(
        (key, 42.0 if key == "min_increase" else value) for key, value in defaults
    )
    controller = RoutingController(FakeSession())
    request = _request(algorithm=algorithm, parameters=defaults)
    scenario = _request(name="scenario", algorithm=algorithm, parameters=changed)

    assert not controller.start(request, (scenario,), backend="native")
    assert "min_increase" in controller.error
    assert not controller.busy


def test_controller_start_with_currents_without_manager_does_not_raise(app):
    controller = RoutingController(FakeSession())
    controller._run_requests = lambda *args: None

    assert controller.start(_request(use_currents=True), backend="legacy")
    controller.worker_thread.join(1)
    assert not controller.worker_thread.is_alive()


def test_failed_native_then_legacy_clears_overlay_and_inspector(app):
    controller = RoutingController(FakeSession())
    controller._run_requests = lambda *args: None
    controller.show_native_result(_routing_result())
    assert controller.native_overlay is not None

    window = SimpleNamespace(
        controller=SimpleNamespace(routing=controller),
        map=SimpleNamespace(
            native_routing=controller.native_overlay,
            set_isochrones=Mock(),
        ),
        navigation_panel=SimpleNamespace(route=None),
        routing_inspector=Mock(),
        _routing_selection_unsubscribe=None,
        _routing_selection_target=None,
        _routing_active_route=None,
    )
    window._native_cleared = MethodType(PreviewWindow._native_cleared, window)
    window._sync_route_isochrones = MethodType(
        PreviewWindow._sync_route_isochrones, window
    )
    controller.native_cleared.connect(window._native_cleared)

    assert controller.start(_request(), backend="legacy")
    controller.worker_thread.join(1)

    assert controller.native_overlay is None
    assert controller.native_selection is None
    assert window.map.native_routing is None
    window.routing_inspector.clear_result.assert_called_once_with(
        "No routing result loaded"
    )


class FakeWorker:
    def __init__(self, status):
        self.status = status
        self.shutdown_called = False
        self.submitted = []

    def submit(self, job):
        self.submitted.append(job)

    def poll(self, timeout=0):
        status, self.status = self.status, None
        return status

    def shutdown(self):
        self.shutdown_called = True


def _controller(tmp_path):
    collection = SimpleNamespace(elements=[])
    controller = RoutingController(FakeSession())
    controller.generation = 1
    controller.closed = False
    controller.cancel_event = Event()
    controller.busy = True
    controller.request = _request()
    controller.scenario_count = 1
    controller.saved_count = 0
    controller.backend = "native"
    controller._ui_call = lambda _token, callback, *args: callback(*args)
    controller._save_result = lambda request, result: collection.elements.append(
        (request, result)
    )
    controller.core = SimpleNamespace(routingManager=collection)
    return controller, collection


def _install_fakes(monkeypatch, controller, status, blockers=()):
    job = SimpleNamespace(calculation_id=str(uuid4()), request=_request())
    worker = FakeWorker(status)
    diagnostics = []

    monkeypatch.setattr(
        "sailworks.core.marine.nativerequest.freeze_native_requests",
        lambda *args, **kwargs: (job,),
    )
    monkeypatch.setattr(
        "sailworks.core.marine.nativeworker.NativeRoutingWorker",
        lambda: worker,
    )
    result = SimpleNamespace(calculation_id=job.calculation_id)
    monkeypatch.setattr(
        "sailworks.core.marine.routingnative.to_routing_result",
        lambda *args, **kwargs: result,
    )
    monkeypatch.setattr(
        "sailworks.core.marine.routingresult.export_blockers",
        lambda _result: tuple(blockers),
    )
    controller.native_diagnostic.connect(diagnostics.append)
    return worker, diagnostics


def test_controller_native_failure_never_saves_or_falls_back(tmp_path, monkeypatch):
    controller, collection = _controller(tmp_path)
    worker, _diagnostics = _install_fakes(
        monkeypatch,
        controller,
        NativeJobStatus(1, "failed", message="worker crashed"),
    )
    errors = []
    controller._finished.connect(lambda _token, _result, error: errors.append(error))
    controller.native_overlay = object()

    controller._run_native_requests(
        1, _request(), (), SimpleNamespace(), (), Event(), None
    )

    assert collection.elements == []
    assert controller.native_overlay is None
    assert worker.shutdown_called
    assert errors and "worker crashed" in str(errors[0])


def test_controller_native_missing_build_is_reported_without_fallback(
    tmp_path, monkeypatch
):
    controller, collection = _controller(tmp_path)
    worker, _diagnostics = _install_fakes(
        monkeypatch,
        controller,
        NativeJobStatus(
            1,
            "failed",
            message=(
                "Native routing module is not built or importable: "
                "No module named 'sailworks_native_routing'"
            ),
        ),
    )
    errors = []
    controller._finished.connect(lambda _token, _result, error: errors.append(error))

    controller._run_native_requests(
        1, _request(), (), SimpleNamespace(), (), Event(), None
    )

    assert collection.elements == []
    assert worker.shutdown_called
    assert errors and "not built or importable" in str(errors[0])


def test_controller_native_diagnostic_is_inspectable_but_not_saved(
    tmp_path, monkeypatch
):
    controller, collection = _controller(tmp_path)
    worker, diagnostics = _install_fakes(
        monkeypatch,
        controller,
        NativeJobStatus(1, "completed", payload={"engine_identity": {}}),
        blockers=("validation:unknown",),
    )
    errors = []
    controller._finished.connect(lambda _token, _result, error: errors.append(error))

    controller._run_native_requests(
        1, _request(), (), SimpleNamespace(), (), Event(), None
    )

    assert collection.elements == []
    assert worker.shutdown_called
    assert len(diagnostics) == 1
    assert errors and "validation:unknown" in str(errors[0])


def test_controller_native_stale_generation_cannot_save(tmp_path, monkeypatch):
    controller, collection = _controller(tmp_path)
    worker, _diagnostics = _install_fakes(
        monkeypatch,
        controller,
        NativeJobStatus(1, "completed", payload={"engine_identity": {}}),
    )
    controller.generation = 2

    controller._run_native_requests(
        1, _request(), (), SimpleNamespace(), (), Event(), None
    )

    assert collection.elements == []
    assert worker.submitted == []
    assert worker.shutdown_called


def test_controller_native_validated_result_is_saved_with_identity(
    tmp_path, monkeypatch
):
    controller, collection = _controller(tmp_path)
    worker, diagnostics = _install_fakes(
        monkeypatch,
        controller,
        NativeJobStatus(1, "completed", payload={"engine_identity": {}}),
    )

    controller._run_native_requests(
        1, _request(), (), SimpleNamespace(), (), Event(), None
    )

    assert len(collection.elements) == 1
    assert diagnostics == []
    assert worker.shutdown_called


@pytest.mark.skipif(
    not BUILD_PYTHON.is_dir(),
    reason="native routing module build is required for a real selected request",
)
def test_controller_runs_a_real_selected_request_and_keeps_provenance(
    app, tmp_path, monkeypatch
):
    from sailworks.core.geo.routingcollection import RoutingCollection
    from sailworks.core.marine.routingresult import export_eligible
    from sailworks.core.marine.routingresultstore import RoutingResultStore
    from test.test_native_selected_worker import (
        START,
        _forecast_inputs,
        _selected_forecast,
    )
    import sailworks.core.storage as storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    sys.path.insert(0, str(BUILD_PYTHON))
    with _forecast_inputs(tmp_path) as source:
        binding = _selected_forecast(tmp_path, source)
    # Use the selected forecast's departure window and a request inside its
    # published coverage, not fixture coordinates or fixture defaults.
    request = _request(
        name="selected-app-request",
        start=(48.25, -123.65),
        destination=(48.29, -123.61),
        departure=START + timedelta(minutes=30),
    )
    collection = RoutingCollection(RoutingResultStore(tmp_path / "results"))
    collection.save = lambda: None
    controller = RoutingController(FakeSession())
    controller.core = SimpleNamespace(routingManager=collection, marine_manager=None)
    controller.generation = 1
    controller.closed = False
    controller.cancel_event = Event()
    controller.busy = True
    controller.request = request
    controller.scenario_count = 1
    controller.saved_count = 0
    controller.backend = "native"
    controller._ui_call = lambda _token, callback, *args: callback(*args)

    controller._run_native_requests(1, request, (), binding, (), Event(), None)

    assert controller.saved_count == 1, controller.error
    assert len(collection.elements) == 1
    route = collection.elements[0]
    assert route.result_ref is not None
    result = collection.load_result(route)
    assert export_eligible(result)
    assert result.outcome.validation.independent is True
    assert result.calculation_id == route.result_summary["calculation_id"]
    diagnostics = result.outcome.diagnostics
    assert diagnostics["request_kind"] == "selected-user-input"
    assert (
        diagnostics["input_fingerprints"]["forecast"]
        == binding.entry.artifacts[0].sha256
    )
    assert diagnostics["input_fingerprints"]["start"] == request.start
    assert diagnostics["legacy_algorithm"]["selected"] == "LegacyAlgorithm"
    assert "not executed" in diagnostics["legacy_algorithm"]["note"]
