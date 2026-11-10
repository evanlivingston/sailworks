"""Tests for the retained routing geometry and picking model."""

from __future__ import annotations

import pytest

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
from sailworks.rendering.routingoverlay import RoutingOverlayModel

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000003"


def _leg(leg_id: str, start: Point, end: Point, start_s: int, end_s: int) -> Leg:
    return Leg(
        leg_id,
        start,
        end,
        start_s,
        "2026-01-01T00:00:00Z",
        end_s,
        "2026-01-01T00:15:00Z",
        {"wind_east_kn": Observation(10.0, "kn", "reported")},
    )


def _result(layers=(), traces=(), legs=None) -> RoutingResult:
    if legs is None:
        legs = (
            _leg(
                "leg-0",
                Point(48.2, -123.7),
                Point(48.4, -123.5),
                1767225600,
                1767226500,
            ),
        )
    return RoutingResult(
        calculation_id=CALCULATION,
        generation=1,
        sequence=1,
        status="complete",
        outcome=Outcome(
            True,
            True,
            Validation("passed", True),
            {"warnings": []},
        ),
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


def _layer(layer_id="layer-0", epoch_s=1767226500, contours=()):
    return Layer(layer_id, epoch_s, canonical_utc(epoch_s), False, contours)


def test_contour_order_and_open_state_are_preserved():
    closed = Contour(
        "c-closed",
        True,
        (Point(48.0, -124.0), Point(48.1, -123.9), Point(48.2, -123.8)),
    )
    open_contour = Contour("c-open", False, (Point(48.3, -123.7), Point(48.4, -123.6)))
    model = RoutingOverlayModel(
        _result(layers=(_layer(contours=(closed, open_contour)),))
    )

    contours = [p for p in model.polylines if p.kind == "contour"]
    assert [p.object_id for p in contours] == ["c-closed", "c-open"]
    assert [p.closed for p in contours] == [True, False]
    assert contours[1].points == ((48.3, -123.7), (48.4, -123.6))


def test_dateline_geometry_is_split_never_joined():
    crossing = Contour("c-dateline", False, (Point(10.0, 179.5), Point(10.0, -179.5)))
    model = RoutingOverlayModel(_result(layers=(_layer(contours=(crossing,)),)))
    chunks = [p for p in model.polylines if p.kind == "contour"]

    assert len(chunks) == 2
    assert [chunk.points for chunk in chunks] == [
        ((10.0, 179.5),),
        ((10.0, -179.5),),
    ]
    # Only the first chunk may inherit the engine closed flag.
    assert all(not chunk.closed for chunk in chunks)


def test_hidden_layer_produces_no_visible_contours():
    contour = Contour("c-0", True, (Point(48.0, -124.0), Point(48.1, -123.9)))
    model = RoutingOverlayModel(_result(layers=(_layer(contours=(contour,)),)))

    assert [p.object_id for p in model.visible_polylines()] == ["c-0"]
    model.set_layer_visible("layer-0", False)
    assert model.visible_polylines() == ()


def test_style_changes_do_not_rebuild_geometry():
    contour = Contour("c-0", True, (Point(48.0, -124.0), Point(48.1, -123.9)))
    trace = Trace(
        "t-0",
        Point(48.1, -123.9),
        (Point(48.2, -123.7), Point(48.1, -123.9)),
    )
    model = RoutingOverlayModel(
        _result(layers=(_layer(contours=(contour,)),), traces=(trace,))
    )
    digest = model.geometry_digest
    revision = model.geometry_revision

    model.set_layer_visible("layer-0", False)
    model.set_selected_layer("layer-0")
    model.set_thickness("contour", 2.0)
    model.set_thickness("trace", 1.0)
    model.set_traces_visible(False)

    assert model.geometry_digest == digest
    assert model.geometry_revision == revision
    assert model.style_revision == 5


def test_picking_returns_the_nearest_retained_trace():
    traces = (
        Trace("t-near", Point(48.10, -123.60), (Point(48.0, -123.7),)),
        Trace("t-far", Point(49.50, -122.50), (Point(49.4, -122.6),)),
    )
    model = RoutingOverlayModel(_result(traces=traces))

    assert model.pick_trace(48.11, -123.59) == "t-near"
    assert model.pick_trace(49.49, -122.51) == "t-far"
    model.set_traces_visible(False)
    assert model.pick_trace(48.11, -123.59) is None


def test_nearest_layer_ties_choose_the_earlier_layer():
    layers = (
        _layer("layer-0", 1767226500),
        _layer("layer-1", 1767228300),
    )
    model = RoutingOverlayModel(_result(layers=layers))

    assert model.nearest_layer(1767227000) == "layer-0"
    assert model.nearest_layer(1767228300) == "layer-1"
    assert model.nearest_layer(1767230000) == "layer-1"


def test_empty_geometry_and_absent_traces_are_explicit():
    model = RoutingOverlayModel(_result())
    assert model.layers == ()
    assert model.polylines == ()
    assert model.pick_trace(0.0, 0.0) is None
    assert model.nearest_layer(1767226500) is None


def test_unknown_layer_reference_is_rejected():
    model = RoutingOverlayModel(_result(layers=(_layer(),)))
    with pytest.raises(KeyError):
        model.set_layer_visible("missing", False)
    with pytest.raises(KeyError):
        model.set_selected_layer("missing")
