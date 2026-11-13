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

from sailworks.core.geo.track import Track
from sailworks.core.marine.nativeworker import NativeJobStatus
from sailworks.core.routingjob import RouteRequest
from sailworks.qt.routingcontroller import (
    RoutingController,
    _native_progress_percent,
)
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
        self.backend = "legacy"
        self.message = "Idle"
        self.percent = 0.0
        self.progress_indeterminate = False
        self.native_started_at = None
        self.native_counters = {}
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
    assert "Legacy (default)" in panel.backend.toolTip()

    panel.backend.setCurrentIndex(panel.backend.findData("native"))
    note = panel.backend.toolTip()
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


def test_panel_refuses_legacy_calculation_when_a_mark_has_rounding(app):
    controller = FakeController()
    track = Track(
        "race",
        [(48.0, -123.5), (48.05, -123.45), (48.1, -123.4)],
    )
    track.set_point_rounding_spec(1, {"side": "starboard", "radius_nm": 0.3})
    controller.core.trackManager = (track,)
    panel = RoutingPanel(controller)
    panel.refresh_destinations()
    panel.destination.setCurrentIndex(panel.destination.findText("Track: race"))
    panel._plan_changed()

    assert panel.backend.currentData() == "legacy"
    panel._calculate()

    assert controller.routing.calls == []
    assert "requires the Native backend" in panel.status.text()
    panel.deleteLater()


def test_panel_reads_mark_rounding_from_track_points(app):
    controller = FakeController()
    track = Track(
        "race",
        [(48.0, -123.5), (48.05, -123.45), (48.1, -123.4)],
    )
    track.set_point_rounding_spec(0, {"side": "starboard", "radius_nm": 3.0})
    track.rename_point(1, "Duntze Rock")
    track.set_point_rounding_spec(
        1,
        {
            "side": "port",
            "radius_nm": 0.4,
        },
    )
    track.set_point_rounding_spec(2, None)
    controller.core.trackManager = (track,)
    panel = RoutingPanel(controller)
    panel.backend.setCurrentIndex(panel.backend.findData("native"))
    panel.refresh_destinations()
    panel.destination.setCurrentIndex(panel.destination.findText("Track: race"))
    panel._plan_changed()

    request, _scenarios = panel._request()

    # The first track point is the start, so its mark rounding is not applied.
    assert request.roundings[0] is None
    assert request.roundings[1] == dict(
        side="port",
        name="Duntze Rock",
    )
    assert request.roundings[2] is None
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


def test_panel_shows_indeterminate_progress_for_native_and_percent_for_legacy(app):
    controller = FakeController()
    panel = RoutingPanel(controller)

    controller.routing.busy = True
    controller.routing.progress_indeterminate = True
    controller.routing.message = "Native calculation running (1/1)…"
    panel.refresh()
    assert panel.progress.minimum() == 0
    assert panel.progress.maximum() == 0
    assert "%" not in panel.status.text()

    controller.routing.busy = True
    controller.routing.progress_indeterminate = False
    controller.routing.percent = 42.0
    controller.routing.message = "Calculating: approximately 42%"
    panel.refresh()
    assert panel.progress.minimum() == 0
    assert panel.progress.maximum() == 100
    assert panel.progress.value() == 42
    panel.deleteLater()


def test_panel_shows_determinate_native_progress_after_first_engine_update(app):
    controller = FakeController()
    panel = RoutingPanel(controller)

    controller.routing.busy = True
    controller.routing.progress_indeterminate = False
    controller.routing.percent = 67.0
    controller.routing.message = (
        "Native calculation · Forward isochrone (2/3) · effort 67%"
    )
    panel.refresh()

    assert panel.progress.minimum() == 0
    assert panel.progress.maximum() == 100
    assert panel.progress.value() == 67
    assert "Forward isochrone" in panel.status.text()
    panel.deleteLater()


def test_panel_native_heartbeat_updates_elapsed_without_progress(app):
    controller = FakeController()
    panel = RoutingPanel(controller)
    routing = controller.routing
    routing.busy = True
    routing.backend = "native"
    routing.progress_indeterminate = False
    routing.percent = 20.0
    routing.message = "Native calculation · Forward isochrone (1/5) · effort 20%"
    routing.native_started_at = time.monotonic() - 65
    routing.native_counters = {
        "generated_states": 2500,
        "retained_states": 1000,
        "land_checks": 0,
        "closest_approach_nm": 6.25,
    }
    panel.refresh()

    assert panel._native_elapsed_timer.isActive()
    text = panel.status.text()
    assert "Forward isochrone (1/5)" in text
    assert "effort 20%" in text
    assert "elapsed 01:05" in text
    assert "retained 1,000/2,500 states" in text
    assert "land checks 0" in text
    assert "closest 6.2 nm" in text

    # The elapsed line advances on the timer alone, with no progress message.
    routing.native_started_at = time.monotonic() - 66
    panel._native_elapsed_timer.timeout.emit()
    assert "elapsed 01:06" in panel.status.text()
    panel.deleteLater()


def test_panel_native_heartbeat_omits_absent_counters(app):
    controller = FakeController()
    panel = RoutingPanel(controller)
    routing = controller.routing
    routing.busy = True
    routing.backend = "native"
    routing.native_started_at = time.monotonic()
    routing.message = "Native calculation · Forward isochrone (1/5) · effort 20%"

    routing.native_counters = {}
    panel.refresh()
    assert "states" not in panel.status.text()
    assert "land checks" not in panel.status.text()
    assert "closest" not in panel.status.text()

    routing.native_counters = {"retained_states": 1234}
    panel.refresh()
    assert "retained 1,234 states" in panel.status.text()

    routing.native_counters = {"generated_states": 4567}
    panel.refresh()
    assert "generated 4,567 states" in panel.status.text()
    panel.deleteLater()


def test_panel_native_heartbeat_timer_stops_on_finish_failure_and_cancel(app):
    controller = FakeController()
    panel = RoutingPanel(controller)
    routing = controller.routing

    def start_heartbeat():
        routing.busy = True
        routing.backend = "native"
        routing.cancel_event = Event()
        routing.native_started_at = time.monotonic()
        panel.refresh()
        assert panel._native_elapsed_timer.isActive()

    start_heartbeat()
    routing.busy = False
    panel.refresh()
    assert not panel._native_elapsed_timer.isActive()

    start_heartbeat()
    routing.busy = False
    routing.message = "Native calculation failed: worker crashed"
    panel.refresh()
    assert not panel._native_elapsed_timer.isActive()

    start_heartbeat()
    routing.cancel_event.set()
    panel.refresh()
    assert not panel._native_elapsed_timer.isActive()
    panel.deleteLater()


def test_native_progress_percent_uses_attempt_window():
    assert _native_progress_percent(
        {
            "stage": "forward_isochrone",
            "attempt": 1,
            "total_attempts": 5,
            "effort_percent": 100,
        }
    ) == pytest.approx(20.0)
    assert _native_progress_percent(
        {
            "stage": "forward_isochrone",
            "attempt": 1,
            "total_attempts": 1,
            "effort_percent": 42,
        }
    ) == pytest.approx(42.0)
    assert _native_progress_percent(
        {
            "stage": "forward_isochrone",
            "attempt": 2,
            "total_attempts": 5,
            "effort_percent": 50,
        }
    ) == pytest.approx(30.0)


def test_native_progress_percent_clamps_and_never_reaches_100_before_complete():
    over = _native_progress_percent(
        {
            "stage": "validation",
            "attempt": 9,
            "total_attempts": 5,
            "effort_percent": 100,
        }
    )
    assert over == pytest.approx(99.0)
    assert over < 100.0
    assert _native_progress_percent(
        {
            "stage": "forward_isochrone",
            "attempt": 1,
            "total_attempts": 5,
            "effort_percent": -20,
        }
    ) == pytest.approx(0.0)
    assert _native_progress_percent(
        {
            "stage": "complete",
            "attempt": 5,
            "total_attempts": 5,
            "effort_percent": 100,
        }
    ) == pytest.approx(100.0)
    assert (
        _native_progress_percent(
            {
                "stage": "validation",
                "attempt": 5,
                "total_attempts": 5,
                "effort_percent": 100,
            }
        )
        < 100.0
    )


def test_native_progress_percent_guards_missing_attempt_fields():
    assert (
        _native_progress_percent(
            {
                "stage": "preflight",
                "attempt": 0,
                "total_attempts": 0,
                "effort_percent": 100,
            }
        )
        is None
    )
    assert (
        _native_progress_percent(
            {
                "stage": "validation",
                "attempt": None,
                "total_attempts": 5,
                "effort_percent": 100,
            }
        )
        is None
    )
    assert (
        _native_progress_percent({"stage": "validation", "effort_percent": None})
        is None
    )


def test_controller_native_progress_sets_stage_and_rejects_stale_or_cancelled(app):
    controller = RoutingController(FakeSession())
    controller.busy = True
    controller.generation = 1
    controller.backend = "native"
    controller.progress_indeterminate = True

    update = {
        "stage": "forward_isochrone",
        "attempt": 2,
        "total_attempts": 3,
        "effort_percent": 100,
        "generated_states": 2500,
        "retained_states": 1000,
        "land_checks": 50,
        "closest_approach_nm": 2.5,
    }
    controller._apply_native_progress(1, update)
    assert controller.percent == pytest.approx(200.0 / 3.0)
    assert controller.percent < 100.0
    assert controller.progress_indeterminate is False
    assert "Forward isochrone (2/3)" in controller.message
    assert "effort 67%" in controller.message
    assert controller.native_counters == {
        "generated_states": 2500,
        "retained_states": 1000,
        "land_checks": 50,
        "closest_approach_nm": 2.5,
    }

    controller._apply_native_progress(
        0,
        {
            "stage": "validation",
            "attempt": 3,
            "total_attempts": 3,
            "effort_percent": 100,
            "generated_states": 9999,
            "retained_states": 9999,
            "land_checks": 9999,
            "closest_approach_nm": 9.9,
        },
    )
    assert controller.percent == pytest.approx(200.0 / 3.0)
    assert "Forward isochrone" in controller.message
    assert controller.native_counters["generated_states"] == 2500

    controller.cancel_event.set()
    controller._apply_native_progress(
        1,
        {
            "stage": "complete",
            "attempt": 3,
            "total_attempts": 3,
            "effort_percent": 100,
            "generated_states": 9999,
        },
    )
    assert controller.percent == pytest.approx(200.0 / 3.0)
    assert controller.native_counters["generated_states"] == 2500


def test_controller_clears_native_indeterminate_progress_on_finish_failure_and_cancel(
    app,
):
    session = FakeSession()
    controller = RoutingController(session)
    seen = {}

    def run_native(token, request, scenarios, sources, providers, cancel, selection):
        seen["indeterminate"] = controller.progress_indeterminate
        controller._finished.emit(token, None, None)

    controller._run_native_requests = run_native
    assert controller.start(_request(), backend="native")
    assert controller.progress_indeterminate is True
    assert controller.native_started_at is not None
    deadline = time.monotonic() + 5
    while controller.busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert seen["indeterminate"] is True
    assert controller.progress_indeterminate is False
    assert controller.native_started_at is None

    failing = RoutingController(FakeSession())
    failing.busy = True
    failing.generation = 1
    failing.request = _request()
    failing.backend = "native"
    failing.progress_indeterminate = True
    failing.native_started_at = time.monotonic()
    failing._finish(1, None, RuntimeError("failed"))
    assert failing.progress_indeterminate is False
    assert failing.native_started_at is None

    cancelling = RoutingController(FakeSession())
    cancelling.busy = True
    cancelling.backend = "native"
    cancelling.progress_indeterminate = True
    cancelling.native_started_at = time.monotonic()
    cancelling.cancel()
    assert cancelling.progress_indeterminate is False
    assert cancelling.percent == 0.0
    assert cancelling.native_started_at is None


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
    window._native_owns_route = MethodType(PreviewWindow._native_owns_route, window)
    window._sync_isochrone_colour_mode = MethodType(
        PreviewWindow._sync_isochrone_colour_mode, window
    )
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
    def __init__(self, status, progress=()):
        self.status = status
        self.progress_updates = list(progress)
        self.shutdown_called = False
        self.submitted = []

    def submit(self, job):
        self.submitted.append(job)

    def poll(self, timeout=0):
        if self.progress_updates:
            return None
        status, self.status = self.status, None
        return status

    def take_progress(self):
        return self.progress_updates.pop(0) if self.progress_updates else None

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


def _install_fakes(monkeypatch, controller, status, blockers=(), progress=()):
    job = SimpleNamespace(calculation_id=str(uuid4()), request=_request())
    worker = FakeWorker(status, progress)
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


def test_controller_forwards_worker_progress_to_panel_state(tmp_path, monkeypatch):
    controller, collection = _controller(tmp_path)
    updates = [
        {
            "stage": "preflight",
            "attempt": 0,
            "total_attempts": 0,
            "effort_percent": 100,
        },
        {
            "stage": "forward_isochrone",
            "attempt": 1,
            "total_attempts": 5,
            "effort_percent": 100,
            "generated_states": 1200,
            "retained_states": 400,
        },
        {
            "stage": "validation",
            "attempt": 5,
            "total_attempts": 5,
            "effort_percent": 100,
            "generated_states": 2400,
            "retained_states": 900,
            "land_checks": 70,
            "closest_approach_nm": 1.25,
        },
    ]
    worker, _diagnostics = _install_fakes(
        monkeypatch,
        controller,
        NativeJobStatus(1, "completed", payload={"engine_identity": {}}),
        progress=updates,
    )
    states = []
    controller._native_progress.connect(
        lambda token, update: states.append(
            (
                token,
                update["stage"],
                controller.percent,
                controller.progress_indeterminate,
                controller.message,
            )
        )
    )

    controller._run_native_requests(
        1, _request(), (), SimpleNamespace(), (), Event(), None
    )

    assert [state[1] for state in states] == [
        "preflight",
        "forward_isochrone",
        "validation",
    ]
    assert states[0][0] == 1
    assert states[0][2] == 0.0
    assert states[0][3] is True
    assert "Preflight" in states[0][4]
    assert states[1][2] == pytest.approx(20.0)
    assert states[1][3] is False
    assert "Forward isochrone (1/5)" in states[1][4]
    assert "effort 20%" in states[1][4]
    assert states[2][2] == pytest.approx(99.0)
    assert states[2][2] < 100.0
    assert "Validation (5/5)" in states[2][4]
    assert controller.native_counters == {
        "generated_states": 2400,
        "retained_states": 900,
        "land_checks": 70,
        "closest_approach_nm": 1.25,
    }
    assert len(collection.elements) == 1
    assert worker.progress_updates == []
    assert worker.shutdown_called


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
    progress = []
    controller._native_progress.connect(lambda _token, update: progress.append(update))

    controller._run_native_requests(1, request, (), binding, (), Event(), None)

    assert progress
    stages = [update["stage"] for update in progress]
    assert stages[0] == "preflight"
    assert "validation" in stages
    assert stages[-1] == "complete"
    assert controller.percent == 100
    assert controller.progress_indeterminate is False
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


def test_panel_selects_a_wave_source_and_sends_it_to_native(app):
    controller = FakeController()
    run_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fields = ("wind", "wave_height", "wave_period", "wave_direction")
    entry = SimpleNamespace(
        entry_id="wave-run",
        model_id="GFSWAVE",
        run_time=run_time,
        fields=fields,
        field_times=tuple(
            (field, (run_time, run_time + timedelta(hours=3))) for field in fields
        ),
    )
    controller.analysis = SimpleNamespace(
        active=SimpleNamespace(tranche=SimpleNamespace(entries=(entry,))),
        snapshot=lambda required_fields=(): object(),
    )
    panel = RoutingPanel(controller)
    panel.set_location("start", (48.0, -123.5))
    panel.set_location("destination", (48.4, -123.1))
    panel.refresh_destinations()
    panel.backend.setCurrentIndex(panel.backend.findData("native"))
    assert panel.wave_data.count() == 2
    panel.wave_data.setCurrentIndex(panel.wave_data.findData("wave-run"))
    assert panel._wave_entry_id == "wave-run"
    panel._calculate()

    assert controller.routing.calls
    request, _scenarios, backend = controller.routing.calls[0]
    assert backend == "native"
    assert request.wave_entry_id == "wave-run"
    panel.deleteLater()


def test_panel_refuses_wave_data_on_the_legacy_backend(app):
    controller = FakeController()
    run_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fields = ("wind", "wave_height", "wave_period", "wave_direction")
    entry = SimpleNamespace(
        entry_id="wave-run",
        model_id="GFSWAVE",
        run_time=run_time,
        fields=fields,
        field_times=tuple(
            (field, (run_time, run_time + timedelta(hours=3))) for field in fields
        ),
    )
    controller.analysis = SimpleNamespace(
        active=SimpleNamespace(tranche=SimpleNamespace(entries=(entry,))),
        snapshot=lambda required_fields=(): object(),
    )
    panel = RoutingPanel(controller)
    panel.set_location("start", (48.0, -123.5))
    panel.set_location("destination", (48.4, -123.1))
    panel.refresh_destinations()
    panel.backend.setCurrentIndex(panel.backend.findData("native"))
    panel.wave_data.setCurrentIndex(panel.wave_data.findData("wave-run"))
    panel.backend.setCurrentIndex(panel.backend.findData("legacy"))
    panel._calculate()

    assert controller.routing.calls == []
    assert "Native" in panel.status.text()
    panel.deleteLater()
