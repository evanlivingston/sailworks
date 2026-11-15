"""Bounded passage horizon unit tests (no GRIB fixtures required)."""

from datetime import datetime, timedelta, timezone

import pytest

from sailworks.core.routingjob import (
    MAX_PASSAGE_HOURS,
    MIN_PASSAGE_HOURS,
    PASSAGE_STEP_HOURS,
    HorizonExceeded,
    RouteRequest,
    exceeds_horizon,
    horizon_deadline,
    run_requested_routing,
)

NAIVE = datetime(2024, 6, 1, 12, 0)
AWARE = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


def make_request(**overrides):
    fields = dict(
        name="horizon",
        start=(0.0, 0.0),
        destination=(1.0, 1.0),
        departure=NAIVE,
        polar_file="polar.csv",
        algorithm=object(),
    )
    fields.update(overrides)
    return RouteRequest(**fields)


def test_constants_match_documented_range():
    assert MIN_PASSAGE_HOURS == 6
    assert MAX_PASSAGE_HOURS == 720
    assert PASSAGE_STEP_HOURS == 6


def test_unbounded_request_has_no_deadline():
    assert make_request().max_duration_hours is None
    assert horizon_deadline(make_request()) is None


@pytest.mark.parametrize(
    "departure, hours",
    [(NAIVE, 6), (NAIVE, 720), (AWARE, 6), (AWARE, 720)],
)
def test_deadline_adds_hours_and_matches_awareness(departure, hours):
    request = make_request(departure=departure, max_duration_hours=hours)
    result = horizon_deadline(request)
    assert result == departure + timedelta(hours=hours)
    assert (result.tzinfo is None) == (departure.tzinfo is None)


@pytest.mark.parametrize("hours", [0, 5, 11, 719, 721, -6, 1440])
def test_deadline_rejects_out_of_range_or_off_step(hours):
    request = make_request(max_duration_hours=hours)
    with pytest.raises(ValueError):
        horizon_deadline(request)


def test_deadline_accepts_every_documented_step():
    for hours in range(MIN_PASSAGE_HOURS, MAX_PASSAGE_HOURS + 1, PASSAGE_STEP_HOURS):
        assert horizon_deadline(make_request(max_duration_hours=hours)) == (
            NAIVE + timedelta(hours=hours)
        )


def test_exceeds_horizon_boundary():
    deadline = NAIVE + timedelta(hours=6)
    assert exceeds_horizon(deadline, deadline) is False
    assert exceeds_horizon(deadline, deadline - timedelta(hours=1)) is False
    assert exceeds_horizon(deadline, deadline + timedelta(microseconds=1)) is True
    assert exceeds_horizon(None, deadline + timedelta(days=365)) is False


def test_horizon_exceeded_defaults_and_attributes():
    plain = HorizonExceeded()
    assert plain.member_id is None
    assert plain.elapsed_hours == 0.0
    assert isinstance(plain, Exception)

    detailed = HorizonExceeded("member-7", 18.5)
    assert detailed.member_id == "member-7"
    assert detailed.elapsed_hours == 18.5
    assert "member-7" in str(detailed)


def test_route_request_keeps_existing_field_combinations():
    minimal = make_request()
    assert minimal.max_duration_hours is None
    assert minimal.roundings == ()
    assert minimal.map_shapes == ()

    bounded = make_request(
        max_duration_hours=12,
        coastline_checks=False,
        roundings=(None,),
        polar_efficiency=0.9,
    )
    assert bounded.max_duration_hours == 12
    assert bounded.coastline_checks is False
    assert bounded.land_avoidance is False


class _FakeGrib:
    def get_wind_at(self, instant, lat, lon):
        return object()


class _FakeWind:
    def __init__(self):
        self.calls = 0

    def check_cancelled(self):
        self.calls += 1

    def close(self):
        pass


class _StepReached(Exception):
    """Sentinel proving the loop advanced past the horizon check."""


class _FakeRouting:
    def __init__(self, start, step_to=None):
        self.grib = _FakeGrib()
        self.position = (0.0, 0.0)
        self.time = start
        self.end = False
        self.steps = 0
        self._step_to = step_to

    def step(self):
        self.steps += 1
        if self._step_to is not None:
            self.time = self._step_to
            self.end = True
        raise _StepReached


def test_loop_raises_when_solver_time_passes_deadline():
    request = make_request(max_duration_hours=6)
    routing = _FakeRouting(NAIVE + timedelta(hours=12))
    with pytest.raises(HorizonExceeded) as caught:
        run_requested_routing(
            None, routing, request, _FakeWind(), lambda *a: None, lambda m: None, None
        )
    assert caught.value.elapsed_hours == pytest.approx(12.0)
    assert routing.steps == 0


def test_loop_without_horizon_ignores_late_solver_time():
    request = make_request()
    routing = _FakeRouting(NAIVE + timedelta(days=30))
    with pytest.raises(_StepReached):
        run_requested_routing(
            None, routing, request, _FakeWind(), lambda *a: None, lambda m: None, None
        )
    assert routing.steps == 1
