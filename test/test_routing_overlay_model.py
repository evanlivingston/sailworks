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
from sailworks.rendering.routingoverlay import (
    RoutingOverlayModel,
    routing_overlay_data,
)

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


def test_hidden_geometry_is_not_pickable_or_hoverable():
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

    model.set_hover_trace("t-0")
    assert model.hover_trace_id == "t-0"
    model.set_traces_visible(False)
    assert model.hover_trace_id is None
    assert model.pick_trace_near(48.11, -123.89) is None
    model.set_hover_trace("t-0")
    assert model.hover_trace_id is None

    model.set_layer_visible("layer-0", False)
    assert model.nearest_layer(1767226500) is None
    assert model.geometry_digest == digest
    assert model.geometry_revision == revision


def test_nearest_layer_skips_hidden_layers():
    layers = (
        _layer("layer-0", 1767226500),
        _layer("layer-1", 1767228300),
    )
    model = RoutingOverlayModel(_result(layers=layers))
    model.set_layer_visible("layer-0", False)

    assert model.nearest_layer(1767226500) == "layer-1"


def test_empty_geometry_and_absent_traces_are_explicit():
    model = RoutingOverlayModel(_result())
    assert model.layers == ()
    assert model.polylines == ()
    assert model.pick_trace(0.0, 0.0) is None
    assert model.nearest_layer(1767226500) is None
    assert model.geometry_state == "absent"
    assert model.trace_times == "absent"
    payload = routing_overlay_data(model)
    assert payload["geometry_state"] == "absent"
    assert payload["trace_times"] == "absent"


def test_hidden_geometry_is_disclosed_not_reported_as_drawn():
    contour = Contour("c-0", True, (Point(48.0, -124.0), Point(48.1, -123.9)))
    model = RoutingOverlayModel(_result(layers=(_layer(contours=(contour,)),)))
    assert model.geometry_state == "drawn"

    model.set_layer_visible("layer-0", False)
    assert model.geometry_state == "hidden"
    assert routing_overlay_data(model)["geometry_state"] == "hidden"


def test_unknown_layer_reference_is_rejected():
    model = RoutingOverlayModel(_result(layers=(_layer(),)))
    with pytest.raises(KeyError):
        model.set_layer_visible("missing", False)
    with pytest.raises(KeyError):
        model.set_selected_layer("missing")


def test_qml_payload_uses_engine_segments_without_joins():
    from sailworks.rendering.routingoverlay import routing_overlay_data

    crossing = Contour("c-dateline", False, (Point(10.0, 179.5), Point(10.0, -179.5)))
    layer = _layer(contours=(crossing,))
    model = RoutingOverlayModel(_result(layers=(layer,)))
    payload = routing_overlay_data(model)

    assert payload["schema_version"] == 1
    assert payload["calculation_id"] == CALCULATION
    assert payload["layers"][0]["id"] == "layer-0"
    # The dateline contour is split, so no segment crosses the world.
    segments = payload["frontiers"][0]["segments"]
    assert segments == []
    assert payload["frontiers"][0]["points"] == []
    assert payload["layers"][0]["contour_count"] == 2
    assert payload["layers"][0]["closed"] == [False, False]


def test_qml_payload_respects_visibility_and_trace_toggle():
    from sailworks.rendering.routingoverlay import routing_overlay_data

    contour = Contour("c-0", True, (Point(48.0, -124.0), Point(48.1, -123.9)))
    trace = Trace(
        "t-0", Point(48.1, -123.9), (Point(48.2, -123.7), Point(48.1, -123.9))
    )
    model = RoutingOverlayModel(
        _result(layers=(_layer(contours=(contour,)),), traces=(trace,))
    )
    assert routing_overlay_data(model)["frontiers"][0]["segments"] == [
        [[48.0, -124.0], [48.1, -123.9]]
    ]
    assert routing_overlay_data(model)["traces"][0]["id"] == "t-0"

    model.set_traces_visible(False)
    assert routing_overlay_data(model)["traces"] == []
    model.set_layer_visible("layer-0", False)
    payload = routing_overlay_data(model)
    assert payload["layers"] == []
    assert payload["frontiers"] == []


def test_qml_payload_is_stable_across_style_changes():
    from sailworks.rendering.routingoverlay import routing_overlay_data

    contour = Contour("c-0", True, (Point(48.0, -124.0), Point(48.1, -123.9)))
    model = RoutingOverlayModel(_result(layers=(_layer(contours=(contour,)),)))
    before = routing_overlay_data(model)
    model.set_thickness("contour", 2.5)
    after = routing_overlay_data(model)

    assert after["frontiers"] == before["frontiers"]
    assert after["geometry_revision"] == before["geometry_revision"]
    assert after["style_revision"] > before["style_revision"]


def test_isochrone_time_payload_colours_and_legend_follow_epoch_order():
    from sailworks.rendering.routingoverlay import (
        ISOCHRONE_TIME_GRADIENT,
        routing_overlay_data,
    )

    oldest = _layer(
        "layer-old",
        1767226500,
        (Contour("c-old", True, (Point(48.0, -124.0), Point(48.1, -123.9))),),
    )
    newest = _layer(
        "layer-new",
        1767228300,
        (Contour("c-new", True, (Point(48.2, -123.7), Point(48.3, -123.6))),),
    )
    model = RoutingOverlayModel(_result(layers=(newest, oldest)))

    payload = routing_overlay_data(model, "time")

    assert payload["isochrone_colour_by"] == "time"
    # Layers and frontiers carry the same colour for a layer.
    by_id = {layer["id"]: layer for layer in payload["layers"]}
    by_frontier = {layer["layer_id"]: layer for layer in payload["frontiers"]}
    assert by_id["layer-old"]["colour"] == by_frontier["layer-old"]["colour"]
    # Oldest to newest: the gradient runs from its first to its last stop.
    assert by_id["layer-old"]["colour"] == list(ISOCHRONE_TIME_GRADIENT[0])
    assert by_id["layer-new"]["colour"] == list(ISOCHRONE_TIME_GRADIENT[-1])
    legend = payload["legend"]
    assert legend["title"] == "Isochrones (UTC)"
    assert [entry[0] for entry in legend["entries"]] == ["00:15Z", "00:45Z"]
    assert [entry[1] for entry in legend["entries"]] == [
        list(ISOCHRONE_TIME_GRADIENT[0]),
        list(ISOCHRONE_TIME_GRADIENT[-1]),
    ]


def test_uniform_isochrone_payload_is_unchanged_by_the_time_switch():
    from sailworks.rendering.routingoverlay import routing_overlay_data

    contour = Contour("c-0", True, (Point(48.0, -124.0), Point(48.1, -123.9)))
    model = RoutingOverlayModel(_result(layers=(_layer(contours=(contour,)),)))

    default = routing_overlay_data(model)
    explicit = routing_overlay_data(model, "uniform")

    assert explicit == default
    assert "colour" not in default["layers"][0]
    assert "colour" not in default["frontiers"][0]
    assert "legend" not in default
    assert "isochrone_colour_by" not in default


def test_hidden_isochrones_produce_no_time_legend():
    from sailworks.rendering.routingoverlay import routing_overlay_data

    contour = Contour("c-0", True, (Point(48.0, -124.0), Point(48.1, -123.9)))
    model = RoutingOverlayModel(_result(layers=(_layer(contours=(contour,)),)))

    model.set_contours_visible(False)
    payload = routing_overlay_data(model, "time")

    assert payload["layers"] == []
    assert payload["legend"]["entries"] == []


def test_missing_layer_times_mark_only_that_layer_and_disclose_it(monkeypatch):
    from types import SimpleNamespace

    from sailworks.rendering import routingoverlay

    contour = Contour("c-0", True, (Point(48.0, -124.0), Point(48.1, -123.9)))
    model = RoutingOverlayModel(_result(layers=(_layer(contours=(contour,)),)))
    monkeypatch.setattr(
        routingoverlay,
        "isochrone_time_colours",
        lambda _layers: ({}, ("layer-0",)),
    )

    payload = routingoverlay.routing_overlay_data(model, "time")

    # Time is never silently downgraded for the whole result.
    assert payload["isochrone_colour_by"] == "time"
    assert payload["isochrone_fallback"] == "no recorded layer times"
    assert "colour" not in payload["layers"][0]
    assert payload["legend"]["entries"] == []
    assert "without recorded time" in payload["legend"]["note"]


def test_numeric_epochs_are_accepted_and_only_bad_layers_are_uniform():
    from types import SimpleNamespace

    from sailworks.rendering.routingoverlay import isochrone_time_colours

    layers = (
        SimpleNamespace(layer_id="whole-float", epoch_s=1767226500.0),
        SimpleNamespace(layer_id="int", epoch_s=1767228300),
        SimpleNamespace(layer_id="fraction", epoch_s=1767226500.5),
        SimpleNamespace(layer_id="boolean", epoch_s=True),
        SimpleNamespace(layer_id="missing", epoch_s=None),
    )
    colours, unavailable = isochrone_time_colours(layers)

    assert set(colours) == {"whole-float", "int"}
    assert unavailable == ("fraction", "boolean", "missing")


def test_degenerate_gradient_spreads_equal_epochs_and_singles():
    from types import SimpleNamespace

    from sailworks.rendering.routingoverlay import (
        ISOCHRONE_TIME_GRADIENT,
        _gradient_colour,
        isochrone_time_colours,
    )

    single, _unavailable = isochrone_time_colours(
        (SimpleNamespace(layer_id="only", epoch_s=1767226500),)
    )
    # A lone layer is mid-gradient, never the oldest deep blue.
    assert single["only"] == _gradient_colour(0.5)
    assert single["only"] != list(ISOCHRONE_TIME_GRADIENT[0])

    equal, _unavailable = isochrone_time_colours(
        (
            SimpleNamespace(layer_id="b", epoch_s=1767226500),
            SimpleNamespace(layer_id="a", epoch_s=1767226500),
        )
    )
    assert equal["a"] == list(ISOCHRONE_TIME_GRADIENT[0])
    assert equal["b"] == list(ISOCHRONE_TIME_GRADIENT[-1])
    assert equal["a"] != equal["b"]


def test_same_minute_legend_labels_are_distinguishable(monkeypatch):
    from types import SimpleNamespace

    from sailworks.rendering.routingoverlay import isochrone_time_legend

    layers = (
        SimpleNamespace(layer_id="a", epoch_s=1767226500),
        SimpleNamespace(layer_id="b", epoch_s=1767226510),
    )
    legend = isochrone_time_legend(
        layers, {"a": [0.0, 0.3, 0.85], "b": [0.65, 0.05, 0.05]}
    )

    labels = [entry[0] for entry in legend["entries"]]
    assert len(set(labels)) == 2
    assert labels == ["00:15:00Z", "00:15:10Z"]


def test_overlay_model_loads_an_offline_result_file(tmp_path):
    contour = Contour("c-0", True, (Point(48.0, -124.0), Point(48.1, -123.9)))
    result = _result(layers=(_layer(contours=(contour,)),))
    path = tmp_path / "result.json"
    path.write_text(result.to_json(), encoding="utf-8")

    model = RoutingOverlayModel.from_result_file(path)
    assert model.calculation_id == CALCULATION
    assert [polyline.object_id for polyline in model.polylines] == ["c-0"]
    assert RoutingOverlayModel.from_result_json(result.to_json()).geometry_digest == (
        model.geometry_digest
    )


def test_qml_payload_carries_layer_time_and_selection():
    from sailworks.rendering.routingoverlay import routing_overlay_data

    contour = Contour("c-0", True, (Point(48.0, -124.0), Point(48.1, -123.9)))
    model = RoutingOverlayModel(_result(layers=(_layer(contours=(contour,)),)))
    model.set_selected_layer("layer-0")
    payload = routing_overlay_data(model)

    assert payload["selected_layer_id"] == "layer-0"
    frontier = payload["frontiers"][0]
    assert frontier["layer_id"] == "layer-0"
    assert frontier["time"] == 1767226500
    assert frontier["selected"] is True
    assert frontier["reverse"] is False


def test_hover_pick_is_bounded_and_does_not_touch_geometry():
    from sailworks.rendering.routingoverlay import routing_overlay_data

    trace = Trace("t-0", Point(48.10, -123.60), (Point(48.0, -123.7),))
    model = RoutingOverlayModel(_result(traces=(trace,)))
    digest = model.geometry_digest
    revision = model.geometry_revision

    assert model.pick_trace_near(48.11, -123.59, 0.1) == "t-0"
    assert model.pick_trace_near(49.5, -123.6, 0.1) is None

    model.set_hover_trace("t-0")
    payload = routing_overlay_data(model)
    assert payload["hover_trace_id"] == "t-0"
    assert payload["traces"][0]["hover"] is True
    assert model.geometry_digest == digest
    assert model.geometry_revision == revision

    model.set_hover_trace(None)
    assert routing_overlay_data(model)["traces"][0]["hover"] is False


def test_contour_toggle_hides_isochrones_without_losing_layer_state():
    from sailworks.rendering.routingoverlay import routing_overlay_data

    contour = Contour("c-0", True, (Point(48.0, -124.0), Point(48.1, -123.9)))
    model = RoutingOverlayModel(_result(layers=(_layer(contours=(contour,)),)))
    assert model.contours_visible is True
    assert model.visible_polylines()
    assert routing_overlay_data(model)["layers"]

    model.set_contours_visible(False)

    # Isochrones disappear, but the per-layer visibility is not overwritten.
    assert model.contour_visible("layer-0") is False
    assert model.is_layer_visible("layer-0") is True
    assert model.visible_polylines() == ()
    assert routing_overlay_data(model)["layers"] == []
    assert routing_overlay_data(model)["frontiers"] == []

    model.set_contours_visible(True)

    assert model.contour_visible("layer-0") is True
    assert model.visible_polylines()
    assert routing_overlay_data(model)["layers"]


def test_contour_toggle_leaves_traces_and_other_layers_alone():
    from sailworks.rendering.routingoverlay import routing_overlay_data

    contour = Contour("c-0", True, (Point(48.0, -124.0), Point(48.1, -123.9)))
    trace = Trace("t-0", Point(48.10, -123.60), (Point(48.0, -123.7),))
    model = RoutingOverlayModel(
        _result(layers=(_layer(contours=(contour,)),), traces=(trace,))
    )
    model.set_contours_visible(False)

    payload = routing_overlay_data(model)
    assert payload["traces"]
    assert payload["layers"] == []
    assert model.trace_is_visible("t-0") is True

    model.set_contours_visible(True)
    payload = routing_overlay_data(model)
    assert payload["traces"]
    assert payload["layers"]
