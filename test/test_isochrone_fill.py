"""Nested isochrone band fill: payload shape, ordering, colours and persistence.

Painting itself lives in QML and is not unit-tested; these tests cover the
payload the canvas consumes and the model flags that drive it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import sailworks.core.storage as storage
from sailworks.core.geo.routing import Routing
from sailworks.core.geo.routingcollection import RoutingCollection
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
    SchemaError,
    Trace,
    Validation,
)
from sailworks.core.marine.routingresult import RoutingResult, canonical_utc
from sailworks.core.marine.routingresultstore import RoutingResultStore
from sailworks.rendering.daynight import day_night_colour
from sailworks.rendering.palettes import (
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

FILL_ALPHA = ISOCHRONE_FILL_ALPHA

# 2021-04-11, a real passage day: 20:00Z is local solar midday at 125°W.
NOON_UTC = 1618171200
FILL_CALCULATION = "6f1c1a1e-0000-4000-8000-000000000197"
SCHEMA_CALCULATION = "6f1c1a1e-0000-4000-8000-000000000148"
SCHEMA_EPOCH = 1767225600
SCHEMA_LATER = 1767229200
SCHEMA_TREE = "a" * 64


def _leg(leg_id: str, start: Point, end: Point) -> Leg:
    return Leg(
        leg_id,
        start,
        end,
        NOON_UTC,
        "2021-04-11T20:00:00Z",
        NOON_UTC + 900,
        "2021-04-11T20:15:00Z",
        {"wind_east_kn": Observation(10.0, "kn", "reported")},
    )


def _result(layers=(), traces=()) -> RoutingResult:
    return RoutingResult(
        calculation_id=FILL_CALCULATION,
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
        legs=(_leg("leg-0", Point(48.2, -123.7), Point(48.4, -123.5)),),
        layers=layers,
        capabilities=Capabilities("main", "absent", "sampled", 12, 1800),
        traces=traces,
    )


def _layer(layer_id="layer-0", epoch_s=NOON_UTC, contours=()):
    return Layer(layer_id, epoch_s, canonical_utc(epoch_s), False, contours)


def _schema_result() -> RoutingResult:
    """A schema-v1 result with enough geometry for artifact persistence."""
    legs = (
        Leg(
            "leg-1",
            Point(48.0, -124.0),
            Point(48.5, -123.5),
            SCHEMA_EPOCH,
            canonical_utc(SCHEMA_EPOCH),
            SCHEMA_LATER,
            canonical_utc(SCHEMA_LATER),
            {"wind_speed_ms": Observation(6.0, "m/s", "reported")},
            {"heading_degrees": 90.0, "tack": "starboard"},
        ),
        Leg(
            "leg-2",
            Point(48.5, -123.5),
            Point(48.8, -123.0),
            SCHEMA_LATER,
            canonical_utc(SCHEMA_LATER),
            SCHEMA_LATER + 1200,
            canonical_utc(SCHEMA_LATER + 1200),
            None,
        ),
    )
    layers = (
        Layer(
            "layer-1",
            SCHEMA_EPOCH,
            canonical_utc(SCHEMA_EPOCH),
            False,
            (
                Contour(
                    "contour-1",
                    True,
                    (
                        Point(48.0, -124.0),
                        Point(48.0, -123.0),
                        Point(48.5, -123.0),
                    ),
                ),
                Contour("contour-2", False, (Point(48.1, -124.1),)),
            ),
        ),
        Layer(
            "layer-2",
            SCHEMA_LATER,
            canonical_utc(SCHEMA_LATER),
            True,
            (Contour("contour-3", False, (Point(48.2, -123.9),)),),
        ),
    )
    traces = (
        Trace(
            "trace-1",
            Point(48.5, -123.5),
            (Point(48.0, -124.0), Point(48.5, -123.5)),
            (SCHEMA_EPOCH, SCHEMA_LATER),
        ),
    )
    return RoutingResult(
        calculation_id=SCHEMA_CALCULATION,
        generation=1,
        sequence=4,
        status="complete",
        outcome=Outcome(True, True, Validation("passed", True)),
        engine_identity=EngineIdentity(
            "https://example.invalid/sailworks-native",
            "deadbeef",
            "1.2.3",
            "1.2",
            SCHEMA_TREE,
            None,
        ),
        provenance=Provenance(canonical_utc(SCHEMA_EPOCH), "recorded"),
        legs=legs,
        layers=layers,
        capabilities=Capabilities(
            producer="synthetic",
            trace_times="per_position",
            validation_mode="sampled",
            sample_count=12,
            cadence=3,
        ),
        traces=traces,
    )


def _collection(tmp_path, monkeypatch, *, max_entries=8):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    store = RoutingResultStore(tmp_path / "artifacts", max_entries=max_entries)
    collection = RoutingCollection(result_store=store)
    # Keep assertions focused on artifact persistence, not the collection file.
    collection.save = lambda: None
    return collection, store


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


def _model(*layers):
    return RoutingOverlayModel(_result(layers=layers))


def _area(points):
    """The payload's own shoelace area, recomputed for ordering checks."""
    from math import cos, radians

    scale = max(cos(radians(sum(p[0] for p in points) / len(points))), 1e-6)
    total = 0.0
    for index, (lat1, lon1) in enumerate(points):
        lat2, lon2 = points[(index + 1) % len(points)]
        total += (lon1 * scale) * lat2 - (lon2 * scale) * lat1
    return abs(total) / 2.0


def test_fill_defaults_off_and_roundtrips_for_schema_v1(tmp_path, monkeypatch):
    collection, _store = _collection(tmp_path, monkeypatch)
    route = collection.save_result("alpha", _schema_result())

    assert route.isochrone_fill_visible is False
    assert "isochrone_fill_visible" not in route.to_json()

    route.isochrone_fill_visible = True
    assert route.to_json()["isochrone_fill_visible"] is True
    assert Routing.from_json(route.to_json()).isochrone_fill_visible is True


def test_fill_defaults_off_and_roundtrips_for_legacy_routes():
    legacy = Routing("legacy", [], [], True)
    assert legacy.isochrone_fill_visible is False
    assert "isochrone_fill_visible" not in legacy.to_json()

    legacy.isochrone_fill_visible = True
    again = Routing.from_json(legacy.to_json())
    assert again.is_legacy
    assert again.isochrone_fill_visible is True


def test_fill_rejects_non_booleans_and_touches_on_change():
    payload = {
        "name": "legacy",
        "visible": True,
        "points": [[48.0, -124.0]],
        "isochrones": [],
    }
    assert Routing.from_json(payload).isochrone_fill_visible is False
    with pytest.raises(SchemaError):
        Routing.from_json({**payload, "isochrone_fill_visible": 1})
    with pytest.raises(SchemaError):
        Routing.from_json({**payload, "isochrone_fill_visible": None})

    route = Routing("legacy", [], [], True)
    revision = route.revision
    route.isochrone_fill_visible = False
    assert route.revision == revision
    route.isochrone_fill_visible = True
    assert route.revision == revision + 1


def test_open_contour_is_never_force_closed_and_only_the_closed_one_fills():
    layer = _layer(
        contours=(_square("closed", 1.0), _square("open", 2.0, closed=False))
    )
    payload = routing_overlay_data(_model(layer), "uniform", True)

    # One layer cannot band, and the hull interior is no longer painted. Only
    # the contour the engine recorded as closed reaches ``fills``; the open one
    # is never closed by the painter.
    assert len(payload["fills"]) == 1
    assert payload["fills"][0]["closed"] is True
    assert payload["fills"][0]["layer_id"] == "layer-0"
    assert {tuple(point) for point in payload["fills"][0]["points"]} == {
        (point.lat, point.lon) for point in _square("closed", 1.0).points
    }
    assert "bands" not in payload
    # Both engine chains still belong to the frontier, but the default stroke is
    # the envelope: four hull edges closed back to the first vertex, not the two
    # raw arms. Nothing is lost — the raw chains stay in the layer payload.
    assert payload["frontiers"][0]["source"] == "hull"
    assert len(payload["frontiers"][0]["segments"]) == 4
    assert len(payload["layers"][0]["segments"]) == 6


def test_bands_replace_hull_fill_and_closed_contours_are_the_last_tier(monkeypatch):
    import sailworks.rendering.routingoverlay as overlay

    # 1. Two hull-able layers are banded; the hull interior is not painted and
    # its closed contours are not filled on top of the strip.
    banded = routing_overlay_data(
        _model(
            _layer("layer-0", contours=(_square("a", 0.5),)),
            _layer("layer-1", contours=(_square("b", 1.0),)),
        ),
        "uniform",
        True,
    )
    assert banded["bands"]
    assert "fills" not in banded

    # 2. One layer cannot be banded, so a truly closed engine contour is the
    # fallback fill.
    single = routing_overlay_data(
        _model(_layer("layer-0", contours=(_square("only", 1.0),))), "uniform", True
    )
    assert "bands" not in single
    assert len(single["fills"]) == 1
    assert len(single["fills"][0]["points"]) == 4

    # 3. A pair the hull stage cannot reduce is still banded: the fill never
    # depended on hull availability.
    monkeypatch.setattr(overlay, "layer_hulls", lambda points, **kwargs: ())
    hull_less = routing_overlay_data(
        _model(
            _layer("layer-0", contours=(_square("c", 0.5),)),
            _layer("layer-1", contours=(_square("d", 1.0),)),
        ),
        "uniform",
        True,
    )
    assert hull_less["bands"]
    assert "fills" not in hull_less


def test_hulled_open_layers_produce_bands_and_no_hull_fills():
    payload = routing_overlay_data(
        _model(
            _layer("layer-0", contours=(_square("a", 0.5, closed=False),)),
            _layer("layer-1", contours=(_square("b", 1.0, closed=False),)),
        ),
        "uniform",
        True,
    )

    # Both layers are hull-able, but an open contour has no proven interior:
    # the fill is the sampled band strip and no hull area is asserted.
    assert payload["bands"]
    assert "fills" not in payload
    # The hull still drives the default boundary stroke for each layer.
    assert [frontier["source"] for frontier in payload["frontiers"]] == [
        "hull",
        "hull",
    ]


def test_payload_only_emits_fills_for_a_closed_contour_with_geometry():
    payload = routing_overlay_data(_model(_layer()), "uniform", True)
    assert "fills" not in payload


def test_closed_fallback_fills_are_ordered_by_strictly_descending_area():
    small = _square("small", 0.5)
    medium = _square("medium", 1.0)
    large = _square("large", 2.0)
    contours = (small, medium, large)

    # One layer cannot band, so its closed contours are the fill of last
    # resort, ordered largest first: each smaller ring covers its parent's
    # interior and leaves only the annulus between them visible.
    payload = routing_overlay_data(_model(_layer(contours=contours)), "uniform", True)

    areas = [_area(fill["points"]) for fill in payload["fills"]]
    assert len(areas) == 3
    assert areas[0] > areas[1] > areas[2]
    assert [fill["layer_id"] for fill in payload["fills"]] == ["layer-0"] * 3
    assert len(payload["fills"][0]["points"]) == 4


def test_uniform_fill_uses_the_registry_base_colour():
    payload = routing_overlay_data(
        _model(_layer(contours=(_square("fill", 1.0),))), "uniform", True
    )
    assert payload["fills"][0]["colour"] == list(ISOCHRONE_UNIFORM_COLOUR) + [
        FILL_ALPHA
    ]


def test_time_fill_matches_the_layer_gradient_colour():
    layer = _layer(contours=(_square("fill", 1.0),))
    payload = routing_overlay_data(_model(layer), "time", True)
    expected = list(gradient_colour("layer_time", 0.5))[:3] + [FILL_ALPHA]
    assert payload["fills"][0]["colour"] == expected


def test_day_night_fill_uses_the_contour_representative_point():
    layer = _layer(epoch_s=NOON_UTC, contours=(_square("fill", 1.0),))
    payload = routing_overlay_data(_model(layer), "day_night", True)

    # The square spans 48..49N, 124..123W, so its centroid is 48.5N, 123.5W.
    expected = list(day_night_colour(NOON_UTC, (48.5, -123.5)))[:3] + [FILL_ALPHA]
    assert payload["fills"][0]["colour"] == expected
    assert payload["fills"][0]["colour"][:3] == list((0.98, 0.82, 0.32))


def test_fills_absent_when_fill_is_off_or_the_eye_is_off():
    layer = _layer(contours=(_square("fill", 1.0),))
    model = _model(layer)

    assert "fills" not in routing_overlay_data(model, "uniform", False)

    model.set_contours_visible(False)
    hidden = routing_overlay_data(model, "uniform", True)
    assert "fills" not in hidden
    assert hidden["layers"] == []


def test_outline_hull_cap_discloses_what_was_left_out(monkeypatch):
    import sailworks.rendering.routingoverlay as overlay

    layers = tuple(
        _layer(f"layer-{index}", contours=(_square(f"c{index}", 0.5 * (index + 1)),))
        for index in range(3)
    )
    monkeypatch.setattr(overlay, "ISOCHRONE_HULL_MAX_HULLS", 2)

    payload = routing_overlay_data(_model(*layers), "uniform", False, True)
    assert len(payload["outlines"]) == 2
    assert payload["outlines_note"] == ("2 of 3 isochrone hulls drawn (largest first)")

    # Without a cap there is nothing to disclose.
    monkeypatch.setattr(overlay, "ISOCHRONE_HULL_MAX_HULLS", 200)
    uncapped = routing_overlay_data(_model(*layers), "uniform", False, True)
    assert "outlines_note" not in uncapped


def test_outline_hull_point_cap_keeps_the_largest_and_discloses(monkeypatch):
    import sailworks.rendering.routingoverlay as overlay

    layers = tuple(
        _layer(f"layer-{index}", contours=(_square(f"c{index}", 0.5 * (index + 1)),))
        for index in range(3)
    )
    # The largest hull has four points; a second would exceed the budget.
    monkeypatch.setattr(overlay, "ISOCHRONE_HULL_MAX_POINTS", 5)

    payload = routing_overlay_data(_model(*layers), "uniform", False, True)
    assert len(payload["outlines"]) == 1
    assert payload["outlines"][0]["layer_id"] == "layer-2"
    assert payload["outlines_note"] == ("1 of 3 isochrone hulls drawn (largest first)")


def test_closed_contour_fill_cap_still_discloses_what_was_left_out(monkeypatch):
    import sailworks.rendering.routingoverlay as overlay

    # A single layer has no pair to band, so its truly closed contours are the
    # fallback; they keep their own cap and their own caption.
    contours = tuple(_square(f"c{index}", 0.2 * index) for index in range(1, 5))
    monkeypatch.setattr(overlay, "ISOCHRONE_FILL_MAX_CONTOURS", 2)

    payload = routing_overlay_data(_model(_layer(contours=contours)), "uniform", True)
    assert len(payload["fills"]) == 2
    assert payload["fills_note"] == ("2 of 4 closed contours filled (largest first)")

    monkeypatch.setattr(overlay, "ISOCHRONE_FILL_MAX_CONTOURS", 200)
    uncapped = routing_overlay_data(_model(_layer(contours=contours)), "uniform", True)
    assert "fills_note" not in uncapped


def test_closed_contour_fill_point_cap_keeps_the_largest(monkeypatch):
    import sailworks.rendering.routingoverlay as overlay

    contours = tuple(_square(f"c{index}", 0.2 * index) for index in range(1, 5))
    # The largest square has four points; a second would exceed the budget.
    monkeypatch.setattr(overlay, "ISOCHRONE_FILL_MAX_POINTS", 5)

    payload = routing_overlay_data(_model(_layer(contours=contours)), "uniform", True)
    assert len(payload["fills"]) == 1
    assert payload["fills_note"] == ("1 of 4 closed contours filled (largest first)")


def test_single_open_layer_is_never_forced_closed():
    payload = routing_overlay_data(
        _model(_layer(contours=(_square("open", 1.0, closed=False),))),
        "uniform",
        True,
    )

    # There is no second layer to band and the only contour is open, so the
    # payload must not invent a ring to fill.
    assert "bands" not in payload
    assert "fills" not in payload
    assert payload["frontiers"][0]["source"] == "hull"


def test_fill_forces_the_canvas_path_and_restores_retained_contours():
    assert isochrone_contours_on_canvas("uniform", False) is False
    assert isochrone_contours_on_canvas("uniform", True) is True
    assert isochrone_contours_on_canvas("time", False) is True
    # Hull outlines force the canvas path on their own, so the retained item
    # gives up its contours as soon as either toggle is on.
    assert isochrone_contours_on_canvas("uniform", False, True) is True

    model = _model(_layer(contours=(_square("fill", 1.0),)))
    host = SimpleNamespace(
        native_routing=model,
        isochrone_colour_by="uniform",
        isochrone_fill_visible=True,
        _retained_active=True,
    )
    model.set_fill_visible(True)
    filled = _routing_overlay(host)
    # The canvas owns the contours while the fill is on, with no retained copy
    # underneath, and the fills travel with the frontiers they belong to.
    assert filled["retained"] is True
    assert filled["frontiers"]
    assert filled["fills"]

    # The window syncs both surfaces; the model is authoritative.
    model.set_fill_visible(False)
    host.isochrone_fill_visible = False
    plain = _routing_overlay(host)
    # Back to the retained single-colour path: frontiers and fills both gone.
    assert plain["frontiers"] == []
    assert "fills" not in plain

    # Outlines alone keep one copy of the frontiers and the hull strokes, and
    # still leave the retained item nothing to duplicate. The model owns the
    # flag; the host mirror follows it.
    model.set_outlines_visible(True)
    host.isochrone_outlines_visible = True
    stroked = _routing_overlay(host)
    assert stroked["frontiers"]
    assert stroked["outlines"]
    assert "fills" not in stroked


def test_model_owns_the_fill_flag_like_contours_and_traces():
    model = _model(_layer(contours=(_square("fill", 1.0),)))
    assert model.fill_visible is False
    assert "fills" not in routing_overlay_data(model, "uniform")

    revision = model.style_revision
    model.set_fill_visible(True)
    assert model.fill_visible is True
    assert model.style_revision == revision + 1
    assert routing_overlay_data(model, "uniform")["fills"]

    with pytest.raises(TypeError):
        model.set_fill_visible("yes")

    # A caller that resolved the flag elsewhere can still override this call.
    assert "fills" not in routing_overlay_data(model, "uniform", False)


def test_legacy_frontier_tuples_never_fill():
    # A legacy route has frontier tuples and no closed flag, so it reaches the
    # payload through the overlay model only when it has schema-v1 geometry.
    # The closest analogue in this payload is a contour set with no closed
    # member: it must yield no fills and must not raise.
    layer = _layer(
        contours=(
            Contour("open-a", False, (Point(48.0, -124.0), Point(48.2, -123.8))),
            Contour("open-b", False, (Point(47.0, -123.0), Point(47.2, -122.8))),
        )
    )
    payload = routing_overlay_data(_model(layer), "uniform", True)
    assert "fills" not in payload
    assert "fills_note" not in payload
