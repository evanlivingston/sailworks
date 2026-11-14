"""The metric registry is the single source of every palette and legend."""

from __future__ import annotations

import json
from pathlib import Path

from sailworks.core.geo.routing import ROUTE_COLOUR_MODES
from sailworks.rendering import palettes
from sailworks.rendering.style import (
    ROUTE_DOWNWIND_COLOUR,
    ROUTE_PORT_COLOUR,
    ROUTE_STARBOARD_COLOUR,
    ROUTE_UNKNOWN_COLOUR,
    ROUTE_UPWIND_COLOUR,
    SPEED_BAND_LABELS,
    SPEED_BANDS,
    route_leg_colour,
    route_legend_entries,
    route_speed_color,
    route_tack_colour,
    route_wind_angle_colour,
)

ROOT = Path(__file__).resolve().parents[1]


def test_registry_covers_every_route_mode_and_the_isochrone_layer():
    assert palettes.route_metric_ids() == ROUTE_COLOUR_MODES
    assert set(("speed", "wind_angle", "tack", "sail_plan", "propulsion")) <= set(
        ROUTE_COLOUR_MODES
    )
    assert "layer_time" in palettes.METRICS


def test_each_ramp_has_exactly_one_definition():
    style_source = (ROOT / "sailworks/rendering/style.py").read_text()
    navigation_source = (ROOT / "sailworks/rendering/navigation.py").read_text()
    overlay_source = (ROOT / "sailworks/rendering/routingoverlay.py").read_text()
    qml_source = (ROOT / "sailworks/qt/shaders/ViewportChartScene.qml").read_text()

    # The tables are data in the registry, not literals in a renderer.
    assert "SPEED_BANDS = (" not in style_source
    assert "ISOCHRONE_TIME_GRADIENT = (" not in overlay_source
    assert "function speedColor" not in qml_source
    assert "speedColor(" not in qml_source
    # No renderer builds a colour table of its own.
    assert "ROUTE_PORT_COLOUR = (" not in navigation_source

    speed = palettes.METRICS["speed"]
    assert SPEED_BANDS == tuple(
        (limit, colour) for limit, colour, _label in speed.bands
    )
    assert SPEED_BAND_LABELS == tuple(label for _l, _c, label in speed.bands)
    # The compatibility wrappers resolve through the registry, not a copy.
    assert route_speed_color(4.5) == palettes.colour_for("speed", 4.5)


def test_speed_bands_keep_the_196_boundaries():
    assert route_speed_color(0.0) == (0.3, 0.5, 1.0, 0.9)
    assert route_speed_color(1.999) == (0.3, 0.5, 1.0, 0.9)
    assert route_speed_color(2.0) == (0.2, 0.7, 0.9, 0.9)
    assert route_speed_color(4.0) == (0.2, 0.85, 0.5, 0.9)
    assert route_speed_color(6.0) == (0.5, 0.9, 0.2, 0.9)
    assert route_speed_color(8.0) == (0.9, 0.85, 0.1, 0.9)
    assert route_speed_color(10.0) == (1.0, 0.6, 0.1, 0.9)
    assert route_speed_color(12.0) == (1.0, 0.3, 0.1, 0.9)
    assert route_speed_color(40.0) == (1.0, 0.3, 0.1, 0.9)
    assert route_speed_color(None) == ROUTE_UNKNOWN_COLOUR
    assert route_speed_color("fast") == ROUTE_UNKNOWN_COLOUR


def test_wind_angle_boundary_and_unknown():
    assert route_wind_angle_colour(89.999) == ROUTE_UPWIND_COLOUR
    assert route_wind_angle_colour(-89.999) == ROUTE_UPWIND_COLOUR
    assert route_wind_angle_colour(90.0) == ROUTE_DOWNWIND_COLOUR
    assert route_wind_angle_colour(None) == ROUTE_UNKNOWN_COLOUR
    assert route_wind_angle_colour(object()) == ROUTE_UNKNOWN_COLOUR


def test_categorical_colours_cycle_in_first_appearance_order():
    values = ("J2", "J1", "J2", "A3", "J1")
    mapping = palettes.colour_map("sail_plan", values)

    cycle = palettes.METRICS["sail_plan"].cycle
    assert list(mapping) == ["j2", "j1", "a3"]
    assert mapping["j2"] == cycle[0]
    assert mapping["j1"] == cycle[1]
    assert mapping["a3"] == cycle[2]
    # Resolution follows the same order, so a plan keeps its colour.
    assert palettes.colour_for("sail_plan", "J2", values) == cycle[0]
    assert palettes.colour_for("sail_plan", "A3", values) == cycle[2]
    # A different leg order is a different route, not a re-colour of this one.
    reordered = palettes.colour_map("sail_plan", ("J1", "J2"))
    assert reordered["j1"] == cycle[0]


def test_categorical_colour_is_stable_across_a_json_roundtrip():
    values = ["J2", "J1", "J2", "A3"]
    restored = json.loads(json.dumps(values))
    assert palettes.colour_map("sail_plan", values) == palettes.colour_map(
        "sail_plan", restored
    )
    assert palettes.legend_for("sail_plan", values) == palettes.legend_for(
        "sail_plan", restored
    )


def test_tack_and_propulsion_categories_match_recorded_prefixes():
    assert route_tack_colour("port-tack") == ROUTE_PORT_COLOUR
    assert route_tack_colour("Starboard") == ROUTE_STARBOARD_COLOUR
    assert route_tack_colour("unknown") == ROUTE_UNKNOWN_COLOUR
    assert route_tack_colour(None) == ROUTE_UNKNOWN_COLOUR

    categories = {
        key: colour for key, colour, _label in palettes.METRICS["propulsion"].categories
    }
    assert palettes.colour_for("propulsion", "Sail") == categories["sail"]
    assert palettes.colour_for("propulsion", "MotorSail") == categories["motorsail"]
    assert palettes.colour_for("propulsion", "motor") == categories["motor"]
    assert palettes.colour_for("propulsion", None) == ROUTE_UNKNOWN_COLOUR
    assert palettes.colour_for("propulsion", "oars") == ROUTE_UNKNOWN_COLOUR


def test_legend_compacts_many_plans_and_keeps_present_entries():
    values = [f"P{index}" for index in range(10)]
    entries = palettes.legend_for("sail_plan", values)
    labels = [label for label, _colour in entries]

    assert len(entries) == palettes.DEFAULT_LEGEND_LIMIT
    assert labels[:7] == values[:7]
    assert labels[-1] == "… 4 more"
    assert labels[-1] not in values
    # Present plans keep the colour the route draws them with.
    colours = dict(entries)
    for value in values[:7]:
        assert colours[value] == palettes.colour_for("sail_plan", value, values)


def test_speed_and_upwind_legends_are_unchanged_from_196():
    assert route_legend_entries("speed") == (
        ("0–2 kt", (0.3, 0.5, 1.0, 0.9)),
        ("2–4 kt", (0.2, 0.7, 0.9, 0.9)),
        ("4–6 kt", (0.2, 0.85, 0.5, 0.9)),
        ("6–8 kt", (0.5, 0.9, 0.2, 0.9)),
        ("8–10 kt", (0.9, 0.85, 0.1, 0.9)),
        ("10–12 kt", (1.0, 0.6, 0.1, 0.9)),
        ("12+ kt", (1.0, 0.3, 0.1, 0.9)),
    )
    assert route_legend_entries("wind_angle") == (
        ("Upwind (<90° TWA)", ROUTE_UPWIND_COLOUR),
        ("Downwind (≥90° TWA)", ROUTE_DOWNWIND_COLOUR),
        ("Unknown", ROUTE_UNKNOWN_COLOUR),
    )
    assert route_legend_entries("tack") == (
        ("Port", ROUTE_PORT_COLOUR),
        ("Starboard", ROUTE_STARBOARD_COLOUR),
        ("Unknown", ROUTE_UNKNOWN_COLOUR),
    )


def test_leg_colour_for_every_mode_and_missing_value():
    assert route_leg_colour("speed", speed=3.0) == route_speed_color(3.0)
    assert route_leg_colour("wind_angle", twa=45.0) == ROUTE_UPWIND_COLOUR
    assert route_leg_colour("tack", tack="port") == ROUTE_PORT_COLOUR
    assert route_leg_colour("sail_plan", sail_plan="J1") == (
        palettes.colour_for("sail_plan", "J1")
    )
    assert route_leg_colour("propulsion", propulsion="Motor") == (
        palettes.colour_for("propulsion", "Motor")
    )
    assert route_leg_colour("sail_plan") == ROUTE_UNKNOWN_COLOUR
    assert route_leg_colour("unregistered", speed=5.0) == ROUTE_UNKNOWN_COLOUR


def test_labels_use_the_registry():
    assert palettes.metric_label("sail_plan") == "Sail plan"
    assert palettes.metric_label("layer_time") == "Isochrones (UTC)"
    assert palettes.metric_label("missing", fallback="Speed") == "Speed"
    assert palettes.label_for("tack", "port-tack") == "Port"
    assert palettes.label_for("sail_plan", "J1") == "J1"
    assert palettes.label_for("speed", None) == "Unknown"


def test_layer_time_gradient_is_registered():
    stops = palettes.METRICS["layer_time"].gradient
    assert palettes.gradient_colour("layer_time", 0.0) == list(stops[0])
    assert palettes.gradient_colour("layer_time", 1.0) == list(stops[-1])
