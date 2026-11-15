"""Tests for per-member historical execution helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sailworks.core import historicalexecution
from sailworks.core.historicalexecution import (
    MemberExecution,
    build_member_request,
    build_native_request,
    classify_outcome,
    current_hold_epoch,
    current_time_shift,
    elapsed_hours,
    historical_provenance,
    member_route_name,
)
from sailworks.core.historicalrouting import (
    CalendarAdjustment,
    HistoricalMemberPlan,
    HistoricalRoutingSpec,
)
from sailworks.core.routingjob import HorizonExceeded, RouteRequest

UTC = timezone.utc
TARGET_DEPARTURE = datetime(2019, 6, 1, 12, 0, tzinfo=UTC)


def _plan(departure=None, *, year=2019, offset_days=-3, adjustments=()):
    if departure is None:
        departure = datetime(2019, 6, 1, 8, 0, tzinfo=UTC)
    return HistoricalMemberPlan(
        member_id=f"member-{year}-{offset_days:+d}",
        year=year,
        offset_days=offset_days,
        intended_wall_datetime=datetime(2019, 6, 1, 8, 0),
        resolved_departure_utc=departure,
        calendar_adjustments=tuple(adjustments),
        coverage_start_utc=datetime(2019, 1, 1, tzinfo=UTC),
        coverage_end_utc=datetime(2020, 1, 1, tzinfo=UTC),
    )


def _request(**overrides):
    values = {
        "name": "Base route",
        "start": (0.0, 0.0),
        "destination": (1.0, 1.0),
        "departure": datetime(2020, 1, 1, 12, 0, tzinfo=UTC),
        "polar_file": "base.pol",
        "algorithm": None,
    }
    values.update(overrides)
    return RouteRequest(**values)


def _spec():
    return HistoricalRoutingSpec(
        job_id="job-1",
        created_at_utc=datetime(2019, 5, 1, tzinfo=UTC),
        selected_departure_local=datetime(2019, 6, 1, 12, 0),
        timezone_id="Europe/Oslo",
        reference_year=2019,
        years=3,
        offset_days=-3,
        max_duration_hours=48,
        region=(4.0, 58.0, 12.0, 64.0),
    )


# 1. member request construction -------------------------------------------------


def test_build_member_request_carries_member_fields_and_leaves_base_alone():
    plan = _plan(year=2021, offset_days=4)
    base = _request(max_duration_hours=None, use_currents=False, current_priority="finest")

    request = build_member_request(
        base,
        plan,
        max_duration_hours=48,
        use_currents=True,
        current_priority="coarsest",
    )

    assert request.departure == plan.resolved_departure_utc
    assert request.max_duration_hours == 48
    assert request.use_currents is True
    assert request.current_priority == "coarsest"
    assert "2021" in request.name
    assert "+4d" in request.name
    # The base request is a distinct, unmodified object.
    assert base.name == "Base route"
    assert base.departure == datetime(2020, 1, 1, 12, 0, tzinfo=UTC)
    assert base.max_duration_hours is None
    assert base.use_currents is False
    assert base.current_priority == "finest"


def test_build_member_request_rejects_invalid_horizon():
    with pytest.raises(ValueError, match=r"max_duration_hours must be 6\.\.720"):
        build_member_request(
            _request(),
            _plan(),
            max_duration_hours=5,
            use_currents=False,
            current_priority="finest",
        )


# 2. current time shift ----------------------------------------------------------


def test_current_time_shift_sign_and_zero():
    before = _plan(datetime(2019, 6, 1, 8, 0, tzinfo=UTC))
    after = _plan(datetime(2019, 6, 1, 18, 0, tzinfo=UTC))
    same = _plan(TARGET_DEPARTURE)

    assert current_time_shift(before, TARGET_DEPARTURE) == -4 * 3600.0
    assert current_time_shift(after, TARGET_DEPARTURE) == 6 * 3600.0
    assert current_time_shift(same, TARGET_DEPARTURE) == 0.0


def test_current_time_shift_requires_aware_target():
    with pytest.raises(ValueError, match="target_departure_utc must be timezone-aware"):
        current_time_shift(_plan(), datetime(2019, 6, 1, 12, 0))


# 3. hold epoch in member time ---------------------------------------------------


def test_current_hold_epoch_shifts_coverage_end_into_member_time():
    plan = _plan(datetime(2019, 6, 1, 8, 0, tzinfo=UTC))
    coverage_end = datetime(2019, 6, 5, 0, 0, tzinfo=UTC)

    expected = (coverage_end + timedelta(hours=-4)).timestamp()
    hold = current_hold_epoch(plan, TARGET_DEPARTURE, coverage_end)

    assert hold == expected
    assert isinstance(hold, float)
    assert hold == datetime(2019, 6, 4, 20, 0, tzinfo=UTC).timestamp()


# 4. outcome classification ------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "error", "expected"),
    [
        (object(), None, "finished"),
        (None, HorizonExceeded("member-1", 48.0), "no_finish"),
        (None, RuntimeError("boom"), "failed"),
        (None, ValueError("bad input"), "failed"),
    ],
)
def test_classify_outcome_matrix(result, error, expected):
    assert classify_outcome(result, error) == expected


def test_classify_outcome_rejects_ambiguous_pairs():
    with pytest.raises(ValueError):
        classify_outcome(object(), RuntimeError("boom"))
    with pytest.raises(ValueError):
        classify_outcome(None, None)


def test_generic_error_is_never_no_finish():
    assert classify_outcome(None, Exception("boom")) == "failed"


# 5. elapsed hours ---------------------------------------------------------------


def test_elapsed_hours_positive_zero_and_negative():
    plan = _plan(datetime(2019, 6, 1, 8, 0, tzinfo=UTC))

    assert elapsed_hours(plan, datetime(2019, 6, 1, 20, 0, tzinfo=UTC)) == 12.0
    assert elapsed_hours(plan, plan.resolved_departure_utc) == 0.0
    with pytest.raises(ValueError):
        elapsed_hours(plan, datetime(2019, 6, 1, 7, 0, tzinfo=UTC))


# 6. native request builder ------------------------------------------------------


@dataclass(frozen=True)
class _FrozenRequest:
    """Stand-in for the frozen NativeRequest; dataclasses.replace must work."""

    name: str
    use_currents: bool
    current_time_shift_s: float = 0.0
    current_hold_epoch_s: float | None = None


@dataclass(frozen=True)
class _Forecast:
    artifact_paths: tuple = (object(),)
    entry: object = None


def _execution(**overrides):
    values = {
        "member_id": "member-1",
        "year": 2019,
        "offset_days": -3,
        "request": _request(use_currents=True),
        "forecast": _Forecast(),
        "current_selection": SimpleNamespace(fields=(object(),), priority="finest"),
        "current_time_shift_s": -14400.0,
        "current_hold_epoch_s": 1559688000.0,
        "computation_key": "key-1",
        "binding_digest": "digest-1",
    }
    values.update(overrides)
    return MemberExecution(**values)


def test_build_native_request_sets_retiming_and_keeps_frozen_fields(monkeypatch):
    calls = {}

    def fake_freeze(request, scenarios, forecast, providers, selection, directory, *, engine):
        calls["request"] = request
        calls["engine"] = engine
        return (_FrozenRequest(name="frozen", use_currents=True),)

    monkeypatch.setattr(historicalexecution, "freeze_native_requests", fake_freeze)

    frozen = build_native_request(
        _execution(), marine_directory="/marine", providers=(), engine="professional"
    )

    assert calls["engine"] == "professional"
    assert calls["request"].use_currents is True
    assert frozen.name == "frozen"
    assert frozen.use_currents is True
    assert frozen.current_time_shift_s == -14400.0
    assert frozen.current_hold_epoch_s == 1559688000.0


def test_build_native_request_without_currents_requires_zero_and_none(monkeypatch):
    monkeypatch.setattr(
        historicalexecution,
        "freeze_native_requests",
        lambda *args, **kwargs: (_FrozenRequest(name="frozen", use_currents=False),),
    )

    execution = _execution(
        request=_request(use_currents=False),
        current_selection=None,
        current_time_shift_s=0.0,
        current_hold_epoch_s=None,
    )
    frozen = build_native_request(execution, marine_directory="/marine")
    assert frozen.current_time_shift_s == 0.0
    assert frozen.current_hold_epoch_s is None

    inconsistent = _execution(
        request=_request(use_currents=False),
        current_selection=None,
        current_time_shift_s=60.0,
    )
    with pytest.raises(ValueError, match="Inactive currents"):
        build_native_request(inconsistent, marine_directory="/marine")


def test_build_native_request_rejects_ambiguous_frozen_count(monkeypatch):
    monkeypatch.setattr(
        historicalexecution,
        "freeze_native_requests",
        lambda *args, **kwargs: (
            _FrozenRequest(name="a", use_currents=True),
            _FrozenRequest(name="b", use_currents=True),
        ),
    )

    with pytest.raises(ValueError, match="exactly one frozen native request"):
        build_native_request(_execution(), marine_directory="/marine")


# 7. provenance ------------------------------------------------------------------


def test_historical_provenance_is_json_serializable_and_complete():
    plan = _plan(
        datetime(2019, 6, 1, 8, 0, tzinfo=UTC),
        adjustments=(CalendarAdjustment(kind="dst", detail="one hour forward"),),
    )
    provenance = historical_provenance(
        plan,
        _spec(),
        binding_digest="digest-1",
        source_id="ERA5",
        artifact_hashes=("hash-a", "hash-b"),
        current_policy="finest",
    )

    payload = provenance["historical_routing"]
    assert json.dumps(provenance)

    required = {
        "schema_version",
        "job_id",
        "member_id",
        "year",
        "offset_days",
        "intended_local",
        "timezone_id",
        "resolved_utc",
        "calendar_adjustments",
        "source_id",
        "artifact_hashes",
        "current_policy",
        "max_duration_hours",
        "region",
        "requested_backend",
        "effective_backend",
        "binding_digest",
    }
    assert required <= set(payload)
    assert payload["schema_version"] == 1
    assert payload["job_id"] == "job-1"
    assert payload["resolved_utc"].endswith("Z")
    assert isinstance(payload["region"], list)
    assert payload["artifact_hashes"] == ["hash-a", "hash-b"]
    assert payload["calendar_adjustments"] == [{"kind": "dst", "detail": "one hour forward"}]
    assert payload["effective_backend"] == "native"


# 8. member route name -----------------------------------------------------------


def test_member_route_name_formats_signed_offsets():
    forward = _plan(year=2019, offset_days=3)
    backward = _plan(year=2019, offset_days=-3)

    assert member_route_name(forward) == "2019 · +3d · 2019-06-01 08:00"
    assert member_route_name(backward) == "2019 · -3d · 2019-06-01 08:00"
