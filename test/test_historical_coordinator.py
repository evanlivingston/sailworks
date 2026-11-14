"""Headless tests for :mod:`sailworks.core.historicalcoordinator`.

Every external effect — wind acquisition, currents, the bound runner and the
scene — is a fake, so the whole sequence runs with a real on-disk store and no
Qt, native build or network.
"""

from datetime import datetime, timezone

import pytest

from sailworks.core import historicalcoordinator, historicaljob
from sailworks.core.historicalcoordinator import (
    CoordinatorError,
    HistoricalRoutingCoordinator,
)
from sailworks.core.historicalexecution import MemberOutcome
from sailworks.core.historicalrouting import HistoricalRoutingSpec, plan_members
from sailworks.core.historicalroutingstore import HistoricalRoutingStore
from sailworks.core.routingjob import RouteRequest

UTC = timezone.utc
SELECTED_LOCAL = datetime(2019, 6, 1, 12, 0)
CURRENT = datetime(2026, 1, 1, tzinfo=UTC)


def _spec(**overrides):
    values = {
        "job_id": "job-210",
        "created_at_utc": datetime(2019, 5, 1, tzinfo=UTC),
        "selected_departure_local": SELECTED_LOCAL,
        "timezone_id": "Europe/Oslo",
        "reference_year": 2019,
        "years": 2,
        "offset_days": 1,
        "max_duration_hours": 240,
        "region": (-10.0, 50.0, 10.0, 60.0),
        "source_id": "ERA5",
    }
    values.update(overrides)
    return HistoricalRoutingSpec(**values)


def _request(**overrides):
    values = {
        "name": "Base route",
        "start": (0.0, 0.0),
        "destination": (1.0, 1.0),
        "departure": datetime(2019, 6, 1, 12, 0, tzinfo=UTC),
        "polar_file": "base.pol",
        "algorithm": None,
    }
    values.update(overrides)
    return RouteRequest(**values)


class FakeWind:
    """Implements the documented contract: ``ensure_wind(units, progress=None)``."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, units, progress=None):
        self.calls.append(tuple(units))
        if self.error is not None:
            raise self.error


class FakeRunner:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, executions, *, on_outcome=None, concurrency=None):
        self.calls.append((tuple(executions), concurrency))
        for outcome in self.outcomes:
            on_outcome(outcome)
        return list(self.outcomes)


class FakeRoute:
    def __init__(self, uid):
        self.uid = uid
        self.name = uid


class FakeCollection:
    def __init__(self):
        self.routes = {}

    def add(self, route):
        self.routes[route.uid] = route

    def remove(self, route):
        self.routes.pop(route.uid, None)
        return True


class FakeScene:
    def __init__(self):
        self.calls = []
        self.groups = {}

    def ensure_group(self, identifier, name, parent=None):
        self.calls.append(("ensure_group", identifier, name, parent))
        self.groups.setdefault(identifier, name)
        return identifier

    def assign(self, keys, group, save=True):
        self.calls.append(("assign", frozenset(keys), group))
        return True

    def reconcile(self, entries):
        self.calls.append(("reconcile", tuple(entries)))

    def save(self):
        self.calls.append(("save",))
        return False

    def count(self, name):
        return sum(1 for call in self.calls if call[0] == name)


def _patch_prepare(monkeypatch, availability=None):
    """Inject availability through ``preflight`` without touching production code."""
    chosen = availability if availability is not None else (lambda window: None)

    def patched(spec, *, now=None, availability=None):
        if availability is None:
            availability = chosen
        return historicaljob.prepare_job(spec, now=now, availability=availability)

    monkeypatch.setattr(historicalcoordinator, "prepare_job", patched)


def _outcomes(spec, kind="finished"):
    return [
        MemberOutcome(plan.member_id, kind, 48.0, route_uid=f"uid-{plan.member_id}")
        for plan in plan_members(spec)
    ]


def _start(monkeypatch, store, spec, *, availability=None, scene=None, outcomes=None, **kwargs):
    _patch_prepare(monkeypatch, availability)
    coordinator = HistoricalRoutingCoordinator(store=store, scene=scene, now=CURRENT)
    wind = kwargs.pop("ensure_wind", FakeWind())
    runner = kwargs.pop("run_bound", FakeRunner(outcomes if outcomes is not None else ()))
    kwargs.setdefault("tranche_factory", lambda member: object())
    job_id = coordinator.start(spec, _request(), ensure_wind=wind, run_bound=runner, **kwargs)
    return coordinator, job_id, wind, runner


def test_happy_path_checkpoints_every_stage(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    spec = _spec()
    coordinator, job_id, wind, runner = _start(
        monkeypatch, store, spec, outcomes=_outcomes(spec), concurrency=2
    )

    record = store.load(job_id)
    assert record.state == "complete"
    assert len(record.members) == 6
    assert record.requested_years == (2018, 2019)
    assert record.effective_years == (2018, 2019)
    assert record.excluded_years == ()
    assert len(wind.calls) == 1
    assert wind.calls[0], "acquisition units must be planned"
    assert all(unit.fields == ("wind",) for unit in wind.calls[0])
    executions, concurrency = runner.calls[0]
    assert len(executions) == 6
    assert concurrency == 2
    assert {member.state for member in record.members.values()} == {"finished"}
    assert {member.attempts for member in record.members.values()} == {1}
    assert len(record.attempt_history) == 6
    summary = record.spec["summary"]
    assert summary["effective_years"] == 2
    assert summary["finished"] == 6
    assert record.last_checkpoint_utc


def test_one_unavailable_year_is_excluded_whole(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    spec = _spec()
    availability = lambda window: ("no coverage" if window.start_utc.year < 2019 else None)
    coordinator, job_id, wind, runner = _start(
        monkeypatch,
        store,
        spec,
        availability=availability,
        outcomes=[
            MemberOutcome(plan.member_id, "finished", 40.0)
            for plan in plan_members(spec)
            if plan.year == 2019
        ],
    )

    record = store.load(job_id)
    assert record.state == "complete_with_exclusions"
    assert record.excluded_years == (2018,)
    assert record.effective_years == (2019,)
    assert record.years[2018].state == "excluded"
    assert record.years[2018].reason
    assert record.spec["exclusions"][0]["year"] == 2018
    assert len(record.members) == 3
    assert len(runner.calls[0][0]) == 3


def test_all_years_unavailable_awaits_availability(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    coordinator, job_id, wind, runner = _start(
        monkeypatch,
        store,
        _spec(),
        availability=lambda window: "no coverage",
    )

    record = store.load(job_id)
    assert record.state == "awaiting_availability"
    assert record.effective_years == ()
    assert record.excluded_years == (2018, 2019)
    assert record.members == {}
    assert wind.calls == []
    assert runner.calls == []


def test_wind_failure_parks_job_and_propagates(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    failure = RuntimeError("wind blew")
    with pytest.raises(RuntimeError, match="wind blew"):
        _start(monkeypatch, store, _spec(), ensure_wind=FakeWind(error=failure))

    record = store.list_jobs()[-1]
    assert record.state == "needs_attention"
    assert "wind blew" in record.spec["last_error"]
    assert record.attempt_history[-1]["stage"] == "acquiring"


def test_currents_without_acquisition_refuse_to_downgrade(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    spec = _spec(required_fields=("wind", "currents"))
    with pytest.raises(CoordinatorError, match="current acquisition"):
        _start(monkeypatch, store, spec)

    record = store.list_jobs()[-1]
    assert record.state == "needs_attention"
    assert "currents" not in record.spec
    assert "current_binding" not in record.spec
    assert record.current_binding == {}


def test_member_failures_are_isolated_and_indeterminate(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    spec = _spec()
    plans = plan_members(spec)
    outcomes = [
        MemberOutcome(plans[0].member_id, "failed", None, error="boom"),
        MemberOutcome(plans[1].member_id, "finished", 12.0, route_uid="uid-ok"),
        *[MemberOutcome(plan.member_id, "finished", 30.0) for plan in plans[2:]],
    ]
    coordinator, job_id, wind, runner = _start(monkeypatch, store, spec, outcomes=outcomes)

    record = store.load(job_id)
    # A failure is indeterminate for the ensemble, so the job is not "complete".
    assert record.state == "needs_attention"
    failed = record.members[plans[0].member_id]
    assert failed.state == "failed"
    assert failed.outcome == "failed"
    assert failed.error == "boom"
    finished = record.members[plans[1].member_id]
    assert finished.state == "finished"
    assert finished.route_uid == "uid-ok"


def test_unknown_member_outcome_raises(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    outcomes = [MemberOutcome("not-a-member", "finished", 1.0)]
    with pytest.raises(CoordinatorError, match="unknown member"):
        _start(monkeypatch, store, _spec(), outcomes=outcomes)


def test_publish_routes_is_grouped_and_idempotent(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    scene = FakeScene()
    spec = _spec()
    coordinator, job_id = _start(monkeypatch, store, spec, scene=scene, outcomes=_outcomes(spec))[
        :2
    ]
    routes = [FakeRoute("uid-a"), FakeRoute("uid-b")]

    assert scene.count("ensure_group") == 1
    assert coordinator.publish_routes(job_id, routes) is True
    assert scene.count("ensure_group") == 1
    assigned = {key for call in scene.calls if call[0] == "assign" for key in call[1]}
    assert assigned == {("routing", "uid-a"), ("routing", "uid-b")}
    assert scene.count("assign") == 2
    assert scene.count("save") == 1
    assert store.load(job_id).scene_group_id == f"historical-routing:{job_id}"

    assert coordinator.publish_routes(job_id, routes) is False
    assert scene.count("ensure_group") == 1
    assert scene.count("assign") == 2
    assert scene.count("save") == 1


def test_delete_job_removes_routes_releases_pins_and_archives(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    spec = _spec()
    coordinator, job_id = _start(monkeypatch, store, spec, outcomes=_outcomes(spec))[:2]
    record = store.load(job_id)
    store.pin(record, ["a" * 64])
    assert store.is_pinned("a" * 64)

    collection = FakeCollection()
    routes = [FakeRoute("uid-a"), FakeRoute("uid-b")]
    for route in routes:
        collection.add(route)

    removed = coordinator.delete_job(job_id, routes, collection=collection)

    assert removed == ("uid-a", "uid-b")
    assert collection.routes == {}
    record = store.load(job_id)
    assert record.state == "archived"
    assert record.scene_detached is True
    assert record.artifact_pins == ()
    assert not store.is_pinned("a" * 64)


def test_recover_returns_store_dispositions(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    spec = _spec()
    coordinator, complete_id = _start(monkeypatch, store, spec, outcomes=_outcomes(spec))[:2]
    record = store.load(complete_id)
    record.state = "running"
    store.save(record)
    second = store.create({"job_id": "other"}, ())
    store.set_job_state(second, "planned")

    dispositions = coordinator.recover()
    by_id = {item[0].job_id: item[1] for item in dispositions if hasattr(item[0], "job_id")}

    assert by_id[complete_id] == "resume"
    assert by_id[second.job_id] == "attention"
