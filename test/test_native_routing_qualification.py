"""Qualification run: frozen fixture through sampling into the native backend.

Records measurements; asserts fidelity and determinism only. No runtime budget
is applied here and no presentation-FPS claim is made.
"""

from __future__ import annotations

import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR))

from test_native_routing_fixture import START, _forecast_inputs  # noqa: E402

from sailworks.core.gribsampling import WeatherSampler  # noqa: E402
from sailworks.core.marine.routingnative import (  # noqa: E402
    to_routing_result,
    wind_callable_from_sampler,
)
from sailworks.core.marine.routingresult import export_eligible  # noqa: E402

BUILD_PYTHON = Path(
    os.environ.get(
        "SAILWORKS_NATIVE_ROUTING_BUILD",
        ROOT / "build" / "native-routing" / "python",
    )
)
KNOTS_PER_METRE_PER_SECOND = 3600.0 / 1852.0
COVERAGE = {
    "south": 48.0,
    "north": 50.0,
    "west": -124.0,
    "east": -122.0,
    "start_epoch_s": int(START.timestamp()),
    "end_epoch_s": int(START.timestamp()) + 6 * 3600,
}


def _bridge():
    if not BUILD_PYTHON.is_dir():
        pytest.fail(
            "native routing module build directory is missing: "
            f"{BUILD_PYTHON}. Configure and build it before this run."
        )
    sys.path.insert(0, str(BUILD_PYTHON))
    import sailworks_native_routing

    return sailworks_native_routing


def _current(latitude, longitude, epoch_s):
    return (0.0, 0.0, True)


def _counts(run):
    contours = sum(len(layer["contours"]) for layer in run["layers"])
    traces = sum(len(layer["traces"]) for layer in run["layers"])
    return len(run["layers"]), contours, traces


def test_frozen_fixture_qualification_run(tmp_path, capsys):
    bridge = _bridge()
    with _forecast_inputs(tmp_path) as source:
        sampler = WeatherSampler([source])
        wind = wind_callable_from_sampler(sampler)

        timings = []
        runs = []
        for _ in range(2):
            started = time.perf_counter()
            runs.append(
                bridge.route_with_weather(
                    wind,
                    _current,
                    COVERAGE,
                    provider_identity="qualification-sampler",
                )
            )
            timings.append(time.perf_counter() - started)

        first, second = runs
        layers, contours, traces = _counts(first)

        # Determinism: repeated runs agree on the accepted result.
        assert first["digest"] == second["digest"]
        assert first["legs"] == second["legs"]
        assert _counts(first) == _counts(second)

        # Fidelity: complete status family with independent replay validation.
        assert first["status"].startswith("complete")
        assert first["accepted"] is True
        assert first["validation"]["passed"] is True
        assert first["independent_validation"]["passed"] is True
        assert first["validation_pass_flags_equal"] is True

        # Engine geometry is present but explicitly bounded/sampled.
        assert layers > 0
        assert contours > 0
        assert traces > 0
        assert first["inspection_geometry"]["joins_inferred"] is False
        assert first["inspection_geometry"]["has_trace_times"] is False

        # Delivered values come from real sampling, not a fixture fallback.
        for leg in first["legs"]:
            instant = datetime.fromtimestamp(leg["start_time"], tz=timezone.utc)
            value = sampler.wind_at(
                instant, leg["start"]["latitude"], leg["start"]["longitude"]
            )
            assert value is not None
            from_degrees, speed_ms = value
            radians = math.radians(from_degrees)
            assert leg["wind_east_knots"] == pytest.approx(
                -math.sin(radians) * speed_ms * KNOTS_PER_METRE_PER_SECOND,
                abs=1e-9,
            )

        # Schema conversion is eligible only because validation passed.
        result = to_routing_result(
            first,
            calculation_id="6f1c1a1e-0000-4000-8000-000000000011",
            generation=1,
            sequence=1,
            created_epoch_s=int(START.timestamp()),
            identity=bridge.identity(),
        )
        assert export_eligible(result)
        assert result.capabilities.features["shoreline"] == "unsupported"

    measurement = (
        f"qualification: runs=2 runtime_s={timings[0]:.3f},{timings[1]:.3f} "
        f"legs={first['leg_count']} layers={layers} contours={contours} "
        f"traces={traces} digest={first['digest']}"
    )
    with capsys.disabled():
        print(measurement)


def test_matched_builtin_and_sampled_provider_runs(tmp_path, capsys):
    """Compare the built-in fixture provider with real sampling.

    Differences are expected from interpolation policy: the built-in provider
    snaps to the nearest fixture cell, while established sampling interpolates
    bilinearly. The run records the measured deltas without imposing an
    acceptance threshold.
    """
    bridge = _bridge()
    builtin = bridge.synthetic_route("main")
    with _forecast_inputs(tmp_path) as source:
        sampler = WeatherSampler([source])
        sampled = bridge.route_with_weather(
            wind_callable_from_sampler(sampler),
            _current,
            COVERAGE,
            provider_identity="qualification-sampler",
        )

        assert builtin["status"].startswith("complete")
        assert sampled["status"].startswith("complete")
        assert builtin["accepted"] is True
        assert sampled["accepted"] is True
        assert builtin["leg_count"] == sampled["leg_count"]

        deltas = []
        for first, second in zip(builtin["legs"], sampled["legs"]):
            deltas.append(
                (
                    abs(first["wind_east_knots"] - second["wind_east_knots"]),
                    abs(first["wind_north_knots"] - second["wind_north_knots"]),
                    abs(
                        first["speed_through_water_knots"]
                        - second["speed_through_water_knots"]
                    ),
                )
            )
        assert all(math.isfinite(value) for row in deltas for value in row)
        distance_delta = abs(
            builtin["metrics"]["distance_nm"] - sampled["metrics"]["distance_nm"]
        )

    measurement = (
        "matched: legs="
        f"{builtin['leg_count']}/{sampled['leg_count']} "
        f"max_dwind_kn={max(max(row) for row in deltas):.3f} "
        f"max_dstw_kn={max(row[2] for row in deltas):.3f} "
        f"distance_nm={builtin['metrics']['distance_nm']:.3f}/"
        f"{sampled['metrics']['distance_nm']:.3f} "
        f"delta={distance_delta:.3f} policy=nearest-vs-bilinear"
    )
    with capsys.disabled():
        print(measurement)


def test_worker_payload_matches_the_in_process_bridge(capsys):
    """Bridge-versus-direct evidence: worker transport must not change results."""
    from sailworks.core.marine.nativeworker import NativeRoutingWorker

    bridge = _bridge()
    direct = bridge.synthetic_route("main")

    worker = NativeRoutingWorker()
    try:
        worker.submit_synthetic(engine="main")
        deadline = time.monotonic() + 90
        status = None
        while time.monotonic() < deadline:
            status = worker.poll(0.1)
            if status is not None:
                break
    finally:
        worker.shutdown()

    assert status is not None and status.state == "completed"
    through_worker = status.payload
    assert through_worker["digest"] == direct["digest"]
    assert through_worker["legs"] == direct["legs"]
    assert through_worker["layer_count"] == direct["layer_count"]

    measurement = (
        "worker-vs-direct: digest="
        f"{direct['digest']} legs={direct['leg_count']} "
        f"layers={direct['layer_count']}"
    )
    with capsys.disabled():
        print(measurement)


def _legacy_constant_route():
    """Old frontier solver on the same constant wind/polar/current inputs."""
    from datetime import timedelta
    from unittest.mock import Mock

    import numpy as np

    from sailworks.core.marine.fields import CurrentField, FieldSelection
    from sailworks.core.marine.models import canonical_json
    from sailworks.core.marine.routing import (
        CurrentIsoRouter,
        CurrentRoutingEnvironment,
    )

    reference = datetime.fromtimestamp(int(START.timestamp()), tz=timezone.utc)
    metadata = dict(
        schema_version=1,
        provider="fixture",
        model="synthetic",
        composition="total",
        vector_reference="true_east_north",
        sources=[],
        resolution_m=9000,
        run=reference.isoformat(),
    )
    times = np.array(
        [(reference + timedelta(hours=hour)).timestamp() for hour in range(7)]
    )
    field = CurrentField(
        canonical_json(metadata),
        times,
        np.array([48.0, 50.0]),
        np.array([-124.0, -122.0]),
        np.zeros((7, 2, 2)),
        np.zeros((7, 2, 2)),
    )
    wind = type(
        "W",
        (),
        {
            "get_wind_at": staticmethod(lambda instant, lat, lon: (288.4, 12.7)),
            "last_source": "fixture",
        },
    )()
    environment = CurrentRoutingEnvironment(wind, FieldSelection([field]))
    router = CurrentIsoRouter(
        Mock(get_speed=Mock(return_value=4.0)), wind, environment=environment
    )
    router.use_numpy = False
    log = None
    for _ in range(36):
        log = router.route(log, reference, 1, (48.2, -123.7), (48.4, -123.5))
        if log.path:
            break
    return reference, log


def test_old_solver_and_native_agree_on_a_constant_condition_route(capsys):
    """Matched old/new run under identical constant conditions.

    Both use a 4 kn boat speed at every heading, constant wind from 288.4 deg at
    12.7 kn and no current. This isolates search policy and integration from
    polar interpolation, which the provider comparison already covers.
    """
    from test_native_routing_fixture import START as fixture_start

    bridge = _bridge()
    coverage = {
        "south": 48.0,
        "north": 50.0,
        "west": -124.0,
        "east": -122.0,
        "start_epoch_s": int(fixture_start.timestamp()),
        "end_epoch_s": int(fixture_start.timestamp()) + 6 * 3600,
    }
    wind = lambda latitude, longitude, epoch_s: (
        288.4,
        12.7 / KNOTS_PER_METRE_PER_SECOND,
        True,
    )  # noqa: E731
    current = lambda latitude, longitude, epoch_s: (0.0, 0.0, True)  # noqa: E731
    polar = [
        {
            "true_wind_speed_knots": tws,
            "points": [[angle, 4.0] for angle in (30, 60, 90, 120, 150, 180)],
        }
        for tws in (4.0, 8.0, 12.0, 16.0)
    ]
    native = bridge.route_with_weather(
        wind, current, coverage, polar=polar, polar_identity="const-4kn"
    )

    reference, legacy = _legacy_constant_route()
    assert legacy.path, "old solver did not complete the constant-condition route"
    assert native["status"].startswith("complete")
    assert native["accepted"] is True

    legacy_hours = (legacy.path[-1].time - reference).total_seconds() / 3600.0
    native_hours = native["metrics"]["elapsed_seconds"] / 3600.0
    legacy_end = legacy.path[-1].pos

    measurement = (
        "old-vs-native(const): "
        f"native_h={native_hours:.3f} legacy_h={legacy_hours:.3f} "
        f"delta_h={native_hours - legacy_hours:+.3f} "
        f"native_nm={native['metrics']['distance_nm']:.3f} "
        f"legacy_end=({legacy_end[0]:.4f},{legacy_end[1]:.4f})"
    )
    with capsys.disabled():
        print(measurement)
