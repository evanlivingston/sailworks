"""Wiring checks for the Historical years panel inside the Compare page."""

import os
from datetime import datetime, timezone
from pathlib import Path
import threading
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QScrollArea, QTreeWidgetItem

import sailworks.qt.historicalera5 as historicalera5
import sailworks.qt.historicalrouting as historicalrouting
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
    assert panel.results.table.isVisibleTo(scroll)
    assert panel.results.canvas.width() > 0
    assert panel.results.canvas.height() > 0


def test_mode_combo_switches_the_stack_and_dispatches(qtbot, monkeypatch, tmp_path):
    panel = routing_panel(qtbot, monkeypatch, tmp_path)
    assert panel.compare_mode.currentData() == "parameter"
    assert panel.compare_stack.currentIndex() == 0

    panel.compare_mode.setCurrentIndex(panel.compare_mode.findData("historical"))
    assert panel.compare_stack.currentIndex() == 1

    calls = []
    monkeypatch.setattr(panel.historical, "start_job", lambda: calls.append(True))
    panel._calculate()
    assert calls == [True]
    assert panel.routing.started == []


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
    assert panel.compare_mode.currentData() == "historical"
    assert panel.compare_stack.currentIndex() == 1
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


def test_complete_plan_route_enables_the_run_button(qtbot):
    request = SimpleNamespace(
        departure=datetime(2021, 6, 15, 12),
        start=(1.0, 2.0),
        destination=(3.0, 4.0),
        waypoints=((5.0, 6.0),),
        polar_file="/tmp/Elan 410.pol",
        algorithm=type("LinearBestIsoRouter", (), {}),
        use_currents=True,
    )
    panel = historical_panel(qtbot, request=request)
    assert panel.options.run_button.isEnabled()
    assert panel.options.run_button.toolTip() == ""


def test_incomplete_plan_route_disables_the_run_button_with_hint(qtbot):
    request = SimpleNamespace(start=(1.0, 2.0), destination=(3.0, 4.0))
    panel = historical_panel(qtbot, request=request)
    assert not panel.options.run_button.isEnabled()
    assert panel.options.run_button.toolTip() == "Set the route and boat in Plan first."

    panel = historical_panel(qtbot, request=None)
    assert not panel.options.run_button.isEnabled()
    assert panel.options.run_button.toolTip() == "Set the route and boat in Plan first."


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
    assert panel.results.table.rowCount() == 1
    assert "1 complete years" in panel.results.summary.text()


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
    assert saved["compare_mode"] == "parameter"
    assert saved["historical"] == panel.historical.values()

    saved["compare_mode"] = "historical"
    saved["historical"] = {
        "years": 4,
        "offset_days": 1,
        "max_duration_hours": 120,
        "use_currents": True,
        "region": list(REGION),
        "current_priority": "newest",
    }
    panel.restore_options(saved)
    assert panel.compare_mode.currentData() == "historical"
    assert panel.compare_stack.currentIndex() == 1
    assert panel.historical.values() == {
        "years": 4,
        "offset_days": 1,
        "max_duration_hours": 120,
        "use_currents": True,
    }

    panel.restore_options({"compare_mode": "not-a-mode", "historical": "ignored"})
    assert panel.compare_mode.currentData() == "parameter"


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
        start=(178.0, -88.0),
        destination=(-179.0, 89.0),
        waypoints=(),
    )
    panel = historical_panel(qtbot, request=request)
    assert panel._region_from_request(request) == (-180.0, -90.0, 180.0, 90.0)


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


def test_results_populate_from_a_fake_summary_and_records(qtbot):
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
    panel._show_record(record)
    assert "1 complete years" in panel.results.summary.text()
    assert panel.results.table.rowCount() == 1
    assert panel.results.table.item(0, 0).text() == "2020"
    assert panel.results.canvas._curve == [(0.0, 0.0), (10.0, 1.0)]
    assert panel.results.canvas._strip == [
        {"year": 2020, "offset_days": 0, "outcome": "finished"}
    ]


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
    assert panel.results.table.rowCount() == 0


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
    assert panel.results.table.rowCount() == 1


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
    assert panel.results.table.rowCount() == 1
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
    assert panel.results.table.rowCount() == 2
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
    assert panel.results.table.rowCount() == 1


def test_existing_compare_calculate_path_is_unchanged(qtbot, monkeypatch, tmp_path):
    panel = routing_panel(qtbot, monkeypatch, tmp_path)
    assert panel.compare_mode.currentData() == "parameter"

    def refuse(backend=None):
        raise ValueError("boom")

    monkeypatch.setattr(panel, "_request", refuse)
    panel._calculate()
    assert panel.status.text() == "boom"
    assert panel.routing.started == []
