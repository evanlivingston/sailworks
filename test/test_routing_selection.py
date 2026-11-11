"""One calculation-scoped selection shared by routing surfaces."""

from __future__ import annotations

import pytest

from sailworks.core.marine.routingrecord import (
    Capabilities,
    Contour,
    EngineIdentity,
    Layer,
    Leg,
    Outcome,
    Point,
    Provenance,
    Trace,
    Validation,
)
from sailworks.core.marine.routingresult import RoutingResult, canonical_utc
from sailworks.core.marine.routingselection import RoutingSelection

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000010"


def _result() -> RoutingResult:
    layers = tuple(
        Layer(
            f"layer-{index}",
            1767226500 + 1800 * index,
            canonical_utc(1767226500 + 1800 * index),
            False,
            (Contour(f"c-{index}", True, (Point(48.0, -124.0), Point(48.1, -123.9))),),
        )
        for index in range(2)
    )
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
        legs=(
            Leg(
                "leg-0",
                Point(48.2, -123.7),
                Point(48.4, -123.5),
                1767225600,
                "2026-01-01T00:00:00Z",
                1767226500,
                "2026-01-01T00:15:00Z",
                None,
                {"heading_degrees": 43.5},
            ),
        ),
        layers=layers,
        capabilities=Capabilities("main", "absent", "sampled", 12, 1800),
        traces=(Trace("trace-0", Point(48.3, -123.6), (Point(48.2, -123.7),)),),
    )


def test_selection_rejects_objects_from_another_calculation():
    selection = RoutingSelection(_result())
    with pytest.raises(KeyError):
        selection.select_layer("missing-layer")
    with pytest.raises(KeyError):
        selection.select_trace("missing-trace")
    with pytest.raises(KeyError):
        selection.select_leg("missing-leg")


def test_selection_notifies_once_per_change_and_is_shared():
    selection = RoutingSelection(_result())
    calls = []
    selection.subscribe(lambda: calls.append(selection.layer_id))

    selection.select_layer("layer-1")
    selection.select_layer("layer-1")
    assert calls == ["layer-1"]

    mirror = RoutingSelection(_result())
    mirrors = []
    mirror.subscribe(lambda: mirrors.append(mirror.leg_id))
    mirror.select_leg("leg-0")
    assert mirrors == ["leg-0"]


def test_forecast_time_change_moves_selection_without_touching_result():
    from sailworks.core.marine.routinginspection import RoutingInspector
    from sailworks.rendering.routingoverlay import (
        RoutingOverlayModel,
        routing_overlay_data,
    )

    result = _result()
    inspector = RoutingInspector(result)
    model = RoutingOverlayModel(result)
    selection = RoutingSelection(result)
    selection.select_layer(inspector.layers()[0].layer_id)
    digest = model.geometry_digest
    readout = inspector.leg_readout("leg-0")

    # Forecast time moves; selection follows the nearest layer.
    selection.select_layer(inspector.nearest_layer(1767228300).layer_id)

    assert selection.layer_id == "layer-1"
    assert model.geometry_digest == digest
    assert model.geometry_revision == 1
    assert inspector.leg_readout("leg-0") == readout
    assert result is inspector._result
    payload = routing_overlay_data(model)
    assert len(payload["frontiers"]) == 2
