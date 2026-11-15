"""Headless tests for :mod:`sailworks.core.historicalcoordinator`.

Every external effect — wind acquisition, currents, the bound runner and the
scene — is a fake, so the whole sequence runs with a real on-disk store and no
Qt, native build or network.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sailworks.core import historicalcoordinator, historicaljob, historicalrecheck
from sailworks.core.historicalcoordinator import (
    CoordinatorError,
    HistoricalRoutingCoordinator,
)
from sailworks.core.historicalexecution import MemberOutcome
from sailworks.core.historicaljob import CurrentBinding
from sailworks.core.historicalretry import HistoricalRetrier, _is_retryable
from sailworks.core.historicalrouting import HistoricalRoutingSpec, plan_members
from sailworks.core.historicalroutingrecords import AcquisitionRecord
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


def _start(
    monkeypatch, store, spec, *, availability=None, scene=None, outcomes=None, **kwargs
):
    _patch_prepare(monkeypatch, availability)
    coordinator = HistoricalRoutingCoordinator(store=store, scene=scene, now=CURRENT)
    wind = kwargs.pop("ensure_wind", FakeWind())
    runner = kwargs.pop(
        "run_bound", FakeRunner(outcomes if outcomes is not None else ())
    )
    kwargs.setdefault("tranche_factory", lambda member: object())
    job_id = coordinator.start(
        spec, _request(), ensure_wind=wind, run_bound=runner, **kwargs
    )
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
    availability = lambda window: (
        "no coverage" if window.start_utc.year < 2019 else None
    )
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
    coordinator, job_id, wind, runner = _start(
        monkeypatch, store, spec, outcomes=outcomes
    )

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
    coordinator, job_id = _start(
        monkeypatch, store, spec, scene=scene, outcomes=_outcomes(spec)
    )[:2]
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


def test_published_routes_hide_isochrones_until_the_user_asks(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    scene = FakeScene()
    spec = _spec()
    coordinator, job_id = _start(
        monkeypatch, store, spec, scene=scene, outcomes=_outcomes(spec)
    )[:2]
    routes = [FakeRoute("uid-a"), FakeRoute("uid-b")]

    coordinator.publish_routes(job_id, routes)

    # An ensemble publishes many routes at once: the lines stay on, the
    # contours do not, so the map is readable until the user opts in.
    assert [route.isochrones_visible for route in routes] == [False, False]

    # A later republish must not undo a manual toggle.
    routes[0].isochrones_visible = True
    coordinator.publish_routes(job_id, routes)
    assert routes[0].isochrones_visible is True


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
    coordinator, complete_id = _start(
        monkeypatch, store, spec, outcomes=_outcomes(spec)
    )[:2]
    record = store.load(complete_id)
    record.state = "running"
    store.save(record)
    second = store.create({"job_id": "other"}, ())
    store.set_job_state(second, "planned")

    dispositions = coordinator.recover()
    by_id = {
        item[0].job_id: item[1] for item in dispositions if hasattr(item[0], "job_id")
    }

    assert by_id[complete_id] == "resume"
    assert by_id[second.job_id] == "attention"


def _mixed_start(monkeypatch, store):
    """A started job with one failed, one no_finish and finished members."""
    spec = _spec()
    plans = plan_members(spec)
    outcomes = [
        MemberOutcome(plans[0].member_id, "failed", None, error="boom"),
        MemberOutcome(plans[1].member_id, "finished", 12.0, route_uid="uid-ok"),
        MemberOutcome(plans[2].member_id, "no_finish", None),
        *[
            MemberOutcome(
                plan.member_id, "finished", 30.0, route_uid=f"uid-{plan.member_id}"
            )
            for plan in plans[3:]
        ],
    ]
    coordinator, job_id, _wind, _runner = _start(
        monkeypatch, store, spec, outcomes=outcomes
    )
    return spec, plans, coordinator, job_id


def _retry(coordinator, job_id, outcomes, **kwargs):
    wind = kwargs.pop("ensure_wind", FakeWind())
    runner = kwargs.pop("run_bound", FakeRunner(outcomes))
    kwargs.setdefault("tranche_factory", lambda member: object())
    retried = coordinator.retry_failed(
        job_id, _request(), ensure_wind=wind, run_bound=runner, **kwargs
    )
    return retried, wind, runner


def test_retry_failed_reruns_only_indeterminate_members(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    spec, plans, coordinator, job_id = _mixed_start(monkeypatch, store)

    retried, _wind, _runner = _retry(
        coordinator,
        job_id,
        [MemberOutcome(plans[0].member_id, "finished", 20.0, route_uid="uid-retry")],
    )

    assert [outcome.member_id for outcome in retried] == [plans[0].member_id]
    record = store.load(job_id)
    assert len(record.members) == len(plans)
    failed = record.members[plans[0].member_id]
    assert failed.state == "finished"
    assert failed.route_uid == "uid-retry"
    assert failed.attempts == 2
    finished = record.members[plans[1].member_id]
    assert finished.route_uid == "uid-ok"
    assert finished.attempts == 1
    determinate = record.members[plans[2].member_id]
    assert determinate.state == "no_finish_within_horizon"
    assert determinate.outcome == "no_finish"
    assert determinate.attempts == 1
    assert determinate.route_uid == ""
    assert (
        coordinator.retry_failed(
            job_id, _request(), ensure_wind=FakeWind(), run_bound=FakeRunner()
        )
        == []
    )


def test_retry_success_completes_job_without_duplicating_members(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    spec, plans, coordinator, job_id = _mixed_start(monkeypatch, store)
    assert store.load(job_id).state == "needs_attention"

    retried, _wind, runner = _retry(
        coordinator,
        job_id,
        [MemberOutcome(plans[0].member_id, "finished", 20.0, route_uid="uid-retry")],
    )

    record = store.load(job_id)
    assert record.state == "complete"
    assert len(record.members) == len(plans)
    assert len(record.effective_years) == 2
    assert len(runner.calls[0][0]) == 1
    summary = record.spec["summary"]
    assert summary["finished"] == len(plans) - 1
    assert summary["no_finish"] == 1
    assert summary["failed"] == 0
    assert summary["effective_years"] == 2
    assert [outcome.route_uid for outcome in retried] == ["uid-retry"]


def test_retry_failure_parks_job_and_records_the_attempt(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    spec, plans, coordinator, job_id = _mixed_start(monkeypatch, store)

    retried, _wind, _runner = _retry(
        coordinator,
        job_id,
        [MemberOutcome(plans[0].member_id, "failed", None, error="still broken")],
    )

    assert len(retried) == 1
    record = store.load(job_id)
    assert record.state == "needs_attention"
    failed = record.members[plans[0].member_id]
    assert failed.state == "failed"
    assert failed.error == "still broken"
    assert failed.attempts == 2
    retries = [
        entry for entry in record.attempt_history if entry.get("event") == "retry"
    ]
    assert len(retries) == 1
    assert retries[0]["member_id"] == plans[0].member_id
    assert retries[0]["attempt"] == 2


def test_retry_acquires_only_the_retryable_members_units(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    spec, plans, coordinator, job_id = _mixed_start(monkeypatch, store)

    _retried, wind, _runner = _retry(
        coordinator, job_id, [MemberOutcome(plans[0].member_id, "finished", 20.0)]
    )

    units = tuple(wind.calls[0])
    ids = [unit.unit_id for unit in units]
    assert ids == sorted(set(ids)), "units must be deduplicated and sorted"
    assert set(ids) == {
        unit.unit_id for unit in historicaljob._plan_units((plans[0],), spec)
    }
    assert len(ids) < len(
        {unit.unit_id for unit in historicaljob._plan_units(tuple(plans), spec)}
    )


def test_retrying_currents_job_without_a_binding_refuses(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    spec = _spec(required_fields=("wind", "currents"))
    plans = plan_members(spec)
    binding = CurrentBinding(
        enabled=True,
        priority="prefer_currents",
        target_departure_utc=datetime(2019, 6, 1, 12, 0, tzinfo=UTC),
        coverage_end_utc=datetime(2019, 6, 8, 12, 0, tzinfo=UTC),
    )
    acquisition = SimpleNamespace(
        binding=binding,
        coverage_end_utc=binding.coverage_end_utc,
        hold_seconds=0.0,
        provider="fixture",
        field_ids=(),
        field_hashes=(),
    )
    outcomes = [
        MemberOutcome(plans[0].member_id, "failed", None, error="boom"),
        *[MemberOutcome(plan.member_id, "finished", 10.0) for plan in plans[1:]],
    ]
    coordinator, job_id, _wind, _runner = _start(
        monkeypatch,
        store,
        spec,
        outcomes=outcomes,
        acquire_currents=lambda prepared: acquisition,
        current_selection_factory=lambda currents: object(),
    )
    record = store.load(job_id)
    record.current_binding = {}
    store.save(record)

    with pytest.raises(CoordinatorError, match="current"):
        coordinator.retry_failed(
            job_id,
            _request(),
            ensure_wind=FakeWind(),
            run_bound=FakeRunner(),
            tranche_factory=lambda member: object(),
            current_selection_factory=lambda currents: object(),
        )

    assert store.load(job_id).current_binding == {}
    assert store.load(job_id).state == "needs_attention"


def test_retrying_an_archived_job_refuses(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    _spec_value, _plans, coordinator, job_id = _mixed_start(monkeypatch, store)
    record = store.load(job_id)
    record.state = "archived"
    store.save(record)

    with pytest.raises(CoordinatorError, match="archived"):
        coordinator.retry_failed(
            job_id,
            _request(),
            ensure_wind=FakeWind(),
            run_bound=FakeRunner(),
            tranche_factory=lambda member: object(),
        )


def test_rerun_failed_units_reacquires_only_failed_units(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    spec, plans, coordinator, job_id = _mixed_start(monkeypatch, store)
    units = historicaljob._plan_units(tuple(plans), spec)
    assert len(units) > 1
    record = store.load(job_id)
    failed_unit, healthy_unit = units[0], units[1]
    record.acquisitions[failed_unit.unit_id] = AcquisitionRecord(
        unit_id=failed_unit.unit_id,
        state="interrupted",
        start_utc="",
        end_utc="",
        error="lost",
        attempts=1,
    )
    record.acquisitions[healthy_unit.unit_id] = AcquisitionRecord(
        unit_id=healthy_unit.unit_id,
        state="complete",
        start_utc="",
        end_utc="",
    )
    store.save(record)

    wind = FakeWind()
    runner = FakeRunner()
    calls_before = len(runner.calls)
    count = coordinator.rerun_failed_units(job_id, ensure_wind=wind)

    assert count == 1
    assert len(wind.calls) == 1
    assert [unit.unit_id for unit in wind.calls[0]] == [failed_unit.unit_id]
    record = store.load(job_id)
    assert record.acquisitions[failed_unit.unit_id].state == "complete"
    assert record.acquisitions[failed_unit.unit_id].attempts == 2
    assert record.acquisitions[healthy_unit.unit_id].state == "complete"
    assert record.acquisitions[healthy_unit.unit_id].attempts == 0
    assert len(runner.calls) == calls_before, "unit retry must never run routing"


def test_coordinator_delegates_retries_to_the_retrier(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    _spec_value, _plans, coordinator, job_id = _mixed_start(monkeypatch, store)
    assert isinstance(coordinator.retrier, HistoricalRetrier)
    assert coordinator.retrier.store is store

    calls = []
    sentinel = [object()]

    def fake_retry(retried_job_id, base_request, **kwargs):
        calls.append((retried_job_id, base_request, kwargs))
        return sentinel

    def fake_units(retried_job_id, **kwargs):
        calls.append((retried_job_id, kwargs))
        return 7

    monkeypatch.setattr(coordinator.retrier, "retry_failed", fake_retry)
    monkeypatch.setattr(coordinator.retrier, "rerun_failed_units", fake_units)
    request = _request()
    wind, runner = FakeWind(), FakeRunner()

    assert (
        coordinator.retry_failed(job_id, request, ensure_wind=wind, run_bound=runner)
        is sentinel
    )
    assert calls[0][0] == job_id
    assert calls[0][1] is request
    assert calls[0][2]["ensure_wind"] is wind
    assert calls[0][2]["run_bound"] is runner
    assert calls[0][2]["concurrency"] is None
    assert calls[0][2]["tranche_factory"] is None
    assert calls[0][2]["current_selection_factory"] is None

    assert coordinator.rerun_failed_units(job_id, ensure_wind=wind) == 7
    assert calls[1] == (job_id, {"ensure_wind": wind, "on_progress": None})


def test_determinate_no_finish_member_is_never_retried(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    spec = _spec()
    plans = plan_members(spec)
    outcomes = [
        MemberOutcome(plans[0].member_id, "no_finish", None),
        *[MemberOutcome(plan.member_id, "finished", 20.0) for plan in plans[1:]],
    ]
    coordinator, job_id, _wind, _runner = _start(
        monkeypatch, store, spec, outcomes=outcomes
    )
    runner = FakeRunner()
    wind = FakeWind()

    retried = coordinator.retry_failed(
        job_id,
        _request(),
        ensure_wind=wind,
        run_bound=runner,
        tranche_factory=lambda member: object(),
    )

    assert retried == []
    assert wind.calls == []
    assert runner.calls == []
    record = store.load(job_id)
    assert record.state == "complete"
    assert record.members[plans[0].member_id].attempts == 1
    assert record.members[plans[0].member_id].outcome == "no_finish"
    assert _is_retryable(record.members[plans[0].member_id]) is False


def _currents_acquisition():
    binding = CurrentBinding(
        enabled=True,
        priority="prefer_currents",
        target_departure_utc=datetime(2019, 6, 1, 12, 0, tzinfo=UTC),
        coverage_end_utc=datetime(2019, 6, 8, 12, 0, tzinfo=UTC),
    )
    return SimpleNamespace(
        binding=binding,
        coverage_end_utc=binding.coverage_end_utc,
        hold_seconds=0.0,
        provider="fixture",
        field_ids=(),
        field_hashes=(),
    )


def _excluded_2018_start(monkeypatch, store, *, currents=False):
    """A started job whose 2018 year was dropped whole for missing coverage."""
    spec = _spec(required_fields=("wind", "currents")) if currents else _spec()
    availability = lambda window: (
        "no coverage" if window.start_utc.year < 2019 else None
    )
    plans = plan_members(spec)
    outcomes = [
        MemberOutcome(plan.member_id, "finished", 30.0)
        for plan in plans
        if plan.year == 2019
    ]
    kwargs = {}
    if currents:
        kwargs["acquire_currents"] = lambda prepared: _currents_acquisition()
        kwargs["current_selection_factory"] = lambda selection: object()
    coordinator, job_id, _wind, _runner = _start(
        monkeypatch, store, spec, availability=availability, outcomes=outcomes, **kwargs
    )
    return spec, plans, coordinator, job_id


def _recheck(coordinator, job_id, outcomes, **kwargs):
    wind = kwargs.pop("ensure_wind", FakeWind())
    runner = kwargs.pop("run_bound", FakeRunner(outcomes))
    kwargs.setdefault("tranche_factory", lambda member: object())
    returned = coordinator.recheck_excluded(
        job_id, _request(), ensure_wind=wind, run_bound=runner, **kwargs
    )
    return returned, wind, runner


def test_recheck_admits_a_year_that_became_available(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    spec, plans, coordinator, job_id = _excluded_2018_start(monkeypatch, store)
    assert store.load(job_id).excluded_years == (2018,)

    monkeypatch.setattr(
        historicalrecheck, "availability_reason", lambda window, now=None: None
    )
    fresh = [
        MemberOutcome(plan.member_id, "finished", 18.0)
        for plan in plans
        if plan.year == 2018
    ]
    returned, wind, runner = _recheck(coordinator, job_id, fresh)

    new_ids = {plan.member_id for plan in plans if plan.year == 2018}
    assert {outcome.member_id for outcome in returned} == new_ids
    record = store.load(job_id)
    assert set(record.members) == {plan.member_id for plan in plans}
    assert record.excluded_years == ()
    assert record.effective_years == (2018, 2019)
    assert record.requested_years == (2018, 2019)
    assert record.years[2018].state == "eligible"
    assert record.years[2018].previous_reasons
    assert record.state == "complete"
    assert record.spec["summary"]["effective_years"] == 2
    assert record.spec["summary"]["finished"] == len(plans)
    assert len(wind.calls) == 1
    assert {unit.unit_id for unit in wind.calls[0]} == {
        unit.unit_id
        for unit in historicaljob._plan_units(
            tuple(plan for plan in plans if plan.year == 2018), spec
        )
    }
    assert len(runner.calls[0][0]) == len(new_ids)


def test_recheck_keeps_a_year_that_is_still_unavailable(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    _spec_value, _plans, coordinator, job_id = _excluded_2018_start(monkeypatch, store)
    before = store.load(job_id)
    checkpoint = before.last_checkpoint_utc
    history = len(before.attempt_history)

    monkeypatch.setattr(
        historicalrecheck,
        "availability_reason",
        lambda window, now=None: "still no coverage",
    )
    returned, wind, runner = _recheck(coordinator, job_id, [])

    assert returned == []
    assert wind.calls == []
    assert runner.calls == []
    after = store.load(job_id)
    assert after.last_checkpoint_utc == checkpoint
    assert len(after.attempt_history) == history
    assert after.excluded_years == (2018,)
    assert after.years[2018].state == "excluded"
    assert after.years[2018].reason


def test_recheck_is_idempotent_after_success(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    _spec_value, plans, coordinator, job_id = _excluded_2018_start(monkeypatch, store)
    monkeypatch.setattr(
        historicalrecheck, "availability_reason", lambda window, now=None: None
    )
    fresh = [
        MemberOutcome(plan.member_id, "finished", 18.0)
        for plan in plans
        if plan.year == 2018
    ]
    _recheck(coordinator, job_id, fresh)

    second, wind, runner = _recheck(coordinator, job_id, [])

    assert second == []
    assert wind.calls == []
    assert runner.calls == []
    assert store.load(job_id).excluded_years == ()


def test_recheck_admits_only_the_year_that_became_available(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    spec = _spec()
    plans = plan_members(spec)
    coordinator, job_id, _wind, _runner = _start(
        monkeypatch, store, spec, availability=lambda window: "no coverage"
    )
    assert store.load(job_id).excluded_years == (2018, 2019)

    monkeypatch.setattr(
        historicalrecheck,
        "availability_reason",
        lambda window, now=None: (
            None if window.start_utc.year < 2019 else "still gone"
        ),
    )
    fresh = [
        MemberOutcome(plan.member_id, "finished", 18.0)
        for plan in plans
        if plan.year == 2018
    ]
    returned, _wind, runner = _recheck(coordinator, job_id, fresh)

    assert {outcome.member_id for outcome in returned} == {
        plan.member_id for plan in plans if plan.year == 2018
    }
    record = store.load(job_id)
    assert len(record.members) == 3
    assert record.effective_years == (2018,)
    assert record.excluded_years == (2019,)
    assert record.years[2019].state == "excluded"
    assert record.state == "complete_with_exclusions"
    assert len(runner.calls[0][0]) == 3


def test_recheck_acquisition_failure_parks_the_job(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    _spec_value, plans, coordinator, job_id = _excluded_2018_start(monkeypatch, store)
    monkeypatch.setattr(
        historicalrecheck, "availability_reason", lambda window, now=None: None
    )
    failure = RuntimeError("wind blew")

    with pytest.raises(RuntimeError, match="wind blew"):
        coordinator.recheck_excluded(
            job_id,
            _request(),
            ensure_wind=FakeWind(error=failure),
            run_bound=FakeRunner(),
        )

    record = store.load(job_id)
    assert record.state == "needs_attention"
    assert "wind blew" in record.spec["last_error"]
    assert record.excluded_years == ()


def test_recheck_respects_the_currents_policy(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    _spec_value, _plans, coordinator, job_id = _excluded_2018_start(
        monkeypatch, store, currents=True
    )
    monkeypatch.setattr(
        historicalrecheck, "availability_reason", lambda window, now=None: None
    )

    record = store.load(job_id)
    stored_binding = dict(record.current_binding)
    record.current_binding = {}
    store.save(record)
    with pytest.raises(CoordinatorError, match="current"):
        coordinator.recheck_excluded(
            job_id,
            _request(),
            ensure_wind=FakeWind(),
            run_bound=FakeRunner(),
            tranche_factory=lambda member: object(),
        )
    assert store.load(job_id).current_binding == {}

    record = store.load(job_id)
    record.current_binding = stored_binding
    store.save(record)
    with pytest.raises(CoordinatorError, match="current selection factory"):
        coordinator.recheck_excluded(
            job_id,
            _request(),
            ensure_wind=FakeWind(),
            run_bound=FakeRunner(),
            tranche_factory=lambda member: object(),
        )


def test_recheck_refuses_archived_and_running_jobs(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    _spec_value, _plans, coordinator, job_id = _excluded_2018_start(monkeypatch, store)

    record = store.load(job_id)
    record.state = "archived"
    store.save(record)
    with pytest.raises(CoordinatorError, match="archived"):
        coordinator.recheck_excluded(
            job_id, _request(), ensure_wind=FakeWind(), run_bound=FakeRunner()
        )

    record = store.load(job_id)
    record.state = "running"
    store.save(record)
    with pytest.raises(CoordinatorError, match="already running"):
        coordinator.recheck_excluded(
            job_id, _request(), ensure_wind=FakeWind(), run_bound=FakeRunner()
        )


def test_rechecked_members_start_at_one_attempt_and_are_logged(monkeypatch, tmp_path):
    store = HistoricalRoutingStore(tmp_path / "store")
    _spec_value, plans, coordinator, job_id = _excluded_2018_start(monkeypatch, store)
    monkeypatch.setattr(
        historicalrecheck, "availability_reason", lambda window, now=None: None
    )
    fresh = [
        MemberOutcome(plan.member_id, "finished", 18.0)
        for plan in plans
        if plan.year == 2018
    ]
    _recheck(coordinator, job_id, fresh)

    record = store.load(job_id)
    new_ids = {plan.member_id for plan in plans if plan.year == 2018}
    assert {record.members[member_id].attempts for member_id in new_ids} == {1}
    entries = [
        entry for entry in record.attempt_history if entry.get("event") == "recheck"
    ]
    assert len(entries) == 1
    assert entries[0]["year"] == 2018
    assert entries[0]["previous_reason"]
    assert set(entries[0]["members"]) == new_ids
