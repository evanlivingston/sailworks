"""Wiring checks for the Historical years panel inside the Compare page."""

import os
from datetime import datetime, timezone
from pathlib import Path
import threading
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

import sailworks.qt.historicalera5 as historicalera5
import sailworks.qt.historicalrouting as historicalrouting
from sailworks.core.historicalroutingrecords import MemberRecord
from sailworks.qt.historicalrouting import HistoricalRoutingPanel
from sailworks.qt.routingpanel import RoutingPanel

REGION = (-30.0, -10.0, 30.0, 40.0)


def application():
    return QApplication.instance() or QApplication([])


class FakeStore:
    def __init__(self, root=None):
        self.root = Path(root or ".")

    def list_jobs(self):
        return []

    def load(self, job_id):
        raise KeyError(job_id)


class FakeRouting(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.message = ""
        self.busy = False
        self.error = ""
        self.percent = 0.0
        self.confirm = None
        self.started = []
        self.cancelled = 0

    def start(self, request, scenarios, backend=None):
        self.started.append((request, scenarios, backend))
        return True

    def cancel(self):
        self.cancelled += 1


class FakePolarManager:
    polars = []

    def get_path(self, name):
        return name


class FakeCore:
    polar_manager = FakePolarManager()
    trackManager = []
    poiManager = []
    boat_info = None
    routingManager = []
    shapeManager = ()
    marine_manager = None


class FakeTimeControl:
    time = datetime(2021, 6, 15, 12)


class FakeController(QObject):
    boat_changed = Signal()

    def __init__(self):
        super().__init__()
        self.core = FakeCore()
        self.routing = FakeRouting()
        self.time_control = FakeTimeControl()
        self.analysis = None


def routing_panel(monkeypatch, tmp_path):
    application()
    monkeypatch.setattr(historicalrouting, "DATA_DIR", str(tmp_path))
    controller = FakeController()
    panel = RoutingPanel(controller)
    panel._fake_controller = controller
    return panel


def historical_panel(*, controller=None, request=None, service_factory=None):
    controller = controller or SimpleNamespace(core=None, analysis=None)
    return HistoricalRoutingPanel(
        controller,
        store_factory=FakeStore,
        request_provider=(lambda: request) if request is not None else None,
        service_factory=service_factory,
    )


def test_mode_combo_switches_the_stack_and_dispatches(monkeypatch, tmp_path):
    panel = routing_panel(monkeypatch, tmp_path)
    assert panel.compare_mode.currentData() == "parameter"
    assert panel.compare_stack.currentIndex() == 0

    panel.compare_mode.setCurrentIndex(panel.compare_mode.findData("historical"))
    assert panel.compare_stack.currentIndex() == 1

    calls = []
    monkeypatch.setattr(panel.historical, "start_job", lambda: calls.append(True))
    panel._calculate()
    assert calls == [True]
    assert panel.routing.started == []


def test_saved_and_restored_options_include_mode_and_historical_values(
    monkeypatch, tmp_path
):
    panel = routing_panel(monkeypatch, tmp_path)
    saved = panel.saved_options()
    assert saved["compare_mode"] == "parameter"
    assert saved["historical"] == panel.historical.values()

    saved["compare_mode"] = "historical"
    saved["historical"] = {
        "years": 4,
        "offset_days": 1,
        "max_duration_hours": 120,
        "region": list(REGION),
        "use_currents": True,
        "current_priority": "newest",
    }
    panel.restore_options(saved)
    assert panel.compare_mode.currentData() == "historical"
    assert panel.compare_stack.currentIndex() == 1
    assert panel.historical.values() == {
        "years": 4,
        "offset_days": 1,
        "max_duration_hours": 120,
        "region": REGION,
        "use_currents": True,
        "current_priority": "newest",
    }

    panel.restore_options({"compare_mode": "not-a-mode", "historical": "ignored"})
    assert panel.compare_mode.currentData() == "parameter"


def test_spec_uses_the_departure_year_and_options_region():
    request = SimpleNamespace(
        departure=datetime(2021, 6, 15, 12),
        start=(0.0, 0.0),
        destination=(1.0, 1.0),
        waypoints=(),
    )
    panel = historical_panel(request=request)
    panel.set_values(
        {
            "years": 5,
            "offset_days": 2,
            "max_duration_hours": 72,
            "region": REGION,
            "use_currents": False,
            "current_priority": "finest",
        }
    )
    spec = panel.spec()
    assert spec.reference_year == 2021
    assert spec.selected_departure_local == datetime(2021, 6, 15, 12)
    assert spec.region == REGION
    assert spec.years == 5
    assert spec.offset_days == 2
    assert spec.max_duration_hours == 72
    assert spec.backend == "native"
    assert spec.job_id == ""


class _Service:
    def __init__(self, error=None):
        self.error = error
        self.submitted = []
        self.cleared = []
        self.cancelled = []

    def submit(self, plan):
        self.submitted.append(plan)
        return SimpleNamespace(era5_key="key", is_alive=lambda: False)

    def key_for(self, plan):
        return "key"

    def error_for(self, key):
        return self.error

    def clear_error(self, key):
        self.cleared.append(key)

    def cancel(self, plan):
        self.cancelled.append(plan)


def _unit():
    return SimpleNamespace(
        start_utc=datetime(2020, 1, 1, tzinfo=timezone.utc),
        end_utc=datetime(2020, 1, 2, tzinfo=timezone.utc),
        region=REGION,
        unit_id="a" * 64,
    )


def test_ensure_wind_skips_covered_units(monkeypatch, tmp_path):
    monkeypatch.setattr(historicalera5, "read_era5_catalog", lambda root: [{"x": 1}])
    monkeypatch.setattr(historicalera5, "select_window_entries", lambda *a, **k: ["ok"])
    service = _Service()
    controller = SimpleNamespace(
        core=None,
        analysis=SimpleNamespace(
            state=SimpleNamespace(store=SimpleNamespace(root=tmp_path))
        ),
    )
    panel = historical_panel(
        controller=controller, service_factory=lambda store: service
    )
    panel.ensure_wind([_unit()])
    assert service.submitted == []


def test_ensure_wind_raises_on_a_reported_error(monkeypatch, tmp_path):
    monkeypatch.setattr(historicalera5, "read_era5_catalog", lambda root: [])
    monkeypatch.setattr(
        historicalera5,
        "select_window_entries",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("missing")),
    )
    monkeypatch.setattr(historicalera5, "plan_request", lambda *a, **k: object())
    service = _Service(error="boom")
    controller = SimpleNamespace(
        core=None,
        analysis=SimpleNamespace(
            state=SimpleNamespace(store=SimpleNamespace(root=tmp_path))
        ),
    )
    panel = historical_panel(
        controller=controller, service_factory=lambda store: service
    )
    try:
        panel.ensure_wind([_unit()])
    except RuntimeError as error:
        assert "boom" in str(error)
    else:
        raise AssertionError("ensure_wind did not report the ERA5 error")
    assert service.submitted
    assert service.cleared == ["key"]


def test_run_bound_blocks_until_finished_and_returns_outcomes():
    outcomes = [SimpleNamespace(member_id="2020-000")]
    panel = historical_panel()
    controller = SimpleNamespace(core=None, analysis=None, error="")

    def start_bound_runs(executions, *, on_finished=None, concurrency=None):
        threading.Timer(0.05, lambda: on_finished(outcomes, None)).start()
        return True

    controller.start_bound_runs = start_bound_runs
    panel.routing_controller = controller
    seen = []
    result = panel.run_bound([object()], on_outcome=seen.append)
    assert result == outcomes
    assert seen == outcomes


def _member():
    return MemberRecord(
        member_id="2020-000",
        year=2020,
        offset_days=0,
        state="complete",
        departure_utc="2020-06-15T12:00:00+00:00",
        coverage_start_utc="2020-06-15T12:00:00+00:00",
        coverage_end_utc="2020-06-25T12:00:00+00:00",
        outcome="finished",
        route_uid="route-a",
    )


def test_results_populate_from_a_fake_summary_and_records():
    panel = historical_panel()
    record = SimpleNamespace(
        job_id="job-1",
        created_at_utc="2026-01-01T00:00:00+00:00",
        state="complete",
        spec={
            "summary": {
                "complete_years": 1,
                "finished": 1,
                "no_finish": 0,
                "failed": 0,
                "completion_curve": [[0.0, 0.0], [10.0, 1.0]],
            }
        },
        members={"2020-000": _member()},
        excluded_years=(),
    )
    panel._show_record(record)
    assert "1 complete years" in panel.results.summary.text()
    assert panel.results.table.rowCount() == 1
    assert panel.results.table.item(0, 0).text() == "2020"
    assert panel.results.canvas._curve == [(0.0, 0.0), (10.0, 1.0)]
    assert panel.results.canvas._strip == [
        {"year": 2020, "offset_days": 0, "outcome": "finished"}
    ]


def test_delete_calls_the_coordinator_and_the_collection():
    route = SimpleNamespace(uid="route-a")
    collection = [route]
    controller = SimpleNamespace(
        core=SimpleNamespace(routingManager=collection), analysis=None
    )
    panel = historical_panel(controller=controller)
    panel._record = SimpleNamespace(job_id="job-1", members={"2020-000": _member()})
    calls = []
    panel.coordinator = SimpleNamespace(
        delete_job=lambda job_id, routes, collection=None: calls.append(
            (job_id, routes, collection)
        )
    )
    panel.delete_job()
    assert calls == [("job-1", [route], collection)]
    assert panel._record is None
    assert panel.results.table.rowCount() == 0


def test_existing_compare_calculate_path_is_unchanged(monkeypatch, tmp_path):
    panel = routing_panel(monkeypatch, tmp_path)
    assert panel.compare_mode.currentData() == "parameter"

    def refuse(backend=None):
        raise ValueError("boom")

    monkeypatch.setattr(panel, "_request", refuse)
    panel._calculate()
    assert panel.status.text() == "boom"
    assert panel.routing.started == []
