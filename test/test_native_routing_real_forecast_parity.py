"""Matched legacy/native real-forecast evidence for issue #150.

Companion to ``test_native_routing_real_forecast.py``. The GRIB artifact, the
frozen fixture polar, the ``CurrentField`` selection and the route request are
shared, so the two solvers see the same wind, current, polar and geometry
inputs. What is measured differs per engine: every reported native bridge leg
is compared numerically with a direct ``WeatherSampler``/``FieldSelection``
read, while the legacy ``CurrentIsoRouter`` is exercised on its shipped
default (numpy frontier) policy and checked for sample availability along any
completed path. The legacy samples are therefore not shown to agree with a
direct read, and these tests do not prove the absence of sampling error. The
engines also differ in search policy and in native-only fixture constraints
and request limits, which the legacy router does not receive, and the native
run interpolates the polar table; no route outcome is asserted equal and no
full settings parity is implied.

The legacy comparison is bounded in simulated steps and wall clock. The step
budget is 10 simulated hours (60 steps of 600 s), beyond the native ETA on the
same weather, so the legacy search is not stopped early relative to the native
arrival. The 120 s wall cap is checked after a completed step, so it can be
overrun by one step and the reported status says so. When the legacy run does
not complete a route inside either bound, the test records the observed
limitation (route status, frontier progress and representative coverage
samples) instead of failing, and no ETA or path digest is invented. An
incomplete legacy route cannot establish solver-route parity, and differences
between the engines remain unattributed across policy, polar interpolation and
constraints. Wall-clock time and ETA deltas are printed as descriptive
observations only; there is no runtime or ETA acceptance threshold here, only
the requirement that the legacy step budget covers the observed native ETA.

Deterministic evidence is reduced to SHA-256 digests so a run can be compared
with another run without trusting timings. The artifact and polar digests are
pinned; the bridge route digest is checked across two in-process runs.

Helper routines live in ``native_routing_real_forecast_parity_helpers.py``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR))

from native_routing_real_forecast_parity_helpers import (  # noqa: E402
    ARTIFACT_SHA256,
    LEGACY_MAX_SECONDS,
    LEGACY_MAX_STEPS,
    LEGACY_STEP_SIMULATED_SECONDS,
    POLAR_PATH,
    POLAR_SHA256,
    _assert_bridge_legs_match_direct_sampling,
    _bridge_evidence,
    _current_metadata,
    _legacy_evidence,
    _legacy_progress_evidence,
    _polar,
    _polar_rows,
    _run_bridge,
    _run_legacy,
    _sha256,
)
from test_native_routing_real_forecast import (  # noqa: E402
    CYCLE,
    CYCLE_EPOCH_S,
    FIXTURE_DIR,
    GRIB_NAME,
    MANIFEST_NAME,
    ROUTE_DESTINATION,
    ROUTE_START,
    _bridge,
    _open_source,
    _strong_current_selection,
)

from sailworks.core.gribsampling import WeatherSampler  # noqa: E402
from sailworks.core.marine.routingnative import (  # noqa: E402
    polar_rows_from_polar,
)


def test_pinned_artifact_polar_and_shared_provider_settings():
    manifest = json.loads((FIXTURE_DIR / MANIFEST_NAME).read_text(encoding="utf-8"))
    artifact = (FIXTURE_DIR / GRIB_NAME).read_bytes()

    assert manifest["artifact"]["sha256"] == ARTIFACT_SHA256
    assert hashlib.sha256(artifact).hexdigest() == ARTIFACT_SHA256
    assert len(artifact) == manifest["artifact"]["bytes"]
    assert hashlib.sha256(POLAR_PATH.read_bytes()).hexdigest() == POLAR_SHA256

    polar = _polar()
    rows = polar_rows_from_polar(polar)
    assert len(rows) == len(polar.tws)
    # The adapter is pure: converting the same frozen file twice is stable and
    # the digest is a reproducible handle for the shared polar setting.
    assert _sha256(rows) == _sha256(_polar_rows())
    assert {len(row["points"]) for row in rows} == {len(polar.twa)}

    selection = _strong_current_selection()
    metadata = _current_metadata(selection)
    assert metadata["run"] == CYCLE.isoformat()
    assert metadata["resolution_m"] == 1000
    assert metadata["vector_reference"] == "true_east_north"
    assert metadata["composition"] == "total"

    # No network identity is consulted: hashes come from the local files only.
    assert manifest["provider"]["cycle_epoch_s"] == CYCLE_EPOCH_S
    assert manifest["consumer"]["path"] == "test/test_native_routing_real_forecast.py"


def test_bridge_legs_sample_identically_to_direct_weather_and_current(capsys):
    bridge = _bridge()
    source = _open_source()
    try:
        sampler = WeatherSampler([source])
        selection = _strong_current_selection()

        first, first_seconds = _run_bridge(bridge, sampler, selection)
        second, second_seconds = _run_bridge(bridge, sampler, selection)

        assert first["accepted"] is True, first.get("message", "")
        assert len(first["legs"]) > 0
        request = first["request"]
        assert (
            request["start"]["latitude"],
            request["start"]["longitude"],
        ) == pytest.approx(ROUTE_START, abs=1e-12)
        assert (
            request["destination"]["latitude"],
            request["destination"]["longitude"],
        ) == pytest.approx(ROUTE_DESTINATION, abs=1e-12)
        assert request["departure"] == CYCLE_EPOCH_S

        first_digest = _sha256(_bridge_evidence(first))
        second_digest = _sha256(_bridge_evidence(second))
        assert len(first_digest) == 64
        assert first_digest == second_digest, "bridge evidence is not reproducible"

        _assert_bridge_legs_match_direct_sampling(first, sampler, selection)

        with capsys.disabled():
            print(
                "matched real-forecast sampling (#150): "
                f"legs={len(first['legs'])} digest={first_digest} "
                f"artifact={ARTIFACT_SHA256[:12]} polar={POLAR_SHA256[:12]} "
                f"wall={first_seconds:.3f}s/{second_seconds:.3f}s "
                "(descriptive, no threshold)"
            )
    finally:
        source.close()


def test_matched_legacy_and_native_runs_report_outcomes_and_coverage(capsys):
    bridge = _bridge()
    source = _open_source()
    try:
        sampler = WeatherSampler([source])
        selection = _strong_current_selection()
        polar = _polar()

        native, native_seconds = _run_bridge(bridge, sampler, selection)
        legacy = _run_legacy(sampler, selection, polar)

        assert native["accepted"] is True, native.get("message", "")
        assert native["legs"]
        assert (
            native["request"]["departure"] == CYCLE_EPOCH_S
        ), "native request is not the shared departure"

        # Numeric sampling agreement is asserted only for the native legs; the
        # legacy path is checked for availability. Behind that asymmetry, the
        # native route outcome is attributable to search policy, while the
        # legacy outcome is not separated from sampling by this test.
        _assert_bridge_legs_match_direct_sampling(native, sampler, selection)

        native_digest = _sha256(_bridge_evidence(native))
        assert len(native_digest) == 64
        legacy_evidence = _legacy_evidence(legacy)
        legacy_digest = (
            _sha256(legacy_evidence) if legacy_evidence is not None else None
        )
        progress_digest = _sha256(_legacy_progress_evidence(legacy))
        assert legacy_digest is None or len(legacy_digest) == 64
        assert len(progress_digest) == 64

        if legacy["completed"]:
            assert legacy["path"]
            assert legacy_digest is not None
            # Matched request: both solvers depart the same point at the same
            # time and target the same destination.
            assert (
                legacy["path"][0].pos[0],
                legacy["path"][0].pos[1],
            ) == pytest.approx(ROUTE_START, abs=1e-12)
            assert legacy["path"][0].time == CYCLE
            assert (
                legacy["path"][-1].pos[0],
                legacy["path"][-1].pos[1],
            ) == pytest.approx(
                ROUTE_DESTINATION, abs=1e-9
            ), "legacy route did not reach the shared destination"
            for point in legacy["path"]:
                instant = point.time
                assert (
                    sampler.wind_at(instant, point.pos[0], point.pos[1]) is not None
                ), "legacy path left GFS coverage"
                assert (
                    selection.sample(point.pos[0], point.pos[1], instant).status
                    == "available"
                ), "legacy path left current coverage"
            assert legacy["limitation"] is None
        else:
            # No completed legacy route inside the bound. This is an observed
            # limitation of the bounded comparison, not a parity result: there
            # is no path, ETA or path digest to compare, so none is invented.
            assert legacy["limitation"] is not None, "incomplete run is undocumented"
            assert legacy["path"] == []
            assert legacy_digest is None
            assert legacy["steps"] > 0, "legacy comparison ran no step"
            assert legacy["progress"], "no legacy frontier progress recorded"
            assert all(
                record["frontier_points"] > 0 and record["best_remaining_nm"] >= 0
                for record in legacy["progress"]
            )
            assert (
                legacy["progress"][-1]["simulated_hours"] > 0.0
            ), "legacy frontier did not advance in simulated time"
            first_coverage = legacy["progress"][0]["coverage"]
            assert first_coverage, "no legacy coverage samples recorded"
            assert first_coverage[0]["wind_available"], "departure outside GFS wind"
            assert (
                first_coverage[0]["current_status"] == "available"
            ), "departure outside current coverage"

        coverage_samples = [
            sample for record in legacy["progress"] for sample in record["coverage"]
        ]
        coverage_available = sum(
            1
            for sample in coverage_samples
            if sample["wind_available"] and sample["current_status"] == "available"
        )
        native_hours = native["metrics"]["elapsed_seconds"] / 3600.0
        last = legacy["progress"][-1] if legacy["progress"] else None

        # Budget fairness, not route parity: the legacy search was given at
        # least as much simulated time as the native run used to arrive.
        assert legacy["step_budget_hours"] >= native_hours, (
            "legacy step budget does not cover the native ETA: "
            f"{legacy['step_budget_hours']:.2f}h < {native_hours:.2f}h"
        )
        assert legacy["wall_budget_seconds"] == LEGACY_MAX_SECONDS
        assert legacy["step_budget_hours"] == pytest.approx(
            LEGACY_MAX_STEPS * LEGACY_STEP_SIMULATED_SECONDS / 3600.0
        )
        if not legacy["completed"]:
            # A wall-capped run must say so and may have overrun the cap by the
            # step that was already running when the cap was exceeded.
            assert not legacy["wall_exceeded"] or "wall-clock" in legacy["status"]

        # Equality of ETA, leg count or end point is deliberately not asserted:
        # the legacy default isochrone search and the native engine differ in
        # policy, polar interpolation and constraints, so no route difference
        # is attributed to a single cause.
        with capsys.disabled():
            budget = (
                f"legacy_step_budget_h={legacy['step_budget_hours']:.1f} "
                f"legacy_wall_budget_s={legacy['wall_budget_seconds']:.0f}"
            )
            if legacy["completed"]:
                legacy_hours = (
                    legacy["path"][-1].time - CYCLE
                ).total_seconds() / 3600.0
                print(
                    "matched real-forecast route comparison (#150): "
                    "legacy_policy=numpy-frontier(default) "
                    f"native_legs={len(native['legs'])} "
                    f"native_h={native_hours:.3f} "
                    f"legacy_route_h={legacy_hours:.3f} "
                    f"legacy_steps={legacy['steps']} "
                    f"legacy_points={len(legacy['path'])} "
                    f"{budget} "
                    f"legacy_coverage={coverage_available}/{len(coverage_samples)} "
                    f"native_digest={native_digest} "
                    f"legacy_path_digest={legacy_digest} "
                    f"wall=native:{native_seconds:.3f}s,legacy:{legacy['seconds']:.3f}s "
                    "(descriptive; route/ETA equality not asserted, difference "
                    "not attributed to sampling)"
                )
            else:
                frontier_points = last["frontier_points"] if last else 0
                frontier_hours = last["simulated_hours"] if last else 0.0
                best_remaining = last["best_remaining_nm"] if last else None
                print(
                    "matched real-forecast route comparison (#150): "
                    "legacy_policy=numpy-frontier(default) "
                    f"native_legs={len(native['legs'])} "
                    f"native_h={native_hours:.3f} "
                    "legacy_route=not-completed "
                    f"legacy_status={legacy['status']!r} "
                    "legacy_path_digest=not-available(incomplete) "
                    f"legacy_steps={legacy['steps']} "
                    f"{budget} "
                    f"legacy_wall_exceeded={legacy['wall_exceeded']} "
                    f"legacy_frontier_points={frontier_points} "
                    f"legacy_frontier_h={frontier_hours:.3f} "
                    f"legacy_best_remaining_nm={best_remaining} "
                    f"legacy_coverage={coverage_available}/{len(coverage_samples)} "
                    f"native_digest={native_digest} "
                    f"legacy_progress_digest={progress_digest} "
                    f"wall=native:{native_seconds:.3f}s,legacy:{legacy['seconds']:.3f}s "
                    "(incomplete legacy route cannot establish solver-route parity; "
                    "differences unattributed across policy, polar and constraints)"
                )
    finally:
        source.close()
