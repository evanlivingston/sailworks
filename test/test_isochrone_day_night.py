"""Day/night isochrone colouring: solar classification, payload, persistence."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sailworks.core.geo.routing import (
    ISOCHRONE_COLOUR_MODES,
    ISOCHRONE_COLOUR_MODE_LABELS,
    Routing,
)
from sailworks.core.geo.solar import (
    CIVIL_TWILIGHT_ELEVATION,
    SUNRISE_ELEVATION,
    daylight_class,
    daylight_class_at,
    solar_elevation,
)
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
from sailworks.core.scene import KIND_ROUTING
from sailworks.rendering.daynight import (
    day_night_colour,
    day_night_segment_colours,
)
from sailworks.rendering.palettes import ROUTE_UNKNOWN_COLOUR
from sailworks.rendering.routingoverlay import (
    RoutingOverlayModel,
    isochrone_canvas_coloured,
    routing_overlay_data,
)

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000197"
# 2021-04-11, a real passage day: 20:00Z is local solar midday at 125°W.
NOON_UTC = 1618171200
NIGHT_UTC = 1618128000
TWILIGHT_UTC = 1618197600
LEGACY_EPOCH = NOON_UTC - 86400
DAY_COLOUR = (0.98, 0.82, 0.32, 0.9)
NIGHT_COLOUR = (0.34, 0.38, 0.48, 0.9)


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
        legs=(_leg("leg-0", Point(48.2, -123.7), Point(48.4, -123.5)),),
        layers=layers,
        capabilities=Capabilities("main", "absent", "sampled", 12, 1800),
        traces=traces,
    )


def _layer(layer_id="layer-0", epoch_s=NOON_UTC, contours=()):
    return Layer(layer_id, epoch_s, canonical_utc(epoch_s), False, contours)


def _terminator_contour():
    """One contour whose ends sit on opposite sides of the terminator."""
    return Contour(
        "c-crossing",
        True,
        (Point(48.0, -125.0), Point(48.0, -5.0), Point(48.0, 10.0)),
    )


def test_solar_elevation_matches_known_midday_and_midnight():
    noon = solar_elevation(48.0, -125.0, NOON_UTC)
    night = solar_elevation(48.0, -125.0, NIGHT_UTC)

    # Local solar noon at 125°W is about 20:20Z, so 20:00Z is high daylight.
    assert noon == pytest.approx(50.4, abs=0.5)
    assert night == pytest.approx(-33.4, abs=0.5)
    assert daylight_class_at(NOON_UTC, 48.0, -125.0) == "day"
    assert daylight_class_at(NIGHT_UTC, 48.0, -125.0) == "night"


def test_twilight_bands_are_half_open_at_the_documented_thresholds():
    assert daylight_class(SUNRISE_ELEVATION + 0.001) == "day"
    assert daylight_class(SUNRISE_ELEVATION) == "civil_twilight"
    assert daylight_class(CIVIL_TWILIGHT_ELEVATION + 0.001) == "civil_twilight"
    assert daylight_class(CIVIL_TWILIGHT_ELEVATION) == "night"
    assert daylight_class(-89.0) == "night"
    assert daylight_class(None) is None


def test_evening_civil_twilight_is_classified_from_a_real_epoch():
    elevation = solar_elevation(48.0, -125.0, TWILIGHT_UTC)

    assert CIVIL_TWILIGHT_ELEVATION < elevation <= SUNRISE_ELEVATION
    assert daylight_class_at(TWILIGHT_UTC, 48.0, -125.0) == "civil_twilight"


def test_missing_time_or_position_stays_unknown():
    assert solar_elevation(None, -125.0, NOON_UTC) is None
    assert solar_elevation(48.0, None, NOON_UTC) is None
    assert solar_elevation(48.0, -125.0, None) is None
    assert daylight_class_at(None, 48.0, -125.0) is None
    assert day_night_colour(None, (48.0, -125.0)) == ROUTE_UNKNOWN_COLOUR
    assert day_night_colour(NOON_UTC, ()) == ROUTE_UNKNOWN_COLOUR


def test_terminator_crossing_contour_gets_two_colours_in_one_layer():
    model = RoutingOverlayModel(
        _result(layers=(_layer(contours=(_terminator_contour(),)),))
    )

    payload = routing_overlay_data(model, "day_night")
    frontier = payload["frontiers"][0]
    colours = frontier["segment_colours"]

    assert payload["isochrone_colour_by"] == "day_night"
    # Uniform and time carry a layer colour; day/night is per point.
    assert "colour" not in payload["layers"][0]
    assert "colour" not in frontier
    assert len(colours) == len(frontier["segments"]) == 2
    assert colours == [list(DAY_COLOUR), list(NIGHT_COLOUR)]
    assert frontier["segments"] == [
        [[48.0, -125.0], [48.0, -5.0]],
        [[48.0, -5.0], [48.0, 10.0]],
    ]
    points = [tuple(segment[0]) for segment in frontier["segments"]]
    assert points == [(48.0, -125.0), (48.0, -5.0)]


def test_day_night_segment_colours_follow_each_leading_point():
    colours = day_night_segment_colours(
        NOON_UTC, ((48.0, -125.0), (48.0, -5.0), (48.0, 10.0))
    )

    assert len(colours) == 2
    assert colours[0] == DAY_COLOUR
    assert colours[1] == NIGHT_COLOUR


def test_day_night_legend_lists_categories_then_unknown():
    model = RoutingOverlayModel(
        _result(layers=(_layer(contours=(_terminator_contour(),)),))
    )

    legend = routing_overlay_data(model, "day_night")["legend"]

    assert legend["title"] == "Day / night"
    assert [entry[0] for entry in legend["entries"]] == [
        "Day",
        "Civil twilight",
        "Night",
        "Unknown",
    ]
    assert legend["entries"][0][1] == list(DAY_COLOUR)
    assert legend["entries"][-1][1] == list(ROUTE_UNKNOWN_COLOUR)


def test_uniform_payload_stays_identical_and_keeps_no_legend():
    model = RoutingOverlayModel(
        _result(layers=(_layer(contours=(_terminator_contour(),)),))
    )

    default = routing_overlay_data(model)
    explicit = routing_overlay_data(model, "uniform")

    assert explicit == default
    assert "segment_colours" not in default["frontiers"][0]
    assert "colour" not in default["frontiers"][0]
    assert "legend" not in default
    assert "isochrone_colour_by" not in default


def test_time_mode_is_unchanged_by_the_new_mode():
    older = _layer("layer-old", NOON_UTC, (_terminator_contour(),))
    newer = _layer(
        "layer-new",
        NOON_UTC + 1800,
        (Contour("c-new", True, (Point(48.2, -123.7), Point(48.3, -123.6))),),
    )
    model = RoutingOverlayModel(_result(layers=(newer, older)))

    payload = routing_overlay_data(model, "time")

    assert payload["isochrone_colour_by"] == "time"
    assert payload["legend"]["title"] == "Isochrones (UTC)"
    assert "segment_colours" not in payload["frontiers"][0]
    assert payload["layers"][0]["colour"] == payload["frontiers"][0]["colour"]


def test_day_night_canvas_modes_are_split_from_the_retained_item():
    assert isochrone_canvas_coloured("uniform") is False
    assert isochrone_canvas_coloured("time") is True
    assert isochrone_canvas_coloured("day_night") is True
    # An invalid mode normalises to uniform, never to a coloured canvas path.
    assert isochrone_canvas_coloured("twilight") is False


def test_retained_item_keeps_only_traces_for_day_night():
    from sailworks.qt.viewportmap import _routing_overlay

    model = RoutingOverlayModel(
        _result(layers=(_layer(contours=(_terminator_contour(),)),))
    )
    host = SimpleNamespace(
        native_routing=model,
        _retained_active=True,
        isochrone_colour_by="day_night",
    )

    payload = _routing_overlay(host)

    assert payload["retained"] is True
    assert payload["frontiers"]
    assert payload["frontiers"][0]["segment_colours"]
    assert payload["traces"] == []

    host.isochrone_colour_by = "uniform"
    uniform = _routing_overlay(host)
    assert uniform["frontiers"] == []


def test_day_night_mode_persists_and_normalises():
    route = Routing(
        "route",
        points=[(48.0, -124.0, canonical_utc(LEGACY_EPOCH), 45.0, 12.0, 5.0, 10.0)],
        isochrones=[],
    )
    assert route.isochrone_colour_by == "uniform"
    assert "isochrone_colour_by" not in route.to_json()

    route.isochrone_colour_by = "day_night"
    payload = route.to_json()
    assert payload["isochrone_colour_by"] == "day_night"
    assert Routing.from_json(payload).isochrone_colour_by == "day_night"

    payload["isochrone_colour_by"] = "twilight"
    assert Routing.from_json(payload).isochrone_colour_by == "uniform"


def test_mode_selector_lists_the_registry_modes_with_labels():
    from sailworks.qt.scenepanel import _isochrone_modes_for

    assert ISOCHRONE_COLOUR_MODES == ("uniform", "time", "day_night")
    assert set(ISOCHRONE_COLOUR_MODE_LABELS) == set(ISOCHRONE_COLOUR_MODES)
    assert ISOCHRONE_COLOUR_MODE_LABELS["day_night"] == "Day / night"
    # A route that references a result artifact can honour every mode.
    assert _isochrone_modes_for(SimpleNamespace(result_ref=object())) == (
        "uniform",
        "time",
        "day_night",
    )
    # A legacy route has nothing to colour, so only Uniform is offered.
    legacy = Routing(
        "legacy",
        points=[(48.0, -124.0, canonical_utc(LEGACY_EPOCH), 45.0, 12.0, 5.0, 10.0)],
        isochrones=[],
    )
    assert _isochrone_modes_for(legacy) == ("uniform",)


def test_legacy_route_rejects_a_persisted_day_night_mode():
    from sailworks.qt.scenepanel import ScenePanel

    legacy = Routing(
        "legacy",
        points=[(48.0, -124.0, canonical_utc(LEGACY_EPOCH), 45.0, 12.0, 5.0, 10.0)],
        isochrones=[],
    )
    panel = SimpleNamespace(
        scene=SimpleNamespace(
            live_entry=lambda kind, key: SimpleNamespace(element=legacy)
        ),
        changed=SimpleNamespace(emit=lambda: None),
    )
    panel._colour_by_target = lambda node: ScenePanel._colour_by_target(panel, node)

    ScenePanel._set_isochrone_colour_by(
        panel, ("item", KIND_ROUTING, legacy.uid), "day_night"
    )

    assert legacy.isochrone_colour_by == "uniform"
