"""Tests for the native bridge to schema-v1 result adapter."""

from __future__ import annotations

import copy

import pytest

from sailworks.core.marine.routingnative import to_routing_result
from sailworks.core.marine.routingrecord import Observation, SchemaError
from sailworks.core.marine.routingresult import export_blockers, export_eligible

IDENTITY = {
    "source": {
        "repository": "rgleason/weather_routing_pi",
        "commit": "4df1b519e75e664dd72eed62dbb62b4a3e02b8d1",
        "tree_sha256": "a" * 64,
    },
    "versions": {"engine": "1.2.1", "engine_cmake_project": "1.1.0"},
}


def _point(latitude: float, longitude: float) -> dict:
    return {"latitude": latitude, "longitude": longitude}


def _leg(start_epoch: int, end_epoch: int, **overrides) -> dict:
    leg = {
        "start": _point(48.2, -123.7),
        "end": _point(48.24, -123.66),
        "start_time": start_epoch,
        "end_time": end_epoch,
        "wind_available": True,
        "wind_east_knots": 12.5,
        "wind_north_knots": 4.0,
        "current_available": True,
        "current_east_knots": 0.4,
        "current_north_knots": -0.15,
        "waves_available": False,
    }
    leg.update(overrides)
    return leg


def _layer(index: int, epoch_s: int, closed=(True, False), times=None) -> dict:
    return {
        "index": index,
        "id": f"isochrone-layer-{index}",
        "time": epoch_s,
        "reverse": False,
        "contours": [
            {
                "points": [
                    _point(48.0, -124.0),
                    _point(48.1, -123.9),
                    _point(48.2, -123.8),
                ],
                "point_count": 3,
                "closed": state,
            }
            for state in closed
        ],
        "traces": [
            {
                "endpoint": _point(48.2, -123.8),
                "route": [_point(48.2, -123.7), _point(48.2, -123.8)],
                "point_count": 2,
                "times": times,
            }
        ],
    }


def _run(**overrides) -> dict:
    run = {
        "engine": "main",
        "status": "complete",
        "accepted": True,
        "validation": {
            "passed": True,
            "failure_reason": "",
            "samples": 236,
        },
        "validation_pass_flags_equal": True,
        "independent_validation": {"passed": True, "samples": 120},
        "legs": [_leg(1767225600, 1767226500)],
        "layers": [_layer(0, 1767226500)],
        "requests": {},
        "request": {"time_step_seconds": 1800},
        "warnings": [],
        "inspection_geometry": {"has_trace_times": False},
    }
    run.update(overrides)
    return run


def _convert(run: dict):
    return to_routing_result(
        run,
        calculation_id="6f1c1a1e-0000-4000-8000-000000000001",
        generation=1,
        sequence=1,
        created_epoch_s=1767225600,
        identity=IDENTITY,
    )


def test_validated_run_becomes_export_eligible_record():
    result = _convert(_run())
    assert result.schema_version == 1
    assert result.status == "complete"
    assert result.outcome.accepted and result.outcome.delivered
    assert result.outcome.validation.state == "passed"
    assert result.outcome.validation.independent is True
    assert len(result.legs) == 1 and len(result.layers) == 1
    assert export_eligible(result)
    assert export_blockers(result) == ()


def test_contour_order_and_open_state_are_preserved():
    result = _convert(_run())
    layer = result.layers[0]
    assert [contour.closed for contour in layer.contours] == [True, False]
    assert [len(contour.points) for contour in layer.contours] == [3, 3]
    assert (layer.contours[0].points[0].lat, layer.contours[0].points[0].lon) == (
        48.0,
        -124.0,
    )
    # No closing vertex or extra point is invented for the open contour.
    assert len(layer.contours[1].points) == 3


def test_trace_times_pass_through_or_stay_null():
    result = _convert(_run())
    trace = result.traces[0]
    assert trace.times is None
    assert result.capabilities.trace_times == "absent"

    timed = _convert(
        _run(layers=[_layer(0, 1767226500, times=[1767225600, 1767226500])])
    )
    assert timed.traces[0].times == (1767225600, 1767226500)
    assert timed.capabilities.trace_times == "per_position"


def test_mismatched_trace_times_are_rejected_not_repaired():
    run = _run(layers=[_layer(0, 1767226500, times=[1767225600])])
    with pytest.raises(SchemaError):
        _convert(run)


def test_unavailable_environment_values_stay_unknown_not_zero():
    # Wind unavailable while current is recorded: the unknown wind components
    # must stay null rather than becoming reassuring zero knots.
    run = _run(
        legs=[
            _leg(
                1767225600,
                1767226500,
                wind_available=False,
                wind_east_knots=12.5,
                wind_north_knots=4.0,
            )
        ]
    )
    result = _convert(run)
    environment = result.legs[0].environment
    assert isinstance(environment["wind_east_kn"], Observation)
    assert environment["wind_east_kn"].availability == "unknown"
    assert environment["wind_east_kn"].value is None
    assert environment["wind_north_kn"].value is None
    assert environment["current_north_kn"].availability == "reported"
    assert environment["current_north_kn"].value == pytest.approx(-0.15)
    assert environment["waves_height_m"].availability == "unknown"
    assert result.provenance.route_environment == "recorded"


def test_leg_without_any_recorded_value_has_no_environment():
    run = _run(
        legs=[
            _leg(
                1767225600,
                1767226500,
                wind_available=False,
                current_available=False,
            )
        ]
    )
    result = _convert(run)
    assert result.legs[0].environment is None
    assert result.provenance.route_environment == "absent"


def test_independence_comes_from_pass_flags_not_sample_counts():
    run = _run()
    run["validation"]["samples"] = 999
    run["independent_validation"]["samples"] = 3
    result = _convert(run)
    assert result.outcome.validation.independent is True

    not_equal = _run(validation_pass_flags_equal=False)
    assert _convert(not_equal).outcome.validation.independent is False
    assert not export_eligible(_convert(not_equal))


def test_cancelled_run_is_unknown_and_never_export_eligible():
    run = _run(
        status="cancelled",
        accepted=False,
        validation={"passed": False, "failure_reason": "cancelled", "samples": 0},
        legs=[],
        layers=[],
    )
    result = _convert(run)
    assert result.outcome.validation.state == "unknown"
    assert result.outcome.validation.independent is False
    assert not export_eligible(result)


def test_empty_layers_are_allowed_and_disclosed():
    result = _convert(_run(layers=[]))
    assert result.layers == ()
    assert result.capabilities.trace_times == "absent"
    # Inspection geometry is not an export precondition for a validated route.
    assert export_eligible(result)


def test_missing_backend_sampling_reports_no_sample_capability():
    result = _convert(_run(validation={"passed": True, "samples": 0}))
    assert result.capabilities.validation_mode == "none"
    assert result.capabilities.sample_count is None
    assert export_eligible(result)


def test_conversion_is_deterministic_and_does_not_mutate_input():
    run = _run()
    snapshot = copy.deepcopy(run)
    first = _convert(run)
    second = _convert(run)
    assert run == snapshot
    assert first.calculation_id == second.calculation_id
    assert [leg.leg_id for leg in first.legs] == [leg.leg_id for leg in second.legs]
    assert [layer.layer_id for layer in first.layers] == [
        layer.layer_id for layer in second.layers
    ]
    assert [trace.trace_id for trace in first.traces] == [
        trace.trace_id for trace in second.traces
    ]


def test_capabilities_disclose_unsupported_and_absent_features():
    result = _convert(_run())
    features = result.capabilities.features
    assert features["weather_sampling"] == "sampled"
    assert features["inspection_geometry"] == "sampled"
    assert features["waves"] == "absent"
    assert features["shoreline"] == "unsupported"
    assert features["depth"] == "unsupported"
    assert features["chart_hazards"] == "unsupported"


def test_feature_capabilities_roundtrip_and_are_validated():
    result = _convert(_run())
    reloaded = type(result).from_json(result.to_json())
    assert dict(reloaded.capabilities.features) == dict(result.capabilities.features)

    record = result.to_native()
    record["capabilities"]["backend"]["features"] = {"shoreline": "invented"}
    with pytest.raises(SchemaError):
        type(result).from_native(record)


def test_records_without_feature_capabilities_still_parse():
    result = _convert(_run())
    record = result.to_native()
    record["capabilities"]["backend"].pop("features", None)
    reloaded = type(result).from_native(record)
    assert reloaded.capabilities.features is None


def test_leg_readout_records_engine_values_and_absence():
    run = _run(
        legs=[
            _leg(
                1767225600,
                1767226500,
                heading_degrees=43.5,
                speed_through_water_knots=4.4,
                true_wind_angle_degrees=152.6,
                propulsion_mode="sail",
                tack="starboard",
                stationary_wait=False,
                warnings=[{"code": "search_pruned", "message": "pruned"}],
                safety_margins={"wind": 3.0},
            )
        ]
    )
    result = _convert(run)
    readout = result.legs[0].readout

    assert readout["heading_degrees"] == 43.5
    assert readout["speed_through_water_knots"] == 4.4
    assert readout["propulsion_mode"] == "sail"
    assert readout["tack"] == "starboard"
    assert readout["stationary_wait"] is False
    assert readout["warning_count"] == 1
    assert readout["warning_codes"] == "search_pruned"
    assert readout["margin_wind"] == 3.0
    # Fields the bridge did not supply are absent, not zero.
    assert "estimated_fuel_litres" not in readout
    assert "wind_gust_knots" not in readout


def test_leg_readout_roundtrips_losslessly():
    run = _run(legs=[_leg(1767225600, 1767226500, heading_degrees=12.5)])
    result = _convert(run)
    reloaded = type(result).from_json(result.to_json())
    assert dict(reloaded.legs[0].readout) == dict(result.legs[0].readout)
