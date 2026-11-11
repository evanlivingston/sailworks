"""Retained routing imagery: geometry reuse, camera/DPR and fidelity."""

from __future__ import annotations

import pytest
from PySide6.QtGui import QColor, QImage

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
from sailworks.rendering.routingoverlay import RoutingOverlayModel
from sailworks.rendering.routingretained import RetainedRoutingLayer

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000013"


class FakeCamera:
    """Linear projection for headless fidelity checks."""

    def __init__(self, offset_x=0.0):
        self.offset_x = offset_x

    def convert_geographic_to_screen(self, latitude, longitude):
        return (self.offset_x + longitude * 100.0, latitude * 100.0)


def _result() -> RoutingResult:
    layer = Layer(
        "layer-0",
        1767226500,
        canonical_utc(1767226500),
        False,
        (Contour("c-0", False, (Point(0.5, 0.0), Point(0.5, 1.0))),),
    )
    trace = Trace("trace-0", Point(0.6, 1.0), (Point(0.5, 0.0), Point(0.6, 1.0)))
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
                Point(0.5, 0.0),
                Point(0.5, 1.0),
                1767225600,
                "2026-01-01T00:00:00Z",
                1767226500,
                "2026-01-01T00:15:00Z",
            ),
        ),
        layers=(layer,),
        capabilities=Capabilities("main", "absent", "sampled", 12, 1800),
        traces=(trace,),
    )


def _pixel_ink(image: QImage, x: int, y: int) -> int:
    colour = image.pixelColor(x, y)
    return colour.alpha()


def test_camera_moves_reuse_retained_geometry():
    model = RoutingOverlayModel(_result())
    layer = RetainedRoutingLayer()
    layer.set_model(model)

    for offset in range(5):
        layer.render(FakeCamera(offset_x=offset), 120, 90)

    assert layer.counters.geometry_builds == 1
    assert layer.counters.renders == 5


def test_style_change_repaints_without_rebuilding_geometry():
    model = RoutingOverlayModel(_result())
    layer = RetainedRoutingLayer(colour=QColor(0, 0, 255, 255))
    layer.set_model(model)
    first = layer.render(FakeCamera(), 120, 90).copy()

    layer.set_style(colour=QColor(255, 0, 0, 255), width=2.0)
    second = layer.render(FakeCamera(), 120, 90).copy()

    assert layer.counters.geometry_builds == 1
    assert layer.counters.style_changes == 1
    # The line is repainted in the new colour at the same coordinates.
    assert first.pixelColor(50, 50) != second.pixelColor(50, 50)
    assert second.pixelColor(50, 50).red() > 0


def test_dpr_scales_the_retained_image_and_geometry():
    model = RoutingOverlayModel(_result())
    layer = RetainedRoutingLayer(colour=QColor(0, 0, 255, 255))
    layer.set_model(model)

    image = layer.render(FakeCamera(), 120, 90, dpr=2.0)

    assert image.width() == 240
    assert image.height() == 180
    assert _pixel_ink(image, 100, 100) > 0


def test_hidden_layers_and_traces_are_not_drawn():
    result = _result()
    model = RoutingOverlayModel(result)
    layer = RetainedRoutingLayer(colour=QColor(0, 0, 255, 255))
    layer.set_model(model)
    model.set_layer_visible("layer-0", False)
    model.set_traces_visible(False)

    image = layer.render(FakeCamera(), 120, 90).copy()

    painted = sum(
        1
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() > 0
    )
    assert painted == 0
    assert layer.counters.geometry_builds == 1
