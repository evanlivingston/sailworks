"""Tests for isochrone inspection over recorded routing results."""

from __future__ import annotations

import pytest

from sailworks.core.marine.routinginspection import RoutingInspector
from sailworks.core.marine.routingrecord import (
    Capabilities,
    Contour,
    EngineIdentity,
    Layer,
    Leg,
    Observation,
    Outcome,
    Point,
    Provenance,
    Trace,
    Validation,
)
from sailworks.core.marine.routingresult import RoutingResult, canonical_utc

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000007"


def _result(layers=(), traces=(), legs=()) -> RoutingResult:
    return RoutingResult(
        calculation_id=CALCULATION,
        generation=1,
        sequence=1,
        status="complete",
        outcome=Outcome(True, True, Validation("passed", True)),
        engine_identity=EngineIdentity(
            "rgleason/weather_routing_pi",
            "4df1b519e75e664dd72eed62dbb62b4a3e02b8d1",
            "1.2.1",
            "1.1.0",
            "a" * 64,
            None,
        ),
        provenance=Provenance("2026-01-01T00:00:00Z", "recorded"),
        legs=legs,
        layers=layers,
        capabilities=Capabilities("main", "absent", "sampled", 12, 1800),
        traces=traces,
    )


def _layer(layer_id: str, epoch_s: int) -> Layer:
    return Layer(
        layer_id,
        epoch_s,
        canonical_utc(epoch_s),
        False,
        (Contour(f"{layer_id}-c0", True, (Point(48.0, -124.0), Point(48.1, -123.9))),),
    )


def _leg(leg_id: str, readout=None) -> Leg:
    return Leg(
        leg_id,
        Point(48.2, -123.7),
        Point(48.4, -123.5),
        1767225600,
        "2026-01-01T00:00:00Z",
        1767226500,
        "2026-01-01T00:15:00Z",
        {"wind_east_kn": Observation(10.0, "kn", "reported")},
        readout,
    )


def test_layer_list_has_exact_utc_and_offsets():
    inspector = RoutingInspector(
        _result(layers=(_layer("layer-0", 1767226500), _layer("layer-1", 1767228300)))
    )
    layers = inspector.layers()
    assert [layer.layer_id for layer in layers] == ["layer-0", "layer-1"]
    assert layers[0].time_utc == "2026-01-01T00:15:00Z"
    assert layers[1].time_utc == "2026-01-01T00:45:00Z"
    assert [layer.offset_s for layer in layers] == [0, 1800]


def test_nearest_layer_ties_choose_earlier():
    inspector = RoutingInspector(
        _result(layers=(_layer("layer-0", 1767226500), _layer("layer-1", 1767228300)))
    )
    assert inspector.nearest_layer(1767227400).layer_id == "layer-0"
    assert inspector.nearest_layer(1767228300).layer_id == "layer-1"


def test_trace_inspection_returns_predecessors_and_absent_times():
    trace = Trace(
        "trace-0",
        Point(48.2, -123.6),
        (Point(48.2, -123.7), Point(48.2, -123.6)),
    )
    inspector = RoutingInspector(_result(traces=(trace,)))
    inspected = inspector.trace_at(48.21, -123.61)

    assert inspected.trace_id == "trace-0"
    assert inspected.positions == ((48.2, -123.7), (48.2, -123.6))
    assert inspected.times is None


def test_leg_readout_reports_missing_fields_not_zeros():
    readout = {
        "heading_degrees": 43.5,
        "speed_through_water_knots": 4.4,
        "warning_count": 0,
    }
    inspector = RoutingInspector(_result(legs=(_leg("leg-0", readout),)))
    values = inspector.leg_readout("leg-0")

    assert values["values"]["heading_degrees"] == 43.5
    assert values["values"]["speed_through_water_knots"] == 4.4
    assert values["values"]["warning_count"] == 0
    assert "current_speed" not in values["values"]
    assert "true_wind_speed_knots" in values["missing"]
    assert "estimated_fuel_litres" in values["missing"]


def test_leg_without_readout_reports_every_field_missing():
    inspector = RoutingInspector(_result(legs=(_leg("leg-0"),)))
    values = inspector.leg_readout("leg-0")
    assert values["values"] == {}
    assert len(values["missing"]) > 0


def test_unknown_leg_is_rejected():
    inspector = RoutingInspector(_result(legs=(_leg("leg-0"),)))
    with pytest.raises(KeyError):
        inspector.leg_readout("missing")


def _wave_result(provenance):
    from dataclasses import replace

    from sailworks.core.marine.routinginspection import diagnostics

    result = _result(legs=(_leg("leg-0"),))
    result = replace(
        result,
        outcome=Outcome(
            True,
            True,
            Validation("passed", True),
            {"wave_provenance": provenance},
        ),
    )
    return {state.code: state.state for state in diagnostics(result)}


def test_diagnostics_disclose_real_wave_source_and_proxy_separately():
    states = _wave_result(
        {
            "mode": "real_waves_and_wind_proxy",
            "uses_real_wave_heights": True,
            "uses_wind_proxy": True,
            "dataset_identity": "GFSWAVE:run:sha",
            "units": {
                "height": "m",
                "period": "s",
                "direction": "degrees coming from true north",
            },
        }
    )
    assert "GFSWAVE:run:sha" in states["wave source"]
    assert "legacy wind proxy" in states["wave source"]
    assert "height m" in states["wave units"]


def test_diagnostics_do_not_dress_the_wind_proxy_as_wave_data():
    states = _wave_result(
        {
            "mode": "wind_proxy",
            "uses_real_wave_heights": False,
            "uses_wind_proxy": True,
        }
    )
    assert "no wave heights" in states["wave source"]
    assert "wave units" not in states


_ZONE_RINGS = {
    "avoid-1": [
        [48.0, -123.4],
        [48.3, -123.4],
        [48.3, -123.0],
        [48.0, -123.0],
    ],
    "avoid-2": [
        [47.0, -122.4],
        [47.2, -122.4],
        [47.2, -122.0],
    ],
}


def _zone_descriptors():
    return [
        {"id": "avoid-1", "name": "Charted reef", "vertex_count": 4},
        {"id": "avoid-2", "name": "Second reef", "vertex_count": 3},
    ]


def _zone_fingerprints(digest=None, zones=None):
    from sailworks.core.geo.mapshapes import avoid_zones_digest_from_native

    zones = _zone_descriptors() if zones is None else zones
    payload = [
        dict(
            zone,
            vertices=[list(vertex) for vertex in _ZONE_RINGS[zone["id"]]],
        )
        for zone in zones
    ]
    if digest is None:
        digest = avoid_zones_digest_from_native(payload)
    return {
        "avoid_zones": payload,
        "avoid_zones_digest": digest,
    }


_ZONE_DIGEST = _zone_fingerprints()["avoid_zones_digest"]


def _zone_result(status="complete", **recorded):
    from dataclasses import replace

    payload = {
        "avoid_zones": _zone_descriptors(),
        "avoid_zones_digest": _ZONE_DIGEST,
        "avoid_zones_active": True,
        "input_fingerprints": _zone_fingerprints(),
    }
    payload.update(recorded)
    # A no-route result is neither accepted nor delivered; the schema rejects
    # a delivered outcome that is not also accepted.
    accepted = status == "complete"
    return replace(
        _result(legs=(_leg("leg-0"),)),
        status=status,
        outcome=Outcome(
            accepted,
            accepted,
            Validation("passed", True) if accepted else Validation("unknown", False),
            payload,
        ),
    )


def test_inspector_reports_frozen_zone_ids_and_digest_without_causal_claim():
    from sailworks.core.marine.routinginspection import diagnostics

    result = _zone_result()
    evidence = RoutingInspector(result).avoid_zones()
    assert evidence is not None
    assert evidence.active is True
    assert evidence.digest == _ZONE_DIGEST
    assert [zone.zone_id for zone in evidence.zones] == ["avoid-1", "avoid-2"]
    assert [zone.vertex_count for zone in evidence.zones] == [4, 3]
    assert evidence.consistency == ()
    assert "no reroute is claimed" in evidence.causality
    states = {state.code: state.state for state in diagnostics(result)}
    assert "avoid-1" in states["avoid zones"]
    assert "avoid-2" in states["avoid zones"]
    assert _ZONE_DIGEST in states["avoid zones"]
    assert "no reroute is claimed" in states["avoid zone effect"]
    assert "zone constraint" not in states
    assert "rerouted" not in states["avoid zones"].casefold()


def test_inspector_flags_fingerprint_digest_mismatch():
    from sailworks.core.marine.routinginspection import diagnostics

    result = _zone_result(
        input_fingerprints=_zone_fingerprints(digest="c3" * 32),
    )
    evidence = RoutingInspector(result).avoid_zones()
    assert any("digest does not match" in item for item in evidence.consistency)
    states = {state.code: state.state for state in diagnostics(result)}
    assert "avoid zone evidence" in states
    assert "does not match" in states["avoid zone evidence"]


def test_inspector_flags_near_identical_zone_sets():
    zones = _zone_descriptors()
    zones[1] = dict(zones[1], vertex_count=4)
    result = _zone_result(input_fingerprints=_zone_fingerprints(zones=zones))
    evidence = RoutingInspector(result).avoid_zones()
    assert any("zone set differs" in item for item in evidence.consistency)


def test_inspector_flags_same_count_zone_geometry_mutation():
    from sailworks.core.marine.routinginspection import diagnostics

    fingerprints = _zone_fingerprints()
    ring = fingerprints["avoid_zones"][0]["vertices"]
    ring[2] = [49.0, -122.0]
    result = _zone_result(input_fingerprints=fingerprints)
    evidence = RoutingInspector(result).avoid_zones()
    assert evidence is not None
    assert evidence.digest == _ZONE_DIGEST
    assert [zone.vertex_count for zone in evidence.zones] == [4, 3]
    assert any("geometry does not match" in item for item in evidence.consistency)
    states = {state.code: state.state for state in diagnostics(result)}
    assert "avoid zone evidence" in states
    assert "geometry does not match" in states["avoid zone evidence"]


def test_inspector_accepts_unchanged_zone_geometry():
    result = _zone_result()
    evidence = RoutingInspector(result).avoid_zones()
    assert evidence is not None
    assert evidence.consistency == ()


def test_inspector_tolerates_legacy_fingerprint_without_vertices():
    legacy = {
        "avoid_zones": _zone_descriptors(),
        "avoid_zones_digest": _ZONE_DIGEST,
    }
    result = _zone_result(input_fingerprints=legacy)
    evidence = RoutingInspector(result).avoid_zones()
    assert evidence is not None
    assert evidence.consistency == ()


def test_no_route_with_active_zones_reports_constraint_without_cause():
    from sailworks.core.marine.routinginspection import diagnostics

    result = _zone_result("no_feasible_route")
    states = {state.code: state.state for state in diagnostics(result)}
    assert "no route delivered" in states["zone constraint"]
    assert "not proven" in states["zone constraint"]
    assert "caused" not in states["zone constraint"]
    assert "no reroute is claimed" in states["avoid zone effect"]


def test_no_zone_result_reports_no_avoid_zone_states():
    from sailworks.core.marine.routinginspection import diagnostics

    result = _result(legs=(_leg("leg-0"),))
    assert RoutingInspector(result).avoid_zones() is None
    codes = {state.code for state in diagnostics(result)}
    assert not codes & {
        "avoid zones",
        "avoid zone effect",
        "zone constraint",
        "avoid zone evidence",
    }
