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


def _result(layer_count: int = 1, traces=()) -> RoutingResult:
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
        traces=traces,
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


def test_hover_previews_and_click_pins_through_selection():
    from types import SimpleNamespace

    from sailworks.core.marine.routingrecord import Trace
    from sailworks.core.marine.routingselection import RoutingSelection
    from sailworks.qt.viewportmap import ViewportMap

    from sailworks.core.marine.routingrecord import Trace

    trace = Trace("t-0", Point(48.1, -123.9), (Point(48.0, -124.0),))
    result = _result(layer_count=1, traces=(trace,))
    model = RoutingOverlayModel(result)
    selection = RoutingSelection(result)
    host = SimpleNamespace(
        native_routing=model,
        controller=SimpleNamespace(routing=SimpleNamespace(native_selection=selection)),
        _viewport_overlay_cache=None,
        publish_route_overlay=lambda force=False: True,
    )

    model_with_trace = model
    host.native_routing = model_with_trace

    ViewportMap._update_routing_hover(host, 48.11, -123.89)
    assert model_with_trace.hover_trace_id == "t-0"

    assert ViewportMap.pin_routing_trace(host) is True
    assert selection.trace_id == "t-0"


def test_hidden_trace_is_not_hovered_or_pinned_through_the_app_path():
    from types import SimpleNamespace

    from sailworks.core.marine.routingrecord import Trace
    from sailworks.core.marine.routingselection import RoutingSelection
    from sailworks.qt.viewportmap import ViewportMap

    trace = Trace("t-0", Point(48.1, -123.9), (Point(48.0, -124.0),))
    result = _result(layer_count=1, traces=(trace,))
    model = RoutingOverlayModel(result)
    selection = RoutingSelection(result)
    host = SimpleNamespace(
        native_routing=model,
        controller=SimpleNamespace(routing=SimpleNamespace(native_selection=selection)),
        _viewport_overlay_cache=None,
        publish_route_overlay=lambda force=False: True,
    )

    ViewportMap._update_routing_hover(host, 48.11, -123.89)
    assert model.hover_trace_id == "t-0"
    model.set_traces_visible(False)
    ViewportMap._update_routing_hover(host, 48.11, -123.89)
    assert model.hover_trace_id is None
    assert ViewportMap.pin_routing_trace(host) is False
    assert selection.trace_id is None


def test_absent_native_geometry_is_disclosed_in_the_viewport_payload():
    from sailworks.qt.viewportmap import _routing_absence_caption

    model = RoutingOverlayModel(_result(layer_count=0))
    payload = _routing_overlay(_host(model))

    assert payload["geometry_state"] == "absent"
    assert payload["trace_times"] == "absent"
    assert payload["layers"] == []
    assert payload["traces"] == []
    assert "No routing layers" in _routing_absence_caption(model)

    host = _host(model)
    host._retained_active = True
    retained = _routing_overlay(host)
    assert retained["retained"] is True
    assert retained["geometry_state"] == "absent"
    assert retained["frontiers"] == []
    assert retained["traces"] == []


def test_retained_item_suppresses_the_duplicate_canvas_geometry():
    from types import SimpleNamespace

    from sailworks.qt.viewportmap import _routing_overlay

    model = RoutingOverlayModel(_result(layer_count=1))
    host = SimpleNamespace(native_routing=model, _retained_active=True)
    payload = _routing_overlay(host)
    assert payload["frontiers"] == []
    assert payload["traces"] == []

    host._retained_active = False
    assert _routing_overlay(host)["frontiers"]


def test_time_mode_keeps_canvas_contours_when_retained_draws_traces():
    from types import SimpleNamespace

    from sailworks.qt.viewportmap import _routing_overlay

    model = RoutingOverlayModel(_result(layer_count=2))
    host = SimpleNamespace(
        native_routing=model,
        _retained_active=True,
        isochrone_colour_by="time",
    )
    payload = _routing_overlay(host)

    # The retained native item has one colour, so time mode leaves the contours
    # to the canvas and keeps only the traces there.
    assert payload["retained"] is True
    assert payload["frontiers"]
    assert payload["frontiers"][0]["colour"]
    assert payload["legend"]["entries"]
    assert payload["traces"] == []


def test_retained_sync_does_not_rebuild_segments_per_publish():
    from types import SimpleNamespace

    from sailworks.qt.viewportmap import ViewportMap

    class FakeItem:
        def __init__(self):
            self.values = {}
            self.segment_writes = 0

        def setProperty(self, name, value):
            if name == "polylines":
                self.segment_writes += 1
            self.values[name] = value
            return True

    class FakeRoot:
        def __init__(self, item):
            self.item = item

        def property(self, name):
            return ""

        def setProperty(self, name, value):
            return True

        def findChild(self, _type, name):
            return self.item if name == "retainedRoutingOverlay" else None

    item = FakeItem()
    host = SimpleNamespace(
        native_routing=RoutingOverlayModel(_result(layer_count=2)),
        camera_state=SimpleNamespace(center_lat=48.3, center_lon=-123.6, zoom=6.0),
        _weather_presenter=SimpleNamespace(widget=None),
        _retained_qml_dir=lambda: "/tmp",
        _retained_import_added=True,
    )
    fake_widget = SimpleNamespace(
        rootObject=lambda: FakeRoot(item),
        engine=lambda: SimpleNamespace(addImportPath=lambda _path: None),
    )
    host._weather_presenter = SimpleNamespace(widget=fake_widget)

    for _ in range(5):
        assert ViewportMap._sync_retained_overlay(host) is True
    assert item.segment_writes == 1

    host.native_routing.set_layer_visible("layer-1", False)
    assert ViewportMap._sync_retained_overlay(host) is True
    assert item.segment_writes == 2


def test_retained_highlight_properties_follow_hover_and_selection():
    from types import SimpleNamespace

    from sailworks.core.marine.routingselection import RoutingSelection
    from sailworks.qt.viewportmap import ViewportMap

    class FakeItem:
        def __init__(self):
            self.values = {}

        def setProperty(self, name, value):
            self.values[name] = value
            return True

    result = _result(layer_count=1)
    model = RoutingOverlayModel(result)
    model._hover_trace_id = "trace-0"
    selection = RoutingSelection(result)
    item = FakeItem()
    host = SimpleNamespace(
        _retained_item=item,
        native_routing=model,
        controller=SimpleNamespace(routing=SimpleNamespace(native_selection=selection)),
    )

    ViewportMap._apply_retained_highlight(host)
    assert item.values["hoverId"] == "trace-0"
    assert item.values["selectedId"] == ""
