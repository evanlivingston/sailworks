"""Default isochrone boundary: the hull envelope, not every engine arm.

The engine exports ordered frontier chains, one per heading. Drawn as a whole
that is a starburst, so the payload strokes the layer's hull envelope by
default. The raw chains stay in ``layers[].segments``: this is a painting
choice, not a change to the calculation data. These cover the default payload,
the optional fill and outline toggles, and the honest fallback for a layer the
hull stage cannot reduce. Painting lives in QML and is not unit-tested here.
"""

from __future__ import annotations

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
    Validation,
)
from sailworks.core.marine.routingresult import RoutingResult, canonical_utc
from sailworks.rendering.routingoverlay import RoutingOverlayModel, routing_overlay_data

NOON_UTC = 1618171200
CALCULATION = "6f1c1a1e-0000-4000-8000-000000000199"


def _leg() -> Leg:
    return Leg(
        "leg-0",
        Point(48.2, -123.7),
        Point(48.4, -123.5),
        NOON_UTC,
        "2021-04-11T20:00:00Z",
        NOON_UTC + 900,
        "2021-04-11T20:15:00Z",
        {"wind_east_kn": Observation(10.0, "kn", "reported")},
    )


def _result(layers=()) -> RoutingResult:
    return RoutingResult(
        calculation_id=CALCULATION,
        generation=1,
        sequence=1,
        status="complete",
        outcome=Outcome(True, True, Validation("passed", True), {"warnings": []}),
        engine_identity=EngineIdentity(
            "rgleason/weather_routing_pi",
            "4df1b519e75e664dd72eed62dbb62b4a3e02b8d1",
            "1.2.1",
            "1.1.0",
            "a" * 64,
            None,
        ),
        provenance=Provenance("2021-04-11T00:00:00Z", "recorded"),
        legs=(_leg(),),
        layers=tuple(layers),
        capabilities=Capabilities("main", "absent", "sampled", 12, 1800),
        traces=(),
    )


def _layer(layer_id, contours, epoch_s=NOON_UTC) -> Layer:
    return Layer(layer_id, epoch_s, canonical_utc(epoch_s), False, tuple(contours))


def _model(layer) -> RoutingOverlayModel:
    return RoutingOverlayModel(_result(layers=(layer,)))


def _starburst_layer(layer_id="layer-0"):
    """Four open heading chains from one origin, as the engine exports them."""
    origin = Point(48.0, -124.0)
    return _layer(
        layer_id,
        (
            Contour("east", False, (origin, Point(48.0, -123.0), Point(48.0, -122.0))),
            Contour("north", False, (origin, Point(49.0, -124.0), Point(50.0, -124.0))),
            Contour("south", False, (origin, Point(47.0, -124.0))),
            Contour("west", False, (origin, Point(48.0, -125.0))),
        ),
    )


def _engine_points(payload) -> set:
    return {
        tuple(point)
        for segment in payload["layers"][0]["segments"]
        for point in segment
    }


def test_default_boundary_strokes_the_hull_and_keeps_the_engine_chains():
    payload = routing_overlay_data(_model(_starburst_layer()))
    frontier = payload["frontiers"][0]
    layer = payload["layers"][0]

    # Six raw arm segments become one closed four-edge envelope; the last edge
    # returns to the first vertex rather than leaving the ring open.
    assert len(layer["segments"]) == 6
    assert frontier["source"] == "hull"
    assert len(frontier["segments"]) == 4
    assert frontier["segments"][-1][1] == frontier["segments"][0][0]

    # No interior arm survives as a stroke, and no boundary vertex is invented:
    # every one of them is a point the engine recorded.
    boundary_points = {tuple(segment[0]) for segment in frontier["segments"]}
    assert (49.0, -124.0) not in boundary_points
    assert boundary_points <= _engine_points(payload)

    # Contour order, open/closed state and selection are untouched.
    assert layer["contour_count"] == 4
    assert layer["closed"] == [False, False, False, False]
    assert frontier["selected"] is False

    model = _model(_starburst_layer())
    model.set_selected_layer("layer-0")
    assert routing_overlay_data(model)["frontiers"][0]["selected"] is True


def test_contour_eye_off_still_emits_no_frontier():
    model = _model(_starburst_layer())
    model.set_contours_visible(False)

    payload = routing_overlay_data(model)
    assert payload["frontiers"] == []
    assert payload["layers"] == []


def test_fill_never_paints_the_hull_interior():
    payload = routing_overlay_data(_model(_starburst_layer()), "uniform", True)

    # The starburst arms are open engine chains, so there is no closed contour
    # to fall back to and no second layer to band. A hull interior is not a
    # proven reachable area, so the fill paints nothing while the default
    # boundary still strokes the envelope.
    assert "bands" not in payload
    assert "fills" not in payload
    assert payload["frontiers"][0]["source"] == "hull"
    assert len(payload["frontiers"][0]["segments"]) == 4


def test_optional_outlines_stroke_the_boundary_without_a_second_shape():
    model = _model(_starburst_layer())

    stroked = routing_overlay_data(model, "uniform", False, True)
    assert "fills" not in stroked
    assert len(stroked["outlines"]) == 1
    # The outline ring and the frontier boundary are the same ring, so the
    # toggle emphasises it instead of painting another shape.
    assert [segment[0] for segment in stroked["frontiers"][0]["segments"]] == (
        stroked["outlines"][0]["points"]
    )

    both = routing_overlay_data(model, "uniform", True, True)
    assert "fills" not in both
    assert "bands" not in both
    assert len(both["outlines"]) == 1


def test_retained_path_strokes_the_same_boundary_as_the_canvas():
    # The single-colour retained item reads the model, not the payload, so it
    # must apply the same precedence or the default view still shows a
    # starburst.
    model = _model(_starburst_layer())
    boundary = model.boundary_polylines
    frontier = routing_overlay_data(model)["frontiers"][0]

    assert len(boundary) == 1
    assert boundary[0].closed is True
    # The ring is closed back to its first vertex, and its vertices are the
    # same ordered envelope the canvas frontier strokes.
    assert len(boundary[0].points) == 5
    assert boundary[0].points[0] == boundary[0].points[-1]
    assert [list(point) for point in boundary[0].points[:-1]] == [
        segment[0] for segment in frontier["segments"]
    ]
    # Engine geometry, ids and picking are untouched.
    assert len(model.polylines) == 4
    assert len(model.result.layers[0].contours) == 4
    assert model.trace_ids == ()


def test_retained_path_keeps_engine_chains_when_no_hull_exists():
    flat = Contour(
        "flat",
        False,
        (Point(48.0, -125.0), Point(48.0, -124.0), Point(48.0, -123.0)),
    )
    model = _model(_layer("layer-0", (flat,)))

    # No envelope means the painter gets exactly what the engine recorded.
    assert model.boundary_polylines == model.polylines


def test_hull_less_layer_keeps_the_engine_stroke_and_gains_no_chord():
    # Collinear points enclose nothing, so the hull stage drops them. The layer
    # keeps its raw consecutive segments instead of being closed by a chord.
    flat = Contour(
        "flat",
        False,
        (Point(48.0, -125.0), Point(48.0, -124.0), Point(48.0, -123.0)),
    )
    payload = routing_overlay_data(
        _model(_layer("layer-0", (flat,))), "uniform", True, True
    )
    frontier = payload["frontiers"][0]

    assert frontier["source"] == "engine"
    assert frontier["segments"] == [
        [[48.0, -125.0], [48.0, -124.0]],
        [[48.0, -124.0], [48.0, -123.0]],
    ]
    # No envelope means no outline and no area; an open interior would leak.
    assert "outlines" not in payload
    assert "fills" not in payload
    assert "bands" not in payload


def test_antimeridian_ring_is_not_smeared_into_a_boundary():
    # A straddle widens the ring past the guard, so the hull is dropped. The
    # layer must not gain a boundary smeared across the world; the dateline
    # split already left it no drawable chain, which is the honest answer.
    crossing = Contour(
        "crossing",
        False,
        (Point(10.0, 179.0), Point(10.0, -179.0), Point(11.0, 179.0)),
    )
    payload = routing_overlay_data(_model(_layer("layer-0", (crossing,))))
    frontier = payload["frontiers"][0]

    assert frontier["source"] == "engine"
    assert frontier["segments"] == []
    assert "outlines" not in payload
    assert "fills" not in payload
