"""Wiring checks for the Historical years panel inside the Compare page."""

import os
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QScrollArea, QTreeWidgetItem

import sailworks.qt.era5download as era5download
import sailworks.qt.historicalera5 as historicalera5
import sailworks.qt.historicalrouting as historicalrouting
from sailworks.core.arco import ArcoDownloadService
from sailworks.core.geo.track import Track
from sailworks.core.historicalroutingrecords import MemberRecord
from sailworks.qt.historicalrouting import HistoricalRoutingPanel
from sailworks.qt.routingpanel import RoutingPanel
from sailworks.qt.scenepanel import NAME_COLUMN, NODE_ROLE
from test.test_qt_scenepanel import application, panel_for

REGION = (-30.0, -10.0, 30.0, 40.0)


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


def routing_panel(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(historicalrouting, "DATA_DIR", str(tmp_path))
    controller = FakeController()
    panel = RoutingPanel(controller)
    qtbot.addWidget(panel)
    panel._fake_controller = controller
    return panel


def historical_panel(qtbot, *, controller=None, request=None, service_factory=None):
    controller = controller or SimpleNamespace(core=None, analysis=None)
    panel = HistoricalRoutingPanel(
        controller,
        store_factory=FakeStore,
        request_provider=(lambda: request) if request is not None else None,
        service_factory=service_factory,
    )
    qtbot.addWidget(panel)
    return panel


def analysis_with_store(store):
    return SimpleNamespace(state=SimpleNamespace(store=store))


def test_tranche_store_reads_the_session_controller_analysis(qtbot):
    """The real controller keeps the analysis on its session controller."""
    store = FakeStore()
    controller = SimpleNamespace(
        core=None,
        session_controller=SimpleNamespace(analysis=analysis_with_store(store)),
    )
    panel = historical_panel(qtbot, controller=controller)
    assert panel._tranche_store() is store


def test_tranche_store_falls_back_to_the_controller_analysis(qtbot):
    store = FakeStore()
    controller = SimpleNamespace(core=None, analysis=analysis_with_store(store))
    panel = historical_panel(qtbot, controller=controller)
    assert panel._tranche_store() is store


def _stub_start_job_inputs(monkeypatch, panel, spec=None):
    monkeypatch.setattr(panel, "spec", lambda: spec)
    monkeypatch.setattr(panel, "_base_request", lambda: object())
    monkeypatch.setattr(panel.options, "validation_message", lambda: "")


def test_start_job_reports_a_missing_tranche_store(qtbot, monkeypatch):
    panel = historical_panel(qtbot)
    messages = []
    monkeypatch.setattr(panel.options, "set_status", messages.append)
    _stub_start_job_inputs(monkeypatch, panel, spec=object())
    panel.actions.start_job()
    assert messages
    assert "forecast analysis store" in messages[-1]
    assert panel._thread is None


def test_start_job_starts_the_thread_when_the_store_exists(qtbot, monkeypatch):
    store = FakeStore()
    controller = SimpleNamespace(
        core=None,
        session_controller=SimpleNamespace(analysis=analysis_with_store(store)),
    )
    panel = historical_panel(qtbot, controller=controller)
    messages = []
    monkeypatch.setattr(panel.options, "set_status", messages.append)
    monkeypatch.setattr(panel.coordinator, "preflight", None)
    _stub_start_job_inputs(monkeypatch, panel, spec=object())
    ran = []
    monkeypatch.setattr(panel, "_run_job", lambda spec, base: ran.append((spec, base)))
    panel.actions.start_job()
    panel._thread.join(2)
    assert "Starting historical routing…" in messages
    assert ran


def test_start_job_reports_unexpected_spec_errors(qtbot, monkeypatch):
    panel = historical_panel(qtbot)
    messages = []
    monkeypatch.setattr(panel.options, "set_status", messages.append)

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(panel, "spec", boom)
    panel.actions.start_job()
    assert messages == ["boom"]
    assert panel._thread is None


def test_historical_panel_fits_a_narrow_compare_page(qtbot):
    controller = SimpleNamespace(core=None, analysis=None)
    panel = HistoricalRoutingPanel(controller, store_factory=FakeStore)
    panel.results.set_has_run(True)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(panel)
    qtbot.addWidget(scroll)
    scroll.resize(360, 600)
    scroll.show()
    qtbot.wait(10)

    assert panel.minimumSizeHint().width() <= 340
    assert scroll.horizontalScrollBar().maximum() == 0
    assert panel.results.isVisibleTo(scroll)
    assert panel.results.delete_button.isVisibleTo(scroll)
    assert panel.results.height() > 0


def test_compare_page_shows_axes_and_toggles_the_historical_section(
    qtbot, monkeypatch, tmp_path
):
    panel = routing_panel(qtbot, monkeypatch, tmp_path)
    assert not hasattr(panel, "compare_mode")
    assert not hasattr(panel, "compare_stack")
    assert len(panel.comparison_axes) == 4
    assert panel.historical_enabled.text() == "Run historical years"
    assert not panel.historical_enabled.isChecked()
    assert panel.historical.isHidden()

    panel.historical_enabled.setChecked(True)
    assert not panel.historical.isHidden()

    panel.historical_enabled.setChecked(False)
    assert panel.historical.isHidden()


def test_calculate_with_historical_checked_runs_the_ensemble(
    qtbot, monkeypatch, tmp_path
):
    panel = routing_panel(qtbot, monkeypatch, tmp_path)
    panel.historical_enabled.setChecked(True)
    historical_calls = []
    monkeypatch.setattr(
        panel.historical, "start_job", lambda: historical_calls.append(True)
    )
    monkeypatch.setattr(
        panel, "_request", lambda backend=None: (SimpleNamespace(roundings=()), ())
    )

    panel._calculate()

    compare = next(
        index
        for index in range(panel.pages.count())
        if panel.pages.tabText(index) == "Compare"
    )
    assert panel.pages.currentIndex() == compare
    assert historical_calls == [True]
    assert panel.routing.started == []
    assert "Set a route" not in panel.status.text()


def test_calculate_with_historical_unchecked_is_unchanged(qtbot, monkeypatch, tmp_path):
    panel = routing_panel(qtbot, monkeypatch, tmp_path)
    assert not panel.historical_enabled.isChecked()
    panel.pages.setCurrentIndex(panel.pages.indexOf(panel.compare_page))
    monkeypatch.setattr(
        panel, "_request", lambda backend=None: (SimpleNamespace(roundings=()), ())
    )

    panel._calculate()

    assert len(panel.routing.started) == 1


def test_historical_run_with_a_missing_route_reports_the_real_reason(
    qtbot, monkeypatch, tmp_path
):
    panel = routing_panel(qtbot, monkeypatch, tmp_path)
    panel.historical_enabled.setChecked(True)
    monkeypatch.setattr(
        panel.historical,
        "_base_request",
        lambda: (_ for _ in ()).throw(
            ValueError("Build a route request before starting historical routing.")
        ),
    )

    panel.historical.start_job()

    assert "Build a route request" in panel.historical.options.status.text()
    assert "Set a route before starting historical routing." not in (
        panel.historical.options.status.text()
    )


def test_show_historical_job_selects_compare_and_dispatches(
    qtbot, monkeypatch, tmp_path
):
    panel = routing_panel(qtbot, monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(panel.historical, "show_job", calls.append)

    panel.show_historical_job("job-9")

    compare = next(
        index
        for index in range(panel.pages.count())
        if panel.pages.tabText(index) == "Compare"
    )
    assert panel.pages.currentIndex() == compare
    assert panel.historical_enabled.isChecked()
    assert not panel.historical.isHidden()
    assert calls == ["job-9"]


def test_scene_group_selection_emits_the_group_and_items_still_select(qtbot, tmp_path):
    application()
    panel = panel_for(tmp_path)
    qtbot.addWidget(panel)
    groups = []
    selected = []
    panel.group_selected.connect(groups.append)
    panel.selected.connect(lambda kind, key: selected.append((kind, key)))

    group_row = QTreeWidgetItem(["historical-routing:job-1", ""])
    group_row.setData(NAME_COLUMN, NODE_ROLE, ("group", "historical-routing:job-1"))
    panel.tree.addTopLevelItem(group_row)
    panel.tree.clearSelection()
    group_row.setSelected(True)
    assert groups == ["historical-routing:job-1"]
    assert selected == []

    item_row = QTreeWidgetItem(["Route", ""])
    item_row.setData(NAME_COLUMN, NODE_ROLE, ("item", "routing", "r1"))
    panel.tree.addTopLevelItem(item_row)
    panel.tree.clearSelection()
    item_row.setSelected(True)
    assert selected == [("routing", "r1")]
    assert groups == ["historical-routing:job-1"]


def test_historical_section_locks_and_restores_the_parameter_axes(
    qtbot, monkeypatch, tmp_path
):
    panel = routing_panel(qtbot, monkeypatch, tmp_path)
    axes = list(panel.comparison_axes.values())
    first, second = axes[0], axes[1]
    first[0].setChecked(True)
    second[0].setChecked(False)

    panel.historical_enabled.setChecked(True)
    assert all(not enabled.isEnabled() for enabled, _ in axes)
    assert all(not values.isEnabled() for _, values in axes)

    panel.historical_enabled.setChecked(False)
    assert all(enabled.isEnabled() for enabled, _ in axes)
    assert first[1].isEnabled()
    assert not second[1].isEnabled()


def test_historical_options_have_no_run_button(qtbot):
    panel = historical_panel(qtbot, request=None)
    assert not hasattr(panel.options, "run_button")
    assert not panel.options.cancel_button.isEnabled()


def test_show_job_loads_a_published_run(qtbot):
    record = SimpleNamespace(
        job_id="job-1",
        created_at_utc="2026-06-15T12:00:00+00:00",
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
    store = FakeStore()
    store.load = lambda job_id: record
    panel = HistoricalRoutingPanel(
        SimpleNamespace(core=None, analysis=None),
        store_factory=lambda root: store,
        request_provider=None,
    )
    qtbot.addWidget(panel)
    assert panel.job_selector is None
    assert panel.results.isHidden()
    assert not panel.results.empty_label.isHidden()

    panel.show_job("job-1")
    assert panel._record is record
    assert not panel.results.isHidden()
    assert panel.results.empty_label.isHidden()
    assert len(panel.results._rows) == 1
    assert panel.results.delete_button.isEnabled()


def test_show_job_reports_a_missing_run_without_raising(qtbot):
    panel = historical_panel(qtbot)
    assert panel.job_selector is None
    panel.show_job("missing")
    assert "Could not load that run" in panel.options.status.text()
    assert panel.results.isHidden()
    assert not panel.results.empty_label.isHidden()


def test_saved_and_restored_options_include_mode_and_historical_values(
    qtbot, monkeypatch, tmp_path
):
    panel = routing_panel(qtbot, monkeypatch, tmp_path)
    saved = panel.saved_options()
    assert saved["historical_enabled"] is False
    assert "compare_mode" not in saved
    assert saved["historical"] == panel.historical.values()

    saved["historical_enabled"] = True
    saved["historical"] = {
        "years": 4,
        "offset_days": 1,
        "max_duration_hours": 120,
        "use_currents": True,
        "region": list(REGION),
        "current_priority": "newest",
    }
    panel.restore_options(saved)
    assert panel.historical_enabled.isChecked()
    assert not panel.historical.isHidden()
    assert panel.historical.values() == {
        "years": 4,
        "offset_days": 1,
        "max_duration_hours": 120,
        "use_currents": True,
    }

    panel.restore_options({"compare_mode": "historical", "historical": "ignored"})
    assert not panel.historical_enabled.isChecked()
    assert panel.historical.isHidden()


def test_restore_selects_a_saved_track_destination_by_name(
    qtbot, monkeypatch, tmp_path
):
    panel = routing_panel(qtbot, monkeypatch, tmp_path)
    track = Track("The Pacific NW Offshore Race 2026 - Course", [(48.4, -123.4)])
    panel._fake_controller.core.trackManager = [track]
    panel.refresh_destinations()

    panel.restore_options({"destination": {"type": "track", "name": track.name}})

    assert panel.destination.currentData() is track
    assert panel.destination.currentText() == "Track: " + track.name


def test_restore_accepts_a_legacy_track_value_shape(qtbot, monkeypatch, tmp_path):
    panel = routing_panel(qtbot, monkeypatch, tmp_path)
    track = Track("The Pacific NW Offshore Race 2026 - Course", [(48.4, -123.4)])
    panel._fake_controller.core.trackManager = [track]
    panel.refresh_destinations()

    panel.restore_options({"destination": {"type": "track", "value": track.name}})

    assert panel.destination.currentData() is track


def test_restored_track_destination_enables_calculate(qtbot, monkeypatch, tmp_path):
    panel = routing_panel(qtbot, monkeypatch, tmp_path)
    panel._fake_controller.core.polar_manager.polars = ["boat.pol"]
    track = Track("The Pacific NW Offshore Race 2026 - Course", [(48.4, -123.4)])
    panel._fake_controller.core.trackManager = [track]
    panel.refresh_polars()
    panel.refresh_destinations()

    panel.restore_options({"destination": {"type": "track", "name": track.name}})
    panel.refresh()

    assert panel.destination.currentData() is track
    assert panel.calculate.isEnabled()


def test_spec_uses_the_departure_year_and_derives_the_region(qtbot):
    request = SimpleNamespace(
        departure=datetime(2021, 6, 15, 12),
        start=(0.0, 0.0),
        destination=(1.0, 1.0),
        waypoints=(),
    )
    panel = historical_panel(qtbot, request=request)
    panel.set_values(
        {
            "years": 5,
            "offset_days": 2,
            "max_duration_hours": 72,
            "use_currents": False,
            "region": REGION,
            "current_priority": "finest",
        }
    )
    spec = panel.spec()
    assert spec.reference_year == 2021
    assert spec.selected_departure_local == datetime(2021, 6, 15, 12)
    # Route geometry padded by 5 degrees, ignoring any stored region value.
    assert spec.region == (-5.0, -5.0, 6.0, 6.0)
    assert spec.years == 5
    assert spec.offset_days == 2
    assert spec.max_duration_hours == 72
    assert spec.backend == "native"
    assert spec.job_id == ""


def test_derived_region_clips_to_the_globe(qtbot):
    request = SimpleNamespace(
        departure=datetime(2021, 6, 15, 12),
        start=(88.0, 176.0),
        destination=(89.0, 177.0),
        waypoints=(),
    )
    panel = historical_panel(qtbot, request=request)
    assert panel._region_from_request(request) == (171.0, 83.0, 180.0, 90.0)


def test_region_from_request_reads_points_as_lat_lon(qtbot):
    request = SimpleNamespace(
        departure=datetime(2021, 6, 15, 12),
        start=(48.4, -123.4),
        destination=(47.0, -125.0),
        waypoints=(),
    )
    panel = historical_panel(qtbot, request=request)
    west, south, east, north = panel._region_from_request(request)
    assert (west, south, east, north) == (-130.0, 42.0, -118.4, 53.4)
    assert west < east
    assert south < north


def test_region_from_request_rejects_a_longitude_span_over_180_degrees(qtbot):
    request = SimpleNamespace(
        departure=datetime(2021, 6, 15, 12),
        start=(10.0, 170.0),
        destination=(20.0, -170.0),
        waypoints=(),
    )
    panel = historical_panel(qtbot, request=request)
    with pytest.raises(ValueError, match="180 degrees of longitude"):
        panel._region_from_request(request)


def test_region_from_request_without_points_returns_none(qtbot):
    request = SimpleNamespace(
        departure=datetime(2021, 6, 15, 12),
        start=None,
        destination=None,
        waypoints=(),
    )
    panel = historical_panel(qtbot, request=request)
    assert panel._region_from_request(request) is None


class _Service:
    def __init__(self, error=None):
        self.error = error
        self.submitted = []
        self.cleared = []
        self.cancelled = []

    def submit(self, plan, *, retry=False):
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


def test_ensure_wind_skips_covered_units(qtbot, monkeypatch, tmp_path):
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
        qtbot, controller=controller, service_factory=lambda store: service
    )
    panel.ensure_wind([_unit()])
    assert service.submitted == []


def test_ensure_wind_raises_on_a_reported_error(qtbot, monkeypatch, tmp_path):
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
        qtbot, controller=controller, service_factory=lambda store: service
    )
    try:
        panel.ensure_wind([_unit()])
    except RuntimeError as error:
        assert "boom" in str(error)
    else:
        raise AssertionError("ensure_wind did not report the ERA5 error")
    assert service.submitted
    assert service.cleared == ["key"]


def test_historical_acquisition_defaults_to_the_arco_service(qtbot):
    panel = historical_panel(qtbot)
    assert panel._service_factory is ArcoDownloadService


def test_historical_acquisition_keeps_an_injected_service_factory(qtbot):
    service = _Service()
    factory = lambda store: service  # noqa: E731 - the panel only calls it once
    panel = historical_panel(qtbot, service_factory=factory)
    assert panel._service_factory is factory


def test_ensure_wind_uses_arco_without_a_cds_fallback(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(historicalera5, "read_era5_catalog", lambda root: [])
    monkeypatch.setattr(
        historicalera5,
        "select_window_entries",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("missing")),
    )
    monkeypatch.setattr(historicalera5, "plan_request", lambda *a, **k: object())

    def no_cds(store):
        raise AssertionError("historical acquisition fell back to the CDS service")

    monkeypatch.setattr(historicalera5, "Era5DownloadService", no_cds)
    service = _Service(error="arco transport refused the transfer")
    constructed = []

    def factory(store):
        constructed.append(store)
        return service

    monkeypatch.setattr(historicalrouting, "ArcoDownloadService", factory)
    store = SimpleNamespace(root=str(tmp_path))
    controller = SimpleNamespace(
        core=None, analysis=SimpleNamespace(state=SimpleNamespace(store=store))
    )
    panel = historical_panel(qtbot, controller=controller)
    with pytest.raises(RuntimeError) as error:
        panel.ensure_wind([_unit()])
    assert "arco transport refused the transfer" in str(error.value)
    assert "CDS" not in str(error.value)
    assert constructed == [store]


def test_era5_download_tab_still_uses_the_cds_service(qtbot, monkeypatch):
    sentinel = SimpleNamespace()
    stores = []

    def factory(store):
        stores.append(store)
        return sentinel

    monkeypatch.setattr(era5download, "Era5DownloadService", factory)
    monkeypatch.setattr(
        era5download.Era5DownloadTab, "refresh_catalog", lambda self: None
    )
    store = SimpleNamespace(root=".")
    controller = SimpleNamespace(
        analysis=SimpleNamespace(state=SimpleNamespace(store=store))
    )
    tab = era5download.Era5DownloadTab(controller)
    qtbot.addWidget(tab)
    assert tab.service is sentinel
    assert stores == [store]


def test_run_bound_blocks_until_finished_and_returns_outcomes(qtbot):
    outcomes = [SimpleNamespace(member_id="2020-000")]
    panel = historical_panel(qtbot)
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


def test_results_project_records_and_pick_the_median(qtbot):
    panel = historical_panel(qtbot)
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
    panel._outcomes = {
        "2020-000": SimpleNamespace(member_id="2020-000", duration_hours=120.0)
    }
    panel._show_record(record)
    assert [(row["member_id"], row["year"]) for row in panel.results._rows] == [
        ("2020-000", 2020)
    ]
    assert panel.results._rows[0]["route_uid"] == "route-a"
    assert panel.results._median_member_id == "2020-000"
    assert panel.results.delete_button.isEnabled()


def test_delete_calls_the_coordinator_and_the_collection(qtbot):
    route = SimpleNamespace(uid="route-a")
    collection = [route]
    controller = SimpleNamespace(
        core=SimpleNamespace(routingManager=collection), analysis=None
    )
    panel = historical_panel(qtbot, controller=controller)
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
    assert panel.results._rows == []
    assert panel.results._median_member_id is None
    assert panel.results.isHidden()


# -- scene attachment, retry and visibility wiring -----------------------


class _ImmediateThread:
    """Run a QThread-substitute synchronously so tests stay deterministic."""

    def __init__(self, target=None, args=(), kwargs=None, **options):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


class FakeScene:
    def __init__(self):
        self.items = {}
        self.saved = 0

    def set_item_visible(self, kind, key, visible):
        self.items[(kind, key)] = bool(visible)
        return True

    def save(self):
        self.saved += 1


def _route(uid, visible=True):
    return SimpleNamespace(uid=uid, visible=visible)


def _member_route(member_id, route_uid, year=2020, offset_days=0):
    return MemberRecord(
        member_id=member_id,
        year=year,
        offset_days=offset_days,
        state="complete",
        departure_utc=f"{year}-06-15T12:00:00+00:00",
        coverage_start_utc=f"{year}-06-15T12:00:00+00:00",
        coverage_end_utc=f"{year}-06-25T12:00:00+00:00",
        outcome="finished",
        route_uid=route_uid,
    )


def _record(*members, job_id="job-1", excluded_years=()):
    return SimpleNamespace(
        job_id=job_id,
        created_at_utc="2026-01-01T00:00:00+00:00",
        state="complete",
        spec={"summary": {}},
        members={member.member_id: member for member in members},
        excluded_years=tuple(excluded_years),
    )


def test_set_scene_attaches_and_publishes_routes_after_a_retry(qtbot):
    route = _route("route-a")
    controller = SimpleNamespace(
        core=SimpleNamespace(routingManager=[route]), analysis=None
    )
    panel = historical_panel(qtbot, controller=controller)
    published = []
    panel.coordinator = SimpleNamespace(
        scene=None,
        publisher=SimpleNamespace(scene=None),
        publish_routes=lambda job_id, routes: published.append((job_id, list(routes))),
    )
    scene = FakeScene()
    panel.set_scene(scene)
    assert panel.coordinator.scene is scene
    assert panel.coordinator.publisher.scene is scene

    record = _record(_member_route("2020-000", "route-a"))
    panel._record = record
    panel.store = SimpleNamespace(load=lambda job_id: record, list_jobs=lambda: [])
    panel._retrying = True
    panel._finished("job-1", "")

    assert published == [("job-1", [route])]
    assert [row["route_uid"] for row in panel.results._rows] == ["route-a"]


def test_retry_failed_uses_the_start_job_seams_and_refreshes_rows(qtbot, monkeypatch):
    route = _route("route-a")
    controller = SimpleNamespace(
        core=SimpleNamespace(routingManager=[route]), analysis=None
    )
    request = SimpleNamespace(
        departure=datetime(2021, 6, 15, 12),
        start=(0.0, 0.0),
        destination=(1.0, 1.0),
        waypoints=(),
    )
    panel = historical_panel(qtbot, controller=controller, request=request)
    record = _record(_member_route("2020-000", "route-a"))
    panel._record = record
    panel.store = SimpleNamespace(load=lambda job_id: record, list_jobs=lambda: [])

    calls = {}

    def retry_failed(job_id, base, **kwargs):
        calls["job_id"] = job_id
        calls["base"] = base
        calls.update(kwargs)
        return []

    panel.coordinator = SimpleNamespace(
        scene=None,
        publisher=SimpleNamespace(scene=None),
        retry_failed=retry_failed,
        publish_routes=lambda *args, **kwargs: None,
    )

    monkeypatch.setattr(historicalrouting.threading, "Thread", _ImmediateThread)
    panel.retry_failed()

    assert calls["job_id"] == "job-1"
    assert calls["base"] is request
    assert calls["ensure_wind"] == panel.ensure_wind
    assert calls["run_bound"] == panel.run_bound
    assert calls["current_selection_factory"] == panel.current_selection_factory
    assert len(panel.results._rows) == 1
    assert panel._retrying is False


def test_show_all_and_hide_all_set_item_visibility_and_save(qtbot):
    routes = [_route("route-a", False), _route("route-b", False)]
    controller = SimpleNamespace(
        core=SimpleNamespace(routingManager=routes), analysis=None
    )
    panel = historical_panel(qtbot, controller=controller)
    scene = FakeScene()
    panel.coordinator = SimpleNamespace(
        scene=scene, publisher=SimpleNamespace(scene=scene)
    )
    panel._record = _record(
        _member_route("2020-000", "route-a"),
        _member_route("2021-000", "route-b", year=2021),
    )
    emissions = []
    panel.visibility_changed.connect(lambda: emissions.append(True))

    panel._show_all()
    assert [route.visible for route in routes] == [True, True]
    assert scene.items == {("routing", "route-a"): True, ("routing", "route-b"): True}
    assert scene.saved == 1
    assert emissions

    panel._hide_all()
    assert [route.visible for route in routes] == [False, False]
    assert scene.saved == 2


def test_representatives_show_only_the_median_finished_route(qtbot):
    routes = [_route("route-a"), _route("route-b"), _route("route-c")]
    controller = SimpleNamespace(
        core=SimpleNamespace(routingManager=routes), analysis=None
    )
    panel = historical_panel(qtbot, controller=controller)
    scene = FakeScene()
    panel.coordinator = SimpleNamespace(
        scene=scene, publisher=SimpleNamespace(scene=scene)
    )
    record = _record(
        _member_route("2020-000", "route-a", year=2020),
        _member_route("2021-000", "route-b", year=2021),
        _member_route("2022-000", "route-c", year=2022),
    )
    panel._record = record
    panel._outcomes = {
        "2020-000": SimpleNamespace(member_id="2020-000", duration_hours=10.0),
        "2021-000": SimpleNamespace(member_id="2021-000", duration_hours=20.0),
        "2022-000": SimpleNamespace(member_id="2022-000", duration_hours=30.0),
    }
    panel._show_record(record)

    panel._representatives()

    assert [route.visible for route in routes] == [False, True, False]
    assert scene.items[("routing", "route-b")] is True
    assert scene.saved == 1


def test_recheck_button_enabled_only_when_a_job_has_excluded_years(qtbot):
    panel = historical_panel(qtbot)

    panel._show_record(_record(_member_route("2020-000", "route-a")))
    assert panel.results.recheck_button.isEnabled() is False

    panel._show_record(
        _record(_member_route("2020-000", "route-a"), excluded_years=(2019,))
    )
    assert panel.results.recheck_button.isEnabled() is True

    # No job means no recheck even when exclusions are remembered.
    panel.results.set_actions_enabled(False, True, False)
    assert panel.results.recheck_button.isEnabled() is False


def test_recheck_excluded_uses_the_same_seams_and_publishes_new_routes(
    qtbot, monkeypatch
):
    routes = [_route("route-a"), _route("route-b")]
    controller = SimpleNamespace(
        core=SimpleNamespace(routingManager=routes), analysis=None
    )
    request = SimpleNamespace(
        departure=datetime(2021, 6, 15, 12),
        start=(0.0, 0.0),
        destination=(1.0, 1.0),
        waypoints=(),
    )
    panel = historical_panel(qtbot, controller=controller, request=request)
    record = _record(_member_route("2020-000", "route-a"), excluded_years=(2019,))
    updated = _record(
        _member_route("2020-000", "route-a"),
        _member_route("2021-000", "route-b", year=2021),
    )
    panel._record = record
    panel.store = SimpleNamespace(load=lambda job_id: updated, list_jobs=lambda: [])

    calls = {}
    published = []

    def recheck_excluded(job_id, base, **kwargs):
        calls["job_id"] = job_id
        calls["base"] = base
        calls.update(kwargs)
        return []

    panel.coordinator = SimpleNamespace(
        scene=None,
        publisher=SimpleNamespace(scene=None),
        recheck_excluded=recheck_excluded,
        publish_routes=lambda job_id, wanted: published.append((job_id, list(wanted))),
    )

    monkeypatch.setattr(historicalrouting.threading, "Thread", _ImmediateThread)
    panel.recheck_excluded()

    assert calls["job_id"] == "job-1"
    assert calls["base"] is request
    assert calls["ensure_wind"] == panel.ensure_wind
    assert calls["run_bound"] == panel.run_bound
    assert calls["current_selection_factory"] == panel.current_selection_factory
    assert calls["concurrency"] is None
    assert [row["route_uid"] for row in panel.results._rows] == [
        "route-a",
        "route-b",
    ]
    assert published == [("job-1", routes)]
    assert panel._retrying is False


def test_cancelling_a_recheck_leaves_the_panel_usable(qtbot, monkeypatch):
    route = _route("route-a")
    controller = SimpleNamespace(
        core=SimpleNamespace(routingManager=[route]), analysis=None
    )
    request = SimpleNamespace(
        departure=datetime(2021, 6, 15, 12),
        start=(0.0, 0.0),
        destination=(1.0, 1.0),
        waypoints=(),
    )
    panel = historical_panel(qtbot, controller=controller, request=request)
    record = _record(_member_route("2020-000", "route-a"), excluded_years=(2019,))
    panel._record = record
    panel.store = SimpleNamespace(load=lambda job_id: record, list_jobs=lambda: [])

    def recheck_excluded(job_id, base, **kwargs):
        raise RuntimeError("cancelled")

    panel.coordinator = SimpleNamespace(
        scene=None,
        publisher=SimpleNamespace(scene=None),
        recheck_excluded=recheck_excluded,
        publish_routes=lambda *args, **kwargs: None,
    )

    monkeypatch.setattr(historicalrouting.threading, "Thread", _ImmediateThread)
    panel.recheck_excluded()

    assert panel._retrying is False
    assert panel._thread.is_alive() is False
    assert panel.results.recheck_button.isEnabled() is True
    assert panel.results.delete_button.isEnabled() is True
    assert len(panel.results._rows) == 1


def test_existing_compare_calculate_path_is_unchanged(qtbot, monkeypatch, tmp_path):
    panel = routing_panel(qtbot, monkeypatch, tmp_path)

    def refuse(backend=None):
        raise ValueError("boom")

    monkeypatch.setattr(panel, "_request", refuse)
    panel._calculate()
    assert panel.status.text() == "boom"
    assert panel.routing.started == []


def test_progress_snapshot_reads_the_routing_controller(qtbot):
    panel = historical_panel(qtbot)
    started = time.monotonic() - 45
    panel.routing_controller = SimpleNamespace(
        busy=True,
        bound_completed=12,
        bound_total=70,
        percent=41.0,
        progress_indeterminate=False,
        message="forward isochrone",
        native_counters={
            "generated_states": 182000,
            "retained_states": 4100,
            "land_checks": 903,
        },
        native_started_at=started,
    )

    snapshot = panel._progress_snapshot()
    assert snapshot["running"] is True
    assert snapshot["completed"] == 12
    assert snapshot["total"] == 70
    assert snapshot["route_percent"] == 41.0
    assert snapshot["route_indeterminate"] is False
    assert snapshot["stage"] == "forward isochrone"
    assert snapshot["counters"] == "182k/4.1k/903"
    assert snapshot["elapsed_seconds"] >= 40


def test_progress_snapshot_waits_for_native_routing(qtbot):
    panel = historical_panel(qtbot)
    panel.routing_controller = SimpleNamespace(
        busy=True,
        bound_completed=0,
        bound_total=0,
        percent=0.0,
        progress_indeterminate=False,
        message="downloading ERA5",
        native_counters=None,
        native_started_at=None,
    )

    snapshot = panel._progress_snapshot()
    assert snapshot["route_percent"] is None
    assert snapshot["route_indeterminate"] is True
    assert snapshot["routing"] is False
    assert snapshot["completed"] == 0


def test_progress_snapshot_reports_routing_once_it_starts(qtbot):
    panel = historical_panel(qtbot)
    panel.routing_controller = SimpleNamespace(
        busy=True,
        bound_completed=4,
        bound_total=70,
        percent=12.0,
        progress_indeterminate=False,
        message="forward isochrone",
        native_counters={},
        native_started_at=None,
    )

    snapshot = panel._progress_snapshot()
    assert snapshot["routing"] is True
    assert snapshot["route_percent"] == 12.0
    assert snapshot["completed"] == 4

    # A started native engine alone is enough, even before a member binds.
    panel.routing_controller = SimpleNamespace(
        busy=True,
        bound_completed=0,
        bound_total=0,
        percent=5.0,
        progress_indeterminate=False,
        message="native startup",
        native_counters={},
        native_started_at=time.monotonic() - 5,
    )

    snapshot = panel._progress_snapshot()
    assert snapshot["routing"] is True
    assert snapshot["route_percent"] == 5.0


def test_acquiring_stage_updates_the_status_text(qtbot):
    panel = historical_panel(qtbot)
    panel.actions.progress("acquiring", 3, 12, "downloading")

    # Counts live in the bars; the status line stays a short phase label.
    assert panel.options.status.text() == "Acquiring weather"
    widget = panel.options.progress_widget
    assert widget.overall_bar.maximum() == 12
    assert widget.overall_bar.value() == 3


def test_weather_progress_events_drive_the_progress_widget(qtbot):
    panel = historical_panel(qtbot)
    panel._progress("acquiring", 2, 10, "Downloading ERA5 wind")

    widget = panel.options.progress_widget
    assert widget.overall_bar.maximum() == 10
    assert widget.overall_bar.value() == 2
    assert widget.route_bar.isHidden()
    # Acquisition owns the widget and replaces the stale start-up status.
    assert panel.options.status.text() == "Acquiring weather"
    assert not panel.options.status.isHidden()


def test_finished_marks_the_progress_widget(qtbot):
    panel = historical_panel(qtbot)
    panel._finished("", "boom")
    assert panel.options.progress_widget.overall_label.text() == "boom"

    panel._finished("", "")
    assert panel.options.progress_widget.overall_label.text() == "done"


def test_is_running_tracks_the_worker_thread(qtbot):
    """A live worker is busy even while the routing controller is idle."""
    panel = historical_panel(qtbot)
    assert panel.is_running() is False

    thread = SimpleNamespace(is_alive=lambda: True, alive_flag=True)
    panel._thread = thread
    assert panel.is_running() is True

    thread.is_alive = lambda: False
    assert panel.is_running() is False


def test_routing_panel_hides_its_duplicate_progress_while_historical_runs():
    """One progress display and one Cancel, not two of each."""
    from types import SimpleNamespace

    from sailworks.qt.routingpanel import RoutingPanel

    seen = {}

    def widget(name):
        return SimpleNamespace(
            setVisible=lambda visible, n=name: seen.__setitem__(n, visible)
        )

    panel = SimpleNamespace(
        progress=widget("progress"),
        status=widget("status"),
        cancel_button=widget("cancel"),
    )

    panel.routing = SimpleNamespace(busy=False)

    # Acquisition: only the historical panel's Cancel can stop it.
    RoutingPanel._sync_run_visibility(panel, True)
    assert seen == {"progress": False, "status": False, "cancel": False}

    # Routing: the engine owns the Cancel.
    panel.routing.busy = True
    RoutingPanel._sync_run_visibility(panel, True)
    assert seen == {"progress": False, "status": False, "cancel": True}

    # Idle: the normal routing controls come back.
    panel.routing.busy = False
    RoutingPanel._sync_run_visibility(panel, False)
    assert seen == {"progress": True, "status": True, "cancel": True}


def test_snapshot_replaces_the_stale_acquisition_status(qtbot):
    panel = historical_panel(qtbot)
    panel.routing_controller = SimpleNamespace(
        busy=True,
        message="Native scenario 2/63 · effort 40%",
        bound_total=63,
        bound_completed=1,
        progress_indeterminate=False,
        percent=40.0,
        native_started_at=None,
        native_counters=None,
    )
    panel.options.set_status("Acquiring weather")

    panel._progress_snapshot()

    assert panel.options.status.text() == "Native scenario 2/63 · effort 40%"
