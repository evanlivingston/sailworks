"""Helper routines for the #150 matched real-forecast evidence test.

Split out of ``test_native_routing_real_forecast_parity.py`` so that both files
stay readable in one pass. This module holds no ``test_`` functions and is not
collected by pytest; it is imported by the parity test.

The legacy route bound is deliberately wider than the native ETA. A legacy
route call advances the isochrones by ``min(hours * 3600, 600)`` s, so the
60-step budget is 10.0 simulated hours. The native run reaches the shared
destination in roughly 3.2 h on the same sampled weather, so a shorter legacy
budget (the previous 12 steps = 2.0 simulated hours) stopped the legacy search
before it could have arrived. The bound is an opportunity, not a parity claim:
the legacy search may still fail to arrive, and such a run is reported as an
observed limitation rather than as agreement or disagreement.

The wall-clock cap is checked after each step because a route call is not
interruptible. A step that begins before the cap can therefore finish after
it, and the reported status says so instead of implying a mid-step stop.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time as time_module
from datetime import datetime, timezone
from pathlib import Path

import pytest

TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR))

from test_native_routing_real_forecast import (  # noqa: E402
    ARTIFACT_REQUEST_COVERAGE,
    CYCLE,
    CYCLE_EPOCH_S,
    FIXTURE_DIR,
    KNOTS_PER_METRE_PER_SECOND,
    ROUTE_DESTINATION,
    ROUTE_START,
    _wind_components,
)

from sailworks.core.marine.routing import (  # noqa: E402
    CurrentIsoRouter,
    CurrentRoutingEnvironment,
)
from sailworks.core.marine.routingnative import (  # noqa: E402
    current_callable_from_selection,
    polar_rows_from_polar,
    wind_callable_from_sampler,
)
from sailworks.core.polarfiles import read_polar  # noqa: E402

POLAR_PATH = FIXTURE_DIR / "polar.csv"
ARTIFACT_SHA256 = "d7cede11957637649c718719309303767629e581a1686fab92f733e8c59809b1"
POLAR_SHA256 = "6ef24f3e72fdd2e5777da86c46f08c595bfa2563836dafdc6f122b2cf60978d9"
PROVIDER_IDENTITY = "gfs-20261026-12z-matched-parity"

# ``CurrentIsoRouter.route(log, CYCLE, 1, ...)`` advances by 600 s per step,
# the router's own one-hour cap.
LEGACY_STEP_SIMULATED_SECONDS = 600.0
# 60 steps = 10.0 simulated hours, beyond the native ETA of about 3.2 h on the
# same sampled weather, so the legacy search is not stopped early relative to
# the native arrival time.
LEGACY_MAX_STEPS = 60
LEGACY_MAX_SECONDS = 120.0
LEGACY_COVERAGE_SAMPLE_POINTS = 3


def _sha256(payload) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _polar():
    return read_polar(POLAR_PATH)


def _polar_rows():
    return polar_rows_from_polar(_polar())


def _bridge_evidence(run):
    """Deterministic subset of a bridge run: no timings, no wall clock."""
    return {
        "request": run["request"],
        "accepted": run["accepted"],
        "status": run["status"],
        "legs": [
            {
                "start": [leg["start"]["latitude"], leg["start"]["longitude"]],
                "end": [leg["end"]["latitude"], leg["end"]["longitude"]],
                "start_time": leg["start_time"],
                "end_time": leg["end_time"],
                "wind": [leg["wind_east_knots"], leg["wind_north_knots"]],
                "current": [
                    leg["current_east_knots"],
                    leg["current_north_knots"],
                ],
            }
            for leg in run["legs"]
        ],
    }


def _legacy_evidence(result):
    """Deterministic legacy path evidence, or ``None`` without a route.

    A run that did not complete a route has no path to digest, so this returns
    ``None`` rather than an empty or invented path digest.
    """
    if not result["completed"]:
        return None
    return {
        "departure": CYCLE_EPOCH_S,
        "start": list(ROUTE_START),
        "destination": list(ROUTE_DESTINATION),
        "path": [
            {
                "position": [float(point.pos[0]), float(point.pos[1])],
                "time": int(point.time.timestamp()),
            }
            for point in result["path"]
        ],
    }


def _legacy_progress_evidence(result):
    """Observed frontier progress; a bounded observation, not a route plan."""
    return {
        "status": result["status"],
        "steps": result["steps"],
        "progress": result["progress"],
    }


class _SamplerWind:
    """Legacy wind interface over the same ``WeatherSampler`` the bridge uses."""

    last_source = PROVIDER_IDENTITY

    def __init__(self, sampler):
        self.sampler = sampler

    def get_wind_at(self, instant, latitude, longitude):
        return self.sampler.wind_at(instant, latitude, longitude)


def _run_bridge(bridge, sampler, selection):
    started = time_module.perf_counter()
    run = bridge.route_with_weather(
        wind_callable_from_sampler(sampler),
        current_callable_from_selection(selection),
        ARTIFACT_REQUEST_COVERAGE,
        provider_identity=PROVIDER_IDENTITY,
        polar=_polar_rows(),
        polar_identity=f"fixture-polar.csv#sha256:{POLAR_SHA256}",
        start=ROUTE_START,
        destination=ROUTE_DESTINATION,
        departure_epoch_s=CYCLE_EPOCH_S,
    )
    return run, time_module.perf_counter() - started


def _frontier_snapshot(log):
    """Frontier size, earliest simulated time and best remaining distance."""
    if not log or not log.isochrones or not log.isochrones[-1]:
        return None
    frontier = log.isochrones[-1]
    earliest = min(frontier, key=lambda point: point.time)
    return {
        "frontier_points": len(frontier),
        "simulated_hours": round((earliest.time - CYCLE).total_seconds() / 3600.0, 4),
        "frontier_time": int(earliest.time.timestamp()),
        "best_remaining_nm": round(min(p.next_wp_dist for p in frontier), 4),
    }


def _representative_points(frontier):
    """Closest, middle and farthest frontier points, at most three."""
    ordered = sorted(frontier, key=lambda point: point.next_wp_dist)
    if len(ordered) <= LEGACY_COVERAGE_SAMPLE_POINTS:
        return ordered
    return [
        ordered[0],
        ordered[len(ordered) // 2],
        ordered[-1],
    ]


def _coverage_samples(sampler, selection, points):
    """Wind/current availability at representative frontier points."""
    samples = []
    for point in points:
        sampled_wind = sampler.wind_at(point.time, point.pos[0], point.pos[1])
        current = selection.sample(point.pos[0], point.pos[1], point.time)
        samples.append(
            {
                "position": [float(point.pos[0]), float(point.pos[1])],
                "time": int(point.time.timestamp()),
                "wind_available": sampled_wind is not None,
                "current_status": current.status,
            }
        )
    return samples


def _legacy_step_budget_hours():
    """Simulated hours the legacy step budget can advance, ignoring wall time."""
    return LEGACY_MAX_STEPS * LEGACY_STEP_SIMULATED_SECONDS / 3600.0


def _run_legacy(sampler, selection, polar):
    """Bounded legacy route on the shipped numpy frontier policy.

    ``use_numpy`` is left at its shipped default so the legacy comparison is
    the policy the router actually ships; the router may still fall back to
    its scalar candidate loop internally when the frontier clock is not
    compatible. The same sampled environment as the bridge is used.

    The simulated step budget is 10 h so the legacy search is not stopped
    before the native ETA. The wall-clock cap is checked after each completed
    step, so a step that started under the cap may finish over it; the status
    records the overrun instead of pretending the cap interrupted a step. An
    incomplete route is returned as an observed limitation, never as an
    arrival or an ETA.
    """
    wind = _SamplerWind(sampler)
    environment = CurrentRoutingEnvironment(wind, selection)
    router = CurrentIsoRouter(polar, wind, environment=environment)
    assert (
        CurrentIsoRouter.use_numpy is True
    ), "legacy comparison must use the shipped numpy frontier policy"

    started = time_module.perf_counter()
    log = None
    steps = 0
    progress = []
    wall_exceeded = False
    while steps < LEGACY_MAX_STEPS:
        log = router.route(log, CYCLE, 1, ROUTE_START, ROUTE_DESTINATION)
        steps += 1
        snapshot = _frontier_snapshot(log)
        if snapshot is not None:
            snapshot["step"] = steps
            snapshot["coverage"] = _coverage_samples(
                sampler, selection, _representative_points(log.isochrones[-1])
            )
            progress.append(snapshot)
        if log.path:
            break
        # Checked after the step: a route call is not interruptible, so the
        # cap can only be observed once the step has finished.
        if time_module.perf_counter() - started > LEGACY_MAX_SECONDS:
            wall_exceeded = True
            break
    elapsed = time_module.perf_counter() - started

    step_budget_hours = _legacy_step_budget_hours()
    completed = bool(log is not None and log.path)
    if completed:
        status = "completed"
        limitation = None
    elif wall_exceeded:
        status = (
            f"incomplete: wall-clock bound {LEGACY_MAX_SECONDS:.0f}s exceeded "
            f"after {steps} steps; elapsed {elapsed:.1f}s "
            "(cap checked after a step, which may overrun it)"
        )
        limitation = status
    else:
        status = (
            f"incomplete: no arrival within {LEGACY_MAX_STEPS} steps "
            f"({step_budget_hours:.1f} simulated hours)"
        )
        limitation = status

    # A completed path is the only route evidence; the frontier snapshot is
    # observed search state and is never reported as an arrival or an ETA.
    frontier = _frontier_snapshot(log)
    return {
        "completed": completed,
        "status": status,
        "limitation": limitation,
        "path": list(log.path) if completed else [],
        "steps": steps,
        "seconds": elapsed,
        "wall_exceeded": wall_exceeded,
        "wall_budget_seconds": LEGACY_MAX_SECONDS,
        "step_budget_hours": step_budget_hours,
        "frontier": frontier,
        "progress": progress,
        "simulated_hours": frontier["simulated_hours"] if frontier else 0.0,
    }


def _assert_bridge_legs_match_direct_sampling(run, sampler, selection):
    """Every reported bridge sample must equal a direct sampler/selection read."""
    for index, leg in enumerate(run["legs"]):
        assert leg["wind_available"] is True, f"legs[{index}] wind unavailable"
        assert leg["current_available"] is True, f"legs[{index}] current unavailable"
        instant = datetime.fromtimestamp(leg["start_time"], tz=timezone.utc)
        latitude = leg["start"]["latitude"]
        longitude = leg["start"]["longitude"]

        sampled_wind = sampler.wind_at(instant, latitude, longitude)
        assert sampled_wind is not None, f"legs[{index}] wind outside artifact"
        assert _wind_components(sampled_wind) == pytest.approx(
            (leg["wind_east_knots"], leg["wind_north_knots"]), abs=1e-9
        )

        sample = selection.sample(latitude, longitude, instant)
        assert sample.status == "available", f"legs[{index}] current unavailable"
        assert (
            sample.values[0] * KNOTS_PER_METRE_PER_SECOND,
            sample.values[1] * KNOTS_PER_METRE_PER_SECOND,
        ) == pytest.approx(
            (leg["current_east_knots"], leg["current_north_knots"]), abs=1e-9
        )

        # Both route endpoints stay inside the artifact; an unavailable end
        # would mean the route left coverage rather than a sampler mismatch.
        end_instant = datetime.fromtimestamp(leg["end_time"], tz=timezone.utc)
        assert (
            sampler.wind_at(
                end_instant, leg["end"]["latitude"], leg["end"]["longitude"]
            )
            is not None
        ), f"legs[{index}] end left GFS coverage"
        assert (
            selection.sample(
                leg["end"]["latitude"], leg["end"]["longitude"], end_instant
            ).status
            == "available"
        ), f"legs[{index}] end left current coverage"


def _current_metadata(selection):
    return selection.fields[0].manifest()
