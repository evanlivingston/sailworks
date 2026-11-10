"""Viewport payload selection between native and legacy routing geometry."""

from __future__ import annotations

from types import SimpleNamespace

from sailworks.core.marine.routingrecord import (
    Capabilities,
    Contour,
    EngineIdentity,
    Layer,
    Leg,
    Outcome,
    Point,
    Provenance,
    Validation,
)
from sailworks.core.marine.routingresult import RoutingResult, canonical_utc
from sailworks.qt.viewportmap import _routing_overlay
from sailworks.rendering.routingoverlay import RoutingOverlayModel

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000005"


def _result(layer_count: int = 1) -> RoutingResult:
    layers = tuple(
        Layer(
            f"layer-{index}",
            1767226500 + 1800 * index,
            canonical_utc(1767226500 + 1800 * index),
            False,
            (
                Contour(
                    f"c-{index}",
                    True,
                    (Point(48.0, -124.0), Point(48.1, -123.9)),
                ),
            ),
        )
        for index in range(layer_count)
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
            ),
        ),
        layers=layers,
        capabilities=Capabilities("main", "absent", "sampled", 12, 1800),
    )


def _host(native=None):
    return SimpleNamespace(
        native_routing=native,
        isochrone_frame=SimpleNamespace(isochrones=(), path=(), exclusions=()),
    )


def test_native_model_takes_precedence_over_legacy_preview():
    model = RoutingOverlayModel(_result(layer_count=2))
    payload = _routing_overlay(_host(model))

    assert payload["calculation_id"] == CALCULATION
    assert [layer["id"] for layer in payload["layers"]] == ["layer-0", "layer-1"]
    assert payload["frontiers"][0]["segments"] == [[[48.0, -124.0], [48.1, -123.9]]]


def test_without_native_model_legacy_payload_is_unchanged():
    payload = _routing_overlay(_host())
    assert payload == {"frontiers": [], "path": [], "exclusions": []}


def test_hidden_native_layers_disappear_from_the_viewport_payload():
    model = RoutingOverlayModel(_result(layer_count=2))
    model.set_layer_visible("layer-0", False)
    payload = _routing_overlay(_host(model))

    assert [layer["id"] for layer in payload["layers"]] == ["layer-1"]
    assert len(payload["frontiers"]) == 1
