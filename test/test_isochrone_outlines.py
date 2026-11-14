"""Hull outline payload: shape, ordering, colours, caps and precedence.

Painting lives in QML and is not unit-tested. These cover what the canvas
consumes: ``outlines`` comes from the same per-layer hull the fill uses, is
ordered largest first, and never coexists with the band strip for the layer it
covers.
"""

from __future__ import annotations

from math import cos, radians, sin

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
    Validation,
)
from sailworks.core.marine.routingresult import RoutingResult, canonical_utc
from sailworks.rendering.daynight import day_night_colour
from sailworks.rendering.isohull import cluster_cell, hull_area, layer_hulls
from sailworks.rendering.palettes import (
    ISOCHRONE_HULL_MAX_HULLS,
    ISOCHRONE_HULL_MAX_POINTS,
    ISOCHRONE_UNIFORM_COLOUR,
    gradient_colour,
)
from sailworks.rendering.routingoverlay import RoutingOverlayModel, routing_overlay_data

NOON_UTC = 1618171200
CALCULATION = "6f1c1a1e-0000-4000-8000-000000000197"
OUTLINE_KEYS = {"layer_id", "epoch_s", "points", "colour", "area"}


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


def _square(contour_id, size, closed=True, lat=48.0, lon=-124.0):
    return Contour(
        contour_id,
        closed,
        (
            Point(lat, lon),
            Point(lat, lon + size),
            Point(lat + size, lon + size),
            Point(lat + size, lon),
        ),
    )


def _layer(index, size, closed=True, epoch_s=NOON_UTC):
    return Layer(
        f"layer-{index}",
        epoch_s,
        canonical_utc(epoch_s),
        False,
        (_square(f"c{index}", size, closed=closed),),
    )


def _model(*layers):
    return RoutingOverlayModel(_result(layers=layers))


def _nested():
    """Three nested closed squares anchored at the same corner."""
    return _model(_layer(0, 0.5), _layer(1, 1.0), _layer(2, 2.0))


def test_outline_entries_carry_the_shape_the_painter_needs():
    payload = routing_overlay_data(_nested(), "uniform", False, True)

    assert "fills" not in payload
    assert len(payload["outlines"]) == 3
    for entry in payload["outlines"]:
        assert set(entry) == OUTLINE_KEYS
        assert entry["epoch_s"] == NOON_UTC
        assert len(entry["points"]) == 4
        assert all(len(point) == 2 for point in entry["points"])
        assert entry["area"] > 0.0


def test_outlines_are_ordered_by_strictly_descending_area():
    payload = routing_overlay_data(_nested(), "uniform", False, True)

    areas = [entry["area"] for entry in payload["outlines"]]
    assert areas[0] > areas[1] > areas[2]
    # Largest first, so each smaller ring covers its parent's interior.
    assert [entry["layer_id"] for entry in payload["outlines"]] == [
        "layer-2",
        "layer-1",
        "layer-0",
    ]


def test_outlines_are_absent_unless_asked_and_gated_by_the_eye():
    model = _nested()

    assert "outlines" not in routing_overlay_data(model, "uniform")

    model.set_contours_visible(False)
    hidden = routing_overlay_data(model, "uniform", False, True)
    assert "outlines" not in hidden
    assert "outlines_note" not in hidden
    assert hidden["layers"] == []


def test_outlines_and_fill_are_independent():
    model = _nested()

    stroked = routing_overlay_data(model, "uniform", False, True)
    assert stroked["outlines"]
    assert "fills" not in stroked

    filled = routing_overlay_data(model, "uniform", True, False)
    assert filled["bands"]
    assert "fills" not in filled
    assert "outlines" not in filled

    both = routing_overlay_data(model, "uniform", True, True)
    # The two toggles paint different shapes and neither suppresses the other.
    assert both["outlines"] == stroked["outlines"]
    assert both["bands"] == filled["bands"]


def test_outline_colours_follow_the_active_mode():
    single = _model(_layer(0, 1.0))

    uniform = routing_overlay_data(single, "uniform", False, True)
    assert uniform["outlines"][0]["colour"] == list(ISOCHRONE_UNIFORM_COLOUR)[:3]

    timed = routing_overlay_data(single, "time", False, True)
    assert (
        timed["outlines"][0]["colour"] == list(gradient_colour("layer_time", 0.5))[:3]
    )

    # The hull of the square spans 48..49N, 124..123W, so its centroid is the
    # point the day/night terminator is sampled at.
    night = routing_overlay_data(single, "day_night", False, True)
    assert (
        night["outlines"][0]["colour"]
        == list(day_night_colour(NOON_UTC, (48.5, -123.5)))[:3]
    )


def test_hull_cap_is_disclosed_in_the_note(monkeypatch):
    import sailworks.rendering.routingoverlay as overlay

    monkeypatch.setattr(overlay, "ISOCHRONE_HULL_MAX_HULLS", 2)
    payload = routing_overlay_data(_nested(), "uniform", False, True)

    assert len(payload["outlines"]) == 2
    assert payload["outlines_note"] == ("2 of 3 isochrone hulls drawn (largest first)")
    # The cap is an outline disclosure. A fill-only payload never paints a hull
    # interior, so it has no hull fill or hull cap note to report; its fill is
    # the sampled band strip between consecutive layers.
    filled = routing_overlay_data(_nested(), "uniform", True, False)
    assert "outlines_note" not in filled
    assert "fills_note" not in filled
    assert "fills" not in filled
    assert filled["bands"]

    # The cap does not disturb the closed-contour fallback a single layer uses
    # when no band can span a pair.
    single = routing_overlay_data(_model(_layer(0, 1.0)), "uniform", True, False)
    assert single["fills"][0]["closed"] is True
    assert "bands" not in single


def test_hull_point_cap_keeps_the_largest(monkeypatch):
    import sailworks.rendering.routingoverlay as overlay

    # The largest hull has four points; a second would exceed the budget.
    monkeypatch.setattr(overlay, "ISOCHRONE_HULL_MAX_POINTS", 5)
    payload = routing_overlay_data(_nested(), "uniform", False, True)

    assert len(payload["outlines"]) == 1
    assert payload["outlines"][0]["layer_id"] == "layer-2"
    assert payload["outlines_note"] == ("1 of 3 isochrone hulls drawn (largest first)")


def test_hull_cap_defaults_are_the_registry_values():
    assert ISOCHRONE_HULL_MAX_HULLS == 120
    assert ISOCHRONE_HULL_MAX_POINTS == 30_000


def test_starburst_arms_collapse_into_one_envelope():
    # The solver expands every heading, so one time step arrives as arms
    # radiating from a shared origin. The hull paints only the envelope.
    arms = []
    for bearing in range(0, 360, 45):
        radians_bearing = radians(bearing)
        arms.append(
            Contour(
                f"arm-{bearing}",
                False,
                tuple(
                    Point(
                        48.0 + 0.25 * step * sin(radians_bearing),
                        -124.0 + 0.25 * step * cos(radians_bearing),
                    )
                    for step in range(1, 5)
                ),
            )
        )
    layer = Layer("layer-0", NOON_UTC, canonical_utc(NOON_UTC), False, tuple(arms))
    payload = routing_overlay_data(
        RoutingOverlayModel(_result(layers=(layer,))), "uniform", True, True
    )

    assert len(payload["outlines"]) == 1
    ring = payload["outlines"][0]["points"]
    # Interior arms fall inside the envelope, so only the extremes survive.
    assert 3 <= len(ring) <= 8
    recorded = {(point.lat, point.lon) for arm in arms for point in arm.points}
    assert {tuple(point) for point in ring} <= recorded
    # Every arm is open, so the fill has no closed contour to fall back to and
    # no pair to band; the hull stays a stroke, not an asserted interior.
    assert "fills" not in payload
    assert "bands" not in payload


def test_hull_geometry_accepts_record_points_and_tuples():
    # The hull helpers are usable straight from the record's ``Point``
    # dataclass as well as from the tuple geometry the overlay builds.
    points = [Point(48.0, -124.0), Point(48.0, -123.0), Point(49.0, -123.0)]
    as_tuples = [(point.lat, point.lon) for point in points]

    assert layer_hulls(points) == layer_hulls(as_tuples)
    assert cluster_cell(points) == cluster_cell(as_tuples)
    assert hull_area(points) == hull_area(as_tuples)
    assert hull_area(points) > 0.0


def test_hulled_closed_layers_are_banded_not_hull_filled():
    payload = routing_overlay_data(_nested(), "uniform", True, False)

    # Nested closed layers are hull-able, but each consecutive pair is banded
    # and the hull interior is never painted.
    assert payload["bands"]
    assert "fills" not in payload


def test_bands_do_not_need_a_hull(monkeypatch):
    import sailworks.rendering.routingoverlay as overlay

    monkeypatch.setattr(overlay, "layer_hulls", lambda points, **kwargs: ())
    payload = routing_overlay_data(
        _model(_layer(0, 0.5), _layer(1, 1.0)), "uniform", True, False
    )

    # The gated band strip is built from the raw contours, so it does not
    # depend on the hull stage; a banded layer is not also closed-filled.
    assert payload["bands"]
    assert "fills" not in payload
    # Outlines come from the hull stage only, so with no hull there is nothing
    # to stroke; the payload says so instead of inventing a boundary.
    stroked = routing_overlay_data(
        _model(_layer(0, 0.5), _layer(1, 1.0)), "uniform", False, True
    )
    assert "outlines" not in stroked


def test_closed_contour_fill_is_the_last_tier(monkeypatch):
    import sailworks.rendering.routingoverlay as overlay

    monkeypatch.setattr(overlay, "layer_hulls", lambda points, **kwargs: ())
    payload = routing_overlay_data(_model(_layer(0, 1.0)), "uniform", True, False)

    # One layer cannot be banded (a band pairs two layers), so the closed
    # contour itself is the fill of last resort.
    assert "bands" not in payload
    assert len(payload["fills"]) == 1
    assert payload["fills"][0]["closed"] is True
    assert len(payload["fills"][0]["points"]) == 4


def test_model_owns_the_outlines_flag_like_the_fill():
    model = _model(_layer(0, 1.0))
    assert model.outlines_visible is False
    assert "outlines" not in routing_overlay_data(model, "uniform")

    revision = model.style_revision
    model.set_outlines_visible(True)
    assert model.outlines_visible is True
    assert model.style_revision == revision + 1
    assert routing_overlay_data(model, "uniform")["outlines"]
    # Outlines never imply a fill.
    assert "fills" not in routing_overlay_data(model, "uniform")

    with pytest.raises(TypeError):
        model.set_outlines_visible("yes")

    # A caller that resolved the flag elsewhere can still override this call.
    assert "outlines" not in routing_overlay_data(model, "uniform", False, False)


def test_collinear_contours_enclose_nothing_and_emit_no_outline():
    flat = Contour(
        "flat",
        True,
        (Point(48.0, -124.0), Point(48.5, -123.5), Point(49.0, -123.0)),
    )
    layer = Layer("layer-0", NOON_UTC, canonical_utc(NOON_UTC), False, (flat,))
    payload = routing_overlay_data(
        RoutingOverlayModel(_result(layers=(layer,))), "uniform", True, True
    )

    assert "outlines" not in payload
    # The closed contour still has a frontier; it just has no envelope.
    assert payload["frontiers"]
