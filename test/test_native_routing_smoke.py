"""Deterministic synthetic calculation and independent replay checks."""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_PYTHON = Path(
    os.environ.get(
        "SAILWORKS_NATIVE_ROUTING_BUILD",
        ROOT / "build" / "native-routing" / "python",
    )
)
KNOT_MS = 1852 / 3600


def _load_fixture_module():
    import importlib.util

    path = ROOT / "test" / "native_routing_fixtures.py"
    spec = importlib.util.spec_from_file_location("native_routing_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


expected_observables = _load_fixture_module().expected_observables


def _bridge():
    if not BUILD_PYTHON.is_dir():
        pytest.fail(f"native routing module build directory is missing: {BUILD_PYTHON}")
    sys.path.insert(0, str(BUILD_PYTHON))
    import sailworks_native_routing

    return sailworks_native_routing


def test_native_observables_match_frozen_formulas():
    bridge = _bridge()
    actual = bridge.synthetic_observables()
    expected = expected_observables()

    for key in ("wind_sw_h0", "wind_ne_h0", "wind_sw_h1_blended"):
        assert actual[key]["available"] is True
        assert actual[key]["east_knots"] == pytest.approx(
            expected[key]["u_ms"] / KNOT_MS, rel=1e-9
        )
        assert actual[key]["north_knots"] == pytest.approx(
            expected[key]["v_ms"] / KNOT_MS, rel=1e-9
        )
        assert actual[key]["speed_knots"] == pytest.approx(
            expected[key]["speed_knots"], rel=1e-9
        )
        assert actual[key]["from_degrees"] == pytest.approx(
            expected[key]["from_deg"], abs=1e-9
        )

    assert actual["pressure_sw_h0_hpa"] == pytest.approx(expected["pressure_sw_h0_hpa"])
    assert actual["pressure_ne_h0_hpa"] == pytest.approx(expected["pressure_ne_h0_hpa"])
    for key in ("current_sw_h0", "current_ne_h0", "current_ne_h3", "current_ne_h6"):
        assert actual[key]["east_ms"] == pytest.approx(
            expected[key]["east_ms"], abs=1e-9
        )
        assert actual[key]["north_ms"] == pytest.approx(
            expected[key]["north_ms"], abs=1e-9
        )
    assert actual["polar_60_8_kn"] == pytest.approx(expected["polar_60_8_kn"])
    assert actual["polar_90_12_kn"] == pytest.approx(expected["polar_90_12_kn"])
    assert actual["polar_180_16_kn"] == pytest.approx(expected["polar_180_16_kn"])


def test_two_synthetic_runs_are_deterministic_and_validated():
    bridge = _bridge()
    first = bridge.synthetic_route("main")
    second = bridge.synthetic_route("main")

    assert first["accepted"] is True
    assert first["status"].startswith("complete")
    assert first["leg_count"] > 0
    assert first["validation"]["passed"] is True
    assert first["independent_validation"]["passed"] is True
    assert first["validation_pass_flags_equal"] is True
    assert first["metrics"]["distance_nm"] > 0
    assert first["metrics"]["elapsed_seconds"] > 0
    # Authored endpoints, not solver counters: the route starts at the
    # request start and reaches the destination within tolerance.
    assert first["legs"][0]["start"]["latitude"] == pytest.approx(48.2)
    assert first["legs"][0]["start"]["longitude"] == pytest.approx(-123.7)
    assert first["legs"][-1]["end"]["latitude"] == pytest.approx(48.4, abs=0.02)
    assert first["legs"][-1]["end"]["longitude"] == pytest.approx(-123.5, abs=0.03)
    assert first["digest"] == second["digest"]
    assert first["leg_count"] == second["leg_count"]
    assert first["legs"] == second["legs"]
    assert first["validation"] == second["validation"]
    assert first["independent_validation"] == second["independent_validation"]
    assert first["metrics"] == second["metrics"]
    assert first["environment_usage"] == second["environment_usage"]
    assert first["warnings"] == second["warnings"]
    # Stage breakdown is compared for equality only; the synthetic route may
    # complete through direct-connection geometry and is not required to
    # consume a search-lattice state budget.
    assert (
        first["diagnostics"]["stage_stop_reasons"]
        == second["diagnostics"]["stage_stop_reasons"]
    )
    assert (
        first["diagnostics"]["effort_tiers_attempted"]
        == second["diagnostics"]["effort_tiers_attempted"]
    )


def test_delivered_geometry_and_time_are_known():
    bridge = _bridge()
    result = bridge.synthetic_route("main")
    legs = result["legs"]
    # Route-level and per-leg warnings share one structured array shape.
    assert isinstance(result["warnings"], list)
    for warning in result["warnings"]:
        assert set(warning) == {"code", "message"}

    assert result["request"]["time_step_seconds"] == 1800
    assert result["request"]["maximum_route_duration_seconds"] == 6 * 3600
    first = legs[0]
    assert first["start"]["latitude"] == pytest.approx(48.2)
    assert first["start"]["longitude"] == pytest.approx(-123.7)
    assert legs[-1]["end"]["latitude"] == pytest.approx(48.4, abs=0.02)
    assert legs[-1]["end"]["longitude"] == pytest.approx(-123.5, abs=0.03)
    previous = None
    for leg in legs:
        assert leg["end_time"] > leg["start_time"]
        assert leg["wind_source"]["dataset_identity"] == "native-routing-synthetic-v1"
        assert (
            leg["current_source"]["dataset_identity"] == "native-routing-synthetic-v1"
        )
        assert leg["wind_available"] is True
        assert leg["current_available"] is True
        assert leg["waves_available"] is False
        assert leg["tack"] in {"port", "starboard", "unknown"}
        assert isinstance(leg["estimated_fuel_litres"], float)
        assert isinstance(leg["warnings"], list)
        for warning in leg["warnings"]:
            assert set(warning) == {"code", "message"}
            assert isinstance(warning["code"], str)
            assert isinstance(warning["message"], str)
        assert set(leg["safety_margins"]) == {
            "wind_knots",
            "wave_metres",
            "fuel_litres",
            "motor_seconds",
        }
        assert "wind_gust_knots" in leg
        if previous is not None:
            assert leg["start_time"] == previous
        previous = leg["end_time"]


def test_native_inspection_layers_are_engine_geometry():
    bridge = _bridge()
    result = bridge.synthetic_route("main")
    layers = result["layers"]

    # The synthetic main run retains at least one engine isochrone layer.
    assert layers
    assert result["layer_count"] == len(layers)

    previous_epoch = None
    for position, layer in enumerate(layers):
        assert layer["index"] == position
        assert isinstance(layer["id"], str)
        assert isinstance(layer["time"], int)
        assert isinstance(layer["reverse"], bool)
        if previous_epoch is not None:
            assert layer["time"] >= previous_epoch
        previous_epoch = layer["time"]

        assert isinstance(layer["contours"], list)
        for contour in layer["contours"]:
            assert isinstance(contour["closed"], bool)
            assert len(contour["points"]) >= 2
            assert contour["point_count"] == len(contour["points"])
            for point in contour["points"]:
                latitude = point["latitude"]
                longitude = point["longitude"]
                assert math.isfinite(latitude) and math.isfinite(longitude)
                assert -90.0 <= latitude <= 90.0
                assert -180.0 <= longitude <= 180.0

        # Traces and their times are optional engine output: assert the
        # alignment invariant rather than a non-empty count.
        assert isinstance(layer["traces"], list)
        for trace in layer["traces"]:
            assert len(trace["route"]) >= 1
            assert trace["point_count"] == len(trace["route"])
            times = trace["times"]
            if times is None:
                continue
            assert len(times) == len(trace["route"])
            assert all(isinstance(value, int) for value in times)
            assert all(a <= b for a, b in zip(times, times[1:]))

    inspection = result["inspection_geometry"]
    assert inspection["producer"] == "main"
    assert inspection["source"] == "engine-visualization"
    assert inspection["joins_inferred"] is False
    assert isinstance(inspection["has_trace_times"], bool)

    identity = bridge.identity()
    assert identity["capabilities"]["inspection_geometry"] is True
    assert "no request flag" in identity["capabilities"]["inspection_geometry_capture"]
    assert "unknown" in identity["capabilities"]["inspection_trace_times"]


def test_independent_validator_rejects_tampered_route():
    bridge = _bridge()
    failure = bridge.synthetic_validation_failure()

    assert failure["original_route_accepted"] is True
    assert failure["tampered"] is True
    assert failure["passed"] is False
    assert failure["failure_reason"]


@pytest.mark.parametrize(
    ("mode", "layer_count", "incumbent"),
    [("standard", 0, False), ("original", 30, False), ("professional", 5, True)],
)
def test_qualified_synthetic_mode_delivery(mode, layer_count, incumbent):
    bridge = _bridge()
    result = bridge.synthetic_route(mode)
    assert result["status"] == "complete"
    assert result["accepted"] is True
    assert result["validation"]["passed"] is True
    assert result["independent_validation"]["passed"] is True
    assert result["layer_count"] == layer_count
    assert len(result["layers"]) == layer_count
    assert result["orchestration"]["incumbent_attempted"] is incumbent
    assert result["orchestration"]["incumbent_forwarded"] is incumbent
    assert result["orchestration"]["incumbent_independently_validated"] is incumbent
    assert result["orchestration"]["incumbent_status"] == (
        "complete" if incumbent else None
    )


def test_orchestration_does_not_claim_host_parity():
    bridge = _bridge()

    identity = bridge.identity()
    assert set(("standard", "original", "professional")) <= set(
        identity["capabilities"]["engines"]
    )
    assert identity["capabilities"]["professional_incumbent_wired"] is True
    assert identity["capabilities"]["professional_parity_claimed"] is False
    assert "host memory probing" in identity["capabilities"]["orchestration_limits"]


@pytest.mark.parametrize("mode", ["standard", "original", "professional"])
def test_qualified_mode_pre_cancellation(mode):
    result = _bridge().synthetic_cancelled_route(mode)
    assert result["status"] == "cancelled"
    assert result["accepted"] is False
    assert result["orchestration"]["incumbent_forwarded"] is False


def test_structured_failure_outside_coverage():
    bridge = _bridge()
    result = bridge.synthetic_invalid_destination()

    assert result["accepted"] is False
    assert result["status"] != "complete"
    assert isinstance(result["status"], str)
    assert result["leg_count"] >= 0


def test_unknown_engine_is_a_structured_python_error():
    bridge = _bridge()
    with pytest.raises(ValueError):
        bridge.synthetic_route("not-an-engine")
