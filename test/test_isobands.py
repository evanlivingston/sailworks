"""Band-strip fill geometry and payload: the fill that does not need closed contours.

Painting lives in QML and is not unit-tested; these cover the pure geometry, the
payload the canvas consumes, and the model flags that drive it.
"""

from __future__ import annotations

from math import cos, radians, sin
from types import SimpleNamespace

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
from sailworks.rendering.isobands import (
    align_directions,
    band_colour,
    band_polygon,
    band_polygons,
    build_bands,
    pair_contours,
    resample,
)
from sailworks.rendering.palettes import (
    ISOCHRONE_BAND_SAMPLES,
    ISOCHRONE_FILL_ALPHA,
    ISOCHRONE_UNIFORM_COLOUR,
    gradient_colour,
)
from sailworks.rendering.routingoverlay import (
    RoutingOverlayModel,
    isochrone_contours_on_canvas,
    routing_overlay_data,
)
from sailworks.qt.viewportmap import _routing_overlay

ALPHA = ISOCHRONE_FILL_ALPHA
EARLIER = 1618171200
LATER = EARLIER + 7200
CALCULATION = "6f1c1a1e-0000-4000-8000-000000000198"


def _leg():
    return Leg(
        "leg-0",
        Point(48.2, -123.7),
        Point(48.4, -123.5),
        EARLIER,
        canonical_utc(EARLIER),
        LATER,
        canonical_utc(LATER),
        {"wind_east_kn": Observation(10.0, "kn", "reported")},
    )


def _result(layers) -> RoutingResult:
    return RoutingResult(
        calculation_id=CALCULATION,
        generation=1,
        sequence=1,
        status="complete",
        outcome=Outcome(True, True, Validation("passed", True)),
        engine_identity=EngineIdentity(
            "engine", "rev", "1.2.1", "1.1.0", "a" * 64, None
        ),
        provenance=Provenance(canonical_utc(EARLIER), "recorded"),
        legs=(_leg(),),
        layers=tuple(layers),
        capabilities=Capabilities("main", "absent", "sampled", 12, 1800),
        traces=(),
    )


def _layer(layer_id, epoch_s, contours):
    return Layer(layer_id, epoch_s, canonical_utc(epoch_s), False, contours)


def _model(layers):
    return RoutingOverlayModel(_result(layers))


@pytest.fixture
def no_hulls(monkeypatch):
    """Force the band tier for layers that would otherwise produce a hull."""
    import sailworks.rendering.routingoverlay as overlay

    monkeypatch.setattr(overlay, "layer_hulls", lambda points, **kwargs: ())


def _open(contour_id, size, lat=48.0, lon=-124.0):
    """An engine contour with no closed flag: three sides of a square."""
    return Contour(
        contour_id,
        False,
        (
            Point(lat, lon),
            Point(lat, lon + size),
            Point(lat + size, lon + size),
        ),
    )


def _square(contour_id, size, lat=48.0, lon=-124.0):
    return Contour(
        contour_id,
        True,
        (
            Point(lat, lon),
            Point(lat, lon + size),
            Point(lat + size, lon + size),
            Point(lat + size, lon),
        ),
    )


def _collinear_points(lat, lon=-124.0):
    """A straight, densely sampled frontier with no enclosing hull."""
    return [(lat, lon + 0.05 * index) for index in range(21)]


def _collinear(contour_id, lat, lon=-124.0):
    """A hull-less layer: collinear points the hull stage cannot reduce."""
    return Contour(
        contour_id,
        False,
        tuple(
            Point(point_lat, point_lon)
            for point_lat, point_lon in _collinear_points(lat, lon)
        ),
    )


def _signed_area(points):
    total = 0.0
    for index, (lat1, lon1) in enumerate(points):
        lat2, lon2 = points[(index + 1) % len(points)]
        total += lon1 * lat2 - lon2 * lat1
    return total / 2.0


def _centroid(points):
    count = len(points)
    return (
        sum(point[0] for point in points) / count,
        sum(point[1] for point in points) / count,
    )


def _line(count, lat, lon0=-124.0, step=0.1):
    """A straight, densely sampled frontier."""
    return [(lat, lon0 + step * index) for index in range(count)]


def _lat_shift(points, changes):
    """``points`` with the listed indices moved to another latitude."""
    return [
        (changes.get(index, point[0]), point[1]) for index, point in enumerate(points)
    ]


def _arc(radius, count=121, bump=None):
    """A densely sampled semicircular front, optionally bulged over a run."""
    points = []
    for index in range(count):
        angle = radians(200.0 + 140.0 * index / (count - 1))
        offset = bump[2] if bump is not None and bump[0] <= index <= bump[1] else 0.0
        span = radius + offset
        points.append((48.0 + span * sin(angle), -123.0 + span * cos(angle)))
    return points


def _starburst(lat=48.0, lon=-124.0, arm=0.6):
    """Candidate arms radiating from one point: the wrong-front shape."""
    points = [(lat, lon)]
    for delta_lat, delta_lon in (
        (arm, arm),
        (-arm, arm),
        (-arm, -arm),
        (arm, -arm),
        (1.5 * arm, 0.0),
        (0.0, 1.5 * arm),
    ):
        points.extend([(lat + delta_lat, lon + delta_lon), (lat, lon)])
    return points


# -- pure geometry ------------------------------------------------------------


def test_resample_preserves_endpoints_and_count():
    result = resample([(0.0, 0.0), (0.0, 1.0)], 5)
    assert len(result) == 5
    assert result[0] == (0.0, 0.0)
    assert result[-1] == (0.0, 1.0)
    for index, point in enumerate(result):
        assert point[1] == pytest.approx(index / 4)

    # A two-point line is the minimum a band edge can be.
    assert len(resample([(1.0, 2.0), (3.0, 4.0)], 2)) == 2


def test_resample_rejects_an_arc_it_cannot_define():
    assert resample([(0.0, 0.0)], 4) == []
    assert resample([], 4) == []


def test_resample_handles_a_zero_length_line():
    result = resample([(1.0, 2.0), (1.0, 2.0)], 3)
    assert result == [(1.0, 2.0)] * 3


def test_align_directions_reverses_when_ends_are_closer_reversed():
    a = [(0.0, 0.0), (0.0, 1.0)]
    same = [(1.0, 0.0), (1.0, 1.0)]
    assert align_directions(a, same) == same

    backward = [(1.0, 1.0), (1.0, 0.0)]
    assert align_directions(a, backward) == list(reversed(backward))


def test_band_polygon_closes_a_quad_strip():
    ring = band_polygon([(0.0, 0.0), (0.0, 1.0)], [(1.0, 0.0), (1.0, 1.0)])
    assert ring[0] == ring[-1] == (0.0, 0.0)
    assert len(ring) == 5
    # Walked forward then backward, the strip is one simple quad.
    assert abs(_signed_area(ring)) == pytest.approx(1.0)


def test_pairing_is_one_to_one_and_nearest():
    prev = [[(0.0, 0.0), (0.0, 1.0)], [(10.0, 0.0), (10.0, 1.0)]]
    nxt = [[(10.1, 0.0), (10.1, 1.0)], [(0.1, 0.0), (0.1, 1.0)]]
    assert pair_contours(prev, nxt) == [(0, 1), (1, 0)]

    # A surplus contour on one side is skipped rather than guessed.
    extra = prev + [[(20.0, 0.0), (20.0, 1.0)]]
    assert pair_contours(extra, nxt) == [(0, 1), (1, 0)]


def test_mismatched_starburst_and_arc_produce_no_band():
    layers = [
        {"layer_id": "a", "epoch_s": 100, "contours": [_starburst()]},
        {"layer_id": "b", "epoch_s": 200, "contours": [_arc(2.0)]},
    ]
    built = build_bands(layers)
    assert built["bands"] == []
    assert built["paired"] == 0
    # The front existed on both sides but the pair was refused, so the caller
    # can say so instead of leaving an unexplained gap.
    assert built["skipped"] == 1
    assert pair_contours(layers[0]["contours"], layers[1]["contours"]) == []


def test_fronts_of_similar_length_that_run_across_each_other_are_skipped():
    across = [_line(81, 48.0, lon0=-125.0)]
    along = [[(46.5 + 0.05 * index, -123.0) for index in range(71)]]
    # Extents meet and the arc lengths are comparable, so only the aligned
    # point distance can tell that these are different fronts.
    assert pair_contours(across, along) == []
    built = build_bands(
        [
            {"layer_id": "a", "epoch_s": 100, "contours": across},
            {"layer_id": "b", "epoch_s": 200, "contours": along},
        ]
    )
    assert built["bands"] == []
    assert built["skipped"] == 1


def test_matched_dense_fronts_still_produce_a_band():
    layers = [
        {"layer_id": "a", "epoch_s": 100, "contours": [_arc(2.0)]},
        {"layer_id": "b", "epoch_s": 200, "contours": [_arc(2.1)]},
    ]
    built = build_bands(layers)
    assert built["paired"] == 1
    assert built["skipped"] == 0
    band = built["bands"][0]
    assert band["layer_id"] == "b"
    assert band["prev_layer_id"] == "a"
    assert len(band["points"]) == 2 * ISOCHRONE_BAND_SAMPLES + 1


def test_gap_in_the_middle_splits_the_band_into_two_polygons():
    base = _line(21, 48.0)
    gap = _lat_shift(base, {index: 48.5 for index in range(9, 13)})
    rings = band_polygons(base, gap, 0.1)
    assert len(rings) == 2
    for ring in rings:
        # Each run stops at the detour instead of stretching across it.
        assert ring[0] == ring[-1]
        assert all(point[0] < 48.4 for point in ring)
        assert len(ring) < 2 * len(base) + 1


def test_pair_with_too_few_good_quads_is_dropped_entirely():
    base = _line(21, 48.0)
    mostly_off = _lat_shift(base, {index: 48.35 for index in range(13)})
    # 13 of 20 quads stretch past the edge limit: not a band with a gap, a
    # failed correspondence, so nothing is drawn.
    assert band_polygons(base, mostly_off, 0.1) == []


def test_spike_quad_is_rejected_and_breaks_the_strip():
    base = _line(21, 48.0)
    spike = _lat_shift(base, {15: 48.9})
    rings = band_polygons(base, spike, 0.1)
    assert len(rings) == 2
    assert all(point[0] < 48.5 for ring in rings for point in ring)


def test_build_bands_carries_layer_identity_and_no_band_for_one_layer():
    layers = [
        {"layer_id": "a", "epoch_s": 100, "contours": [[(0.0, 0.0), (0.0, 1.0)]]},
        {"layer_id": "b", "epoch_s": 200, "contours": [[(1.0, 0.0), (1.0, 1.0)]]},
    ]
    built = build_bands(layers)
    assert built["paired"] == 1
    assert built["capped"] is False
    band = built["bands"][0]
    assert band["layer_id"] == "b"
    assert band["prev_layer_id"] == "a"
    assert band["epoch_s"] == 200
    assert band["prev_epoch_s"] == 100
    assert len(band["points"]) == 2 * ISOCHRONE_BAND_SAMPLES + 1

    assert build_bands(layers[:1]) == {
        "bands": [],
        "paired": 0,
        "rings": 0,
        "capped_rings": 0,
        "skipped": 0,
        "capped": False,
    }


def test_capped_counts_refused_rings_not_pairs(monkeypatch):
    import sailworks.rendering.isobands as isobands

    ring = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0)]
    # A split pair yields two rings; the cap applies per ring, so two pairs can
    # out-produce their pair count and the disclosure must count the rings.
    monkeypatch.setattr(isobands, "band_polygons", lambda a, b, step: [ring, ring])
    layers = [
        {
            "layer_id": name,
            "epoch_s": 100 * index,
            "contours": [[(0.0, 0.0), (0.0, 1.0)]],
        }
        for index, name in enumerate(("a", "b", "c"))
    ]

    built = build_bands(layers, max_bands=3)
    assert built["paired"] == 2
    assert built["rings"] == 4
    assert len(built["bands"]) == 3
    assert built["capped_rings"] == 1
    assert built["capped"] is True

    # Paired is smaller than the ring count with no budget refusal: the old
    # pair-count test read that as capped, the ring count does not.
    whole = build_bands(layers[:2])
    assert whole["paired"] == 1
    assert whole["rings"] == 2
    assert len(whole["bands"]) == 2
    assert whole["capped_rings"] == 0
    assert whole["capped"] is False


def test_band_caps_keep_the_first_band_and_disclose():
    layers = [
        {
            "layer_id": "a",
            "epoch_s": 100,
            "contours": [[(0.0, float(i)), (0.0, float(i) + 1.0)] for i in range(3)],
        },
        {
            "layer_id": "b",
            "epoch_s": 200,
            "contours": [[(1.0, float(i)), (1.0, float(i) + 1.0)] for i in range(3)],
        },
    ]
    capped = build_bands(layers, max_bands=1)
    assert len(capped["bands"]) == 1
    assert capped["paired"] == 3
    assert capped["capped"] is True

    tight = build_bands(layers, max_points=1)
    assert len(tight["bands"]) == 1
    assert tight["capped"] is True

    assert build_bands(layers)["capped"] is False


def test_day_night_band_colour_uses_the_time_midpoint():
    band = {
        "points": [(48.0, -124.0), (48.0, -124.0)],
        "layer_id": "layer-1",
        "epoch_s": LATER,
        "prev_epoch_s": EARLIER,
    }
    expected = list(day_night_colour((EARLIER + LATER) // 2, (48.0, -124.0)))[:3]
    assert band_colour(band, "day_night", {}) == expected + [ALPHA]


# -- payload ------------------------------------------------------------------


def test_open_contours_produce_band_polygons_between_layers(no_hulls):
    earlier = _layer("layer-0", EARLIER, (_open("a", 0.5),))
    later = _layer("layer-1", LATER, (_open("b", 1.0),))
    payload = routing_overlay_data(_model([earlier, later]), "uniform", True)

    # The regression: no contour is closed, so no closed fill exists at all.
    assert "fills" not in payload
    assert len(payload["bands"]) == 1
    band = payload["bands"][0]
    assert band["layer_id"] == "layer-1"
    assert band["prev_layer_id"] == "layer-0"
    assert band["points"][0] == band["points"][-1]
    assert len(band["points"]) == 2 * ISOCHRONE_BAND_SAMPLES + 1
    assert band["colour"] == list(ISOCHRONE_UNIFORM_COLOUR) + [ALPHA]
    # The frontiers are still drawn; the band sits under them.
    assert len(payload["frontiers"]) == 2


def test_bands_appear_when_no_hull_can_be_built(no_hulls):
    earlier = _layer("layer-0", EARLIER, (_open("a", 0.5),))
    later = _layer("layer-1", LATER, (_open("b", 1.0),))
    payload = routing_overlay_data(_model([earlier, later]), "uniform", True)

    # With the hull stage unable to reduce these layers, the gated band strip
    # is the fill, and the layers are not also closed-filled.
    assert len(payload["bands"]) == 1
    assert payload["bands"][0]["layer_id"] == "layer-1"
    assert "fills" not in payload


def test_hulls_take_precedence_over_bands_for_the_same_layers():
    earlier = _layer("layer-0", EARLIER, (_open("a", 0.5),))
    later = _layer("layer-1", LATER, (_open("b", 1.0),))
    payload = routing_overlay_data(_model([earlier, later]), "uniform", True)

    # Three non-collinear points per layer enclose a hull, so the hull fills
    # the layer and no band or closed contour is painted over it.
    assert len(payload["fills"]) == 2
    assert "bands" not in payload


def test_bands_do_not_bridge_a_hulled_visible_layer():
    earlier = _layer("layer-0", EARLIER, (_collinear("lane-a", 48.0),))
    middle = _layer("layer-1", EARLIER + 3600, (_square("m", 0.3),))
    later = _layer("layer-2", LATER, (_collinear("lane-b", 48.02),))

    # The two hull-less layers would pair if they were treated as adjacent.
    assert build_bands(
        [
            {
                "layer_id": "layer-0",
                "epoch_s": EARLIER,
                "contours": [_collinear_points(48.0)],
            },
            {
                "layer_id": "layer-2",
                "epoch_s": LATER,
                "contours": [_collinear_points(48.02)],
            },
        ]
    )["bands"]

    # The hulled middle layer splits the band run, so no band spans from the
    # first visible layer to the third.
    payload = routing_overlay_data(_model([earlier, middle, later]), "uniform", True)
    assert "bands" not in payload
    assert [fill["layer_id"] for fill in payload["fills"]] == ["layer-1"]


def test_payload_reuses_the_models_hull_cache(monkeypatch):
    import sailworks.rendering.routingoverlay as overlay

    calls = []
    original = overlay.layer_hulls

    def counted(points, **kwargs):
        calls.append(points)
        return original(points, **kwargs)

    monkeypatch.setattr(overlay, "layer_hulls", counted)
    model = _model(
        [
            _layer("layer-0", EARLIER, (_open("a", 0.5),)),
            _layer("layer-1", LATER, (_open("b", 1.0),)),
        ]
    )

    first = routing_overlay_data(model, "uniform", True, True)
    computed = len(calls)
    assert computed == 2

    second = routing_overlay_data(model, "uniform", True, True)
    # A second frame reuses the model's revision-keyed cache; recomputing the
    # hulls per payload would double the count.
    assert len(calls) == computed
    assert second["fills"] == first["fills"]


def test_single_layer_never_produces_bands():
    payload = routing_overlay_data(
        _model([_layer("only", EARLIER, (_open("a", 0.5),))]), "uniform", True
    )
    assert "bands" not in payload
    assert "bands_note" not in payload


def test_closed_fill_is_the_fallback_when_no_band_can_span_the_pair():
    earlier = _layer("layer-0", EARLIER, (_square("closed", 1.0),))
    later = _layer("layer-1", LATER, (Contour("dot", False, (Point(48.0, -124.0),)),))
    payload = routing_overlay_data(_model([earlier, later]), "uniform", True)

    assert "bands" not in payload
    assert [fill["layer_id"] for fill in payload["fills"]] == ["layer-0"]


def test_banded_layers_do_not_also_emit_closed_fills(no_hulls):
    earlier = _layer("layer-0", EARLIER, (_square("a", 0.5),))
    later = _layer("layer-1", LATER, (_square("b", 1.0),))
    payload = routing_overlay_data(_model([earlier, later]), "uniform", True)

    assert payload["bands"]
    # Both layers are covered by bands, so the closed fills would double-draw
    # the same area and are suppressed.
    assert "fills" not in payload


def test_time_band_uses_the_later_layer_gradient_colour(no_hulls):
    earlier = _layer("layer-0", EARLIER, (_open("a", 0.5),))
    later = _layer("layer-1", LATER, (_open("b", 1.0),))
    payload = routing_overlay_data(_model([earlier, later]), "time", True)

    band = payload["bands"][0]
    assert band["colour"] == list(gradient_colour("layer_time", 1.0))[:3] + [ALPHA]


def test_day_night_band_uses_the_midpoint_at_its_centroid(no_hulls):
    earlier = _layer("layer-0", EARLIER, (_open("a", 0.5),))
    later = _layer("layer-1", LATER, (_open("b", 1.0),))
    payload = routing_overlay_data(_model([earlier, later]), "day_night", True)

    band = payload["bands"][0]
    expected = list(
        day_night_colour((EARLIER + LATER) // 2, _centroid(band["points"]))
    )[:3]
    assert band["colour"] == expected + [ALPHA]


def test_band_cap_discloses_what_was_left_out(monkeypatch, no_hulls):
    import sailworks.rendering.routingoverlay as overlay
    from sailworks.rendering import isobands

    contours = tuple(_open(f"o{index}", 0.4 * index) for index in range(1, 4))
    earlier = _layer("layer-0", EARLIER, contours)
    later = _layer(
        "layer-1",
        LATER,
        tuple(_open(f"p{index}", 0.4 * index + 0.2) for index in range(1, 4)),
    )

    def capped(layers):
        return isobands.build_bands(layers, max_bands=1)

    monkeypatch.setattr(overlay, "build_bands", capped)
    payload = routing_overlay_data(_model([earlier, later]), "uniform", True)
    assert len(payload["bands"]) == 1
    assert payload["bands_note"] == "1 of 3 layer bands drawn (capped)"


def test_skipped_pairs_are_disclosed_in_the_note(monkeypatch, no_hulls):
    import sailworks.rendering.routingoverlay as overlay
    from sailworks.rendering import isobands

    earlier = _layer("layer-0", EARLIER, (_open("a", 0.5),))
    later = _layer("layer-1", LATER, (_open("b", 0.6),))

    def gated(layers):
        built = isobands.build_bands(layers)
        built["skipped"] = 2
        return built

    monkeypatch.setattr(overlay, "build_bands", gated)
    payload = routing_overlay_data(_model([earlier, later]), "uniform", True)
    assert len(payload["bands"]) == 1
    assert payload["bands_note"] == "1 band (2 pairs skipped)"


def test_canvas_suppression_is_unchanged_and_bands_ride_the_canvas(no_hulls):
    assert isochrone_contours_on_canvas("uniform", False) is False
    assert isochrone_contours_on_canvas("uniform", True) is True
    assert isochrone_contours_on_canvas("time", False) is True

    model = _model(
        [
            _layer("layer-0", EARLIER, (_open("a", 0.5),)),
            _layer("layer-1", LATER, (_open("b", 1.0),)),
        ]
    )
    host = SimpleNamespace(
        native_routing=model,
        isochrone_colour_by="uniform",
        isochrone_fill_visible=True,
        _retained_active=True,
    )
    model.set_fill_visible(True)
    filled = _routing_overlay(host)
    # The canvas owns the contours while the fill is on, and the bands travel
    # with the frontiers they belong to.
    assert filled["frontiers"]
    assert filled["bands"]

    model.set_fill_visible(False)
    host.isochrone_fill_visible = False
    plain = _routing_overlay(host)
    # Back to the retained single-colour path: both frontiers and bands gone.
    assert plain["frontiers"] == []
    assert "bands" not in plain
