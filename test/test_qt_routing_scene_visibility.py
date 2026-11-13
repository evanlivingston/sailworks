"""Routing scene visibility at the window sync boundary."""

from __future__ import annotations

import os
from types import MethodType, SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sailworks.core.geo.routing import Routing
from sailworks.core.marine.routingrecord import Contour, Point, Trace
from sailworks.qt.window import PreviewWindow
from sailworks.rendering.routingoverlay import RoutingOverlayModel, routing_overlay_data
from test.test_routing_overlay_model import _layer, _result

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000148"
OTHER = "6f1c1a1e-0000-4000-8000-000000000149"


class FakeMap:
    def __init__(self):
        self.native_routing = None
        self.frames = []

    def set_isochrones(self, frontiers):
        self.frames.append(tuple(frontiers))

    def invalidate_navigation(self):
        pass

    def refresh_route_overlay(self):
        pass

    def update(self):
        pass


class FakeNative:
    def __init__(self, calculation_id=CALCULATION):
        self.calculation_id = calculation_id
        self.contours_visible = None
        self.traces_visible = None
        self.selection = None

    def set_contours_visible(self, visible):
        self.contours_visible = visible

    def set_traces_visible(self, visible):
        self.traces_visible = visible

    def attach_selection(self, selection):
        self.selection = selection


class FakeInspector:
    def __init__(self):
        self.errors = []

    def show_error(self, message):
        self.errors.append(message)


def frontier_payload(frontiers):
    """Tuple-of-tuples payload without depending on point identity."""
    payload = []
    for frontier in frontiers:
        points = []
        for point in frontier:
            if hasattr(point, "to_list"):
                points.append(tuple(point.to_list()))
            elif hasattr(point, "lat") and hasattr(point, "lon"):
                points.append((point.lat, point.lon))
            else:
                points.append(tuple(point))
        payload.append(tuple(points))
    return tuple(payload)


def contour_ids(model):
    return tuple(
        polyline.object_id for polyline in model.polylines if polyline.kind == "contour"
    )


def fake_window(route, native, *, active_route=None, busy=False):
    routing = SimpleNamespace(
        busy=busy,
        native_overlay=native,
        native_selection=None,
    )
    window = SimpleNamespace(
        navigation_panel=SimpleNamespace(route=route, refresh=lambda: None),
        controller=SimpleNamespace(
            routing=routing,
            time_control=SimpleNamespace(time=0),
        ),
        map=FakeMap(),
        log_map=FakeMap(),
        _routing_active_route=active_route,
    )
    window._publish_race_track_overlay = lambda: None
    window._race_time_changed = lambda *_args: None
    # Bind the production ownership and sync helpers so this fixture exercises
    # the same route/overlay matching and cache-key sync as the real window
    # rather than a partial stand-in. Isochrone mode resolution itself is a
    # module-level pure function, so it needs no binding.
    window._native_owns_route = MethodType(PreviewWindow._native_owns_route, window)
    window._sync_isochrone_colour_mode = MethodType(
        PreviewWindow._sync_isochrone_colour_mode, window
    )
    return window


def route_with_summary(calculation_id=CALCULATION, *, visible=True, isochrones=True):
    route = Routing("route", [], [], True, isochrones_visible=isochrones)
    route.visible = visible
    route.result_summary = {"calculation_id": calculation_id}
    return route


def test_hidden_isochrones_gate_canvas_and_native_without_hydrating():
    route = route_with_summary(isochrones=False)
    route.result_ref = object()
    route._isochrones = None
    native = FakeNative(CALCULATION)
    window = fake_window(route, native, active_route=route)

    PreviewWindow._sync_route_isochrones(window)

    assert route._isochrones is None
    assert window.map.frames == [()]
    assert native.contours_visible is False
    assert window.map.native_routing is native


def test_visible_isochrones_stay_independent_of_route_visibility():
    route = route_with_summary(isochrones=True)
    route.isochrones = [[(48.0, -124.0), (48.1, -123.9)]]
    native = FakeNative(CALCULATION)
    window = fake_window(route, native, active_route=route)

    PreviewWindow._sync_route_isochrones(window)

    assert route.visible is True
    assert window.map.frames == [([(48.0, -124.0), (48.1, -123.9)],)]
    assert native.contours_visible is True
    assert window.map.native_routing is native


def test_foreign_native_result_is_detached_not_toggled():
    route = route_with_summary(OTHER, isochrones=False)
    native = FakeNative(CALCULATION)
    other = route_with_summary()
    window = fake_window(route, native, active_route=other)

    PreviewWindow._sync_route_isochrones(window)

    assert native.contours_visible is None
    assert window.map.native_routing is None


def test_independently_opened_result_is_not_governed_by_route_toggle():
    route = route_with_summary(OTHER, isochrones=True)
    native = FakeNative(CALCULATION)
    window = fake_window(route, native, active_route=None)

    PreviewWindow._sync_route_isochrones(window)

    assert native.contours_visible is None
    assert window.map.native_routing is native


def test_missing_summary_uses_active_route_identity():
    route = route_with_summary(isochrones=False)
    route.result_summary = None
    native = FakeNative(CALCULATION)
    window = fake_window(route, native, active_route=route)

    PreviewWindow._sync_route_isochrones(window)

    assert native.contours_visible is False
    assert window.map.native_routing is native

    foreign = fake_window(route, native, active_route=route_with_summary())
    PreviewWindow._sync_route_isochrones(foreign)
    assert foreign.map.native_routing is None


def test_unrelated_scene_change_does_not_hydrate_or_sync():
    route = route_with_summary(isochrones=True)
    route._isochrones = None
    window = fake_window(route, FakeNative(CALCULATION), active_route=route)

    PreviewWindow._scene_changed(window)

    assert window.map.frames == []
    assert route._isochrones is None


def test_reopened_saved_route_hydrates_from_artifact():
    route = route_with_summary(isochrones=True)
    route.result_ref = object()
    store = object()
    route.collection = SimpleNamespace(result_store=store)
    route._isochrones = None
    loads = []

    def load_result(arg):
        loads.append(arg)
        return SimpleNamespace(layers=())

    route.load_result = load_result
    native = FakeNative(CALCULATION)
    window = fake_window(route, native, active_route=route)

    PreviewWindow._sync_route_isochrones(window)

    assert loads == [store]
    assert route._isochrones == []
    assert window.map.frames == [()]
    assert native.contours_visible is True


def test_repeated_eye_toggle_after_reopen_restores_the_same_contours():
    """Eye OFF/ON cycles redraw the saved result, never re-read or solve it."""
    layer_0 = _layer(
        "layer-0",
        contours=(
            Contour(
                "c-closed",
                True,
                (Point(48.0, -124.0), Point(48.1, -123.9), Point(48.2, -123.8)),
            ),
            Contour("c-open", False, (Point(48.3, -123.7), Point(48.4, -123.6))),
        ),
    )
    layer_1 = _layer(
        "layer-1",
        1767228300,
        contours=(
            Contour("c-later", True, (Point(48.5, -123.5), Point(48.6, -123.4))),
        ),
    )
    trace = Trace("t-0", Point(48.2, -123.7), (Point(48.4, -123.5),))
    result = _result(layers=(layer_0, layer_1), traces=(trace,))

    # The saved summary must name the restored artifact's calculation so the
    # native overlay is recognised as this route's own result.
    route = route_with_summary(result.calculation_id, isochrones=True)
    route.result_ref = object()
    store = object()
    route.collection = SimpleNamespace(result_store=store)
    route._isochrones = None
    loads = []

    def load_result(arg):
        loads.append(arg)
        return result

    route.load_result = load_result
    native = RoutingOverlayModel(result)
    # One layer stays hidden independently: the toggle must not disturb it.
    native.set_layer_visible("layer-1", False)
    window = fake_window(route, native, active_route=route)

    def solver(*_args, **_kwargs):
        raise AssertionError("reopened route must never run the routing solver")

    window.controller.routing.calculate = solver

    # First ON hydrates the artifact once.
    PreviewWindow._sync_route_isochrones(window)
    first_on = frontier_payload(window.map.frames[-1])
    first_ids = contour_ids(native)
    first_overlay = routing_overlay_data(native)
    geometry = native.geometry_digest
    assert loads == [store]
    assert native.contours_visible is True
    assert first_ids == ("c-closed", "c-open", "c-later")
    assert first_overlay["layers"]
    assert window.map.native_routing is native

    for _cycle in range(2):
        style_before_off = native.style_revision
        # OFF: only the isochrone display is gated.
        route.isochrones_visible = False
        PreviewWindow._sync_route_isochrones(window)
        assert window.map.frames[-1] == ()
        assert native.contours_visible is False
        assert routing_overlay_data(native)["frontiers"] == []
        assert routing_overlay_data(native)["layers"] == []
        assert [p for p in native.visible_polylines() if p.kind == "contour"] == []
        # The route line, the trace geometry and the hydrated cache survive.
        assert route.visible is True
        assert route._isochrones is not None
        assert native.trace_ids == ("t-0",)
        assert native.geometry_digest == geometry

        # ON again: payload and contour identities match the first ON.
        route.isochrones_visible = True
        PreviewWindow._sync_route_isochrones(window)
        assert frontier_payload(window.map.frames[-1]) == first_on
        assert contour_ids(native) == first_ids
        restored = routing_overlay_data(native)
        expected = dict(first_overlay)
        # Toggling changes only the style signature that drives the repaint.
        # Geometry, layer/trace/path identities and colours stay identical.
        assert restored.pop("style_revision") != style_before_off
        expected.pop("style_revision")
        assert restored == expected
        assert restored["layers"] == first_overlay["layers"]
        assert restored["frontiers"] == first_overlay["frontiers"]
        assert restored["traces"] == first_overlay["traces"]
        assert restored["path"] == first_overlay["path"]
        assert restored["geometry_state"] == first_overlay["geometry_state"]
        assert restored["features"] == first_overlay["features"]
        assert restored["calculation_id"] == first_overlay["calculation_id"]
        assert restored["geometry_revision"] == first_overlay["geometry_revision"]
        assert native.contours_visible is True
        assert native.is_layer_visible("layer-0") is True
        assert native.is_layer_visible("layer-1") is False
        assert native.contour_visible("layer-1") is False
        assert native.geometry_digest == geometry
        assert window.map.native_routing is native

    # Two full OFF/ON cycles never hydrated twice or recalculated.
    assert loads == [store]


@pytest.mark.parametrize("exc", [OSError("artifact missing"), ValueError("bad json")])
def test_missing_or_corrupt_artifact_with_checked_eye_is_disclosed(exc):
    """A checked eye on an unreadable artifact says so, rather than going blank."""
    route = route_with_summary(isochrones=True)
    route.result_ref = object()
    route.collection = SimpleNamespace(result_store=object())
    # A reopened schema-v1 route starts unhydrated and reads lazily.
    route._isochrones = None

    def load_result(_route):
        raise exc

    route.load_result = load_result

    # The existing inspector path reports the restore failure visibly.
    inspector = FakeInspector()
    dock_visible = []
    window = SimpleNamespace(
        controller=SimpleNamespace(
            core=SimpleNamespace(
                routingManager=SimpleNamespace(load_result=load_result)
            )
        ),
        routing_inspector=inspector,
        routing_inspector_dock=SimpleNamespace(show=lambda: dock_visible.append(True)),
    )

    assert PreviewWindow._show_saved_routing_route(window, route) is False
    assert inspector.errors == [f"Routing result unavailable: {exc}"]
    assert dock_visible == [True]

    # The checked eye still refuses to raise and records the reason.
    assert route.isochrones_visible is True
    assert route.isochrones == ()
    assert route.contour_error is not None
    assert route.export_eligible is False


def test_unavailable_isochrones_reach_the_map_caption():
    from sailworks.qt.viewportmap import _routing_absence_caption

    route = route_with_summary(isochrones=True)
    route.result_ref = object()
    route.collection = SimpleNamespace(result_store=object())
    route._isochrones = None

    def unavailable(_store):
        raise OSError("artifact missing")

    route.load_result = unavailable
    window = fake_window(route, None)

    PreviewWindow._sync_route_isochrones(window)

    assert route.contour_error
    assert window.map.isochrone_error == route.contour_error
    caption = _routing_absence_caption(None, window.map.isochrone_error)
    assert caption.startswith("Isochrones unavailable: ")
    assert "artifact missing" in caption


def test_healthy_route_has_no_isochrone_caption():
    from sailworks.qt.viewportmap import _routing_absence_caption

    assert _routing_absence_caption(None) == ""


def test_legacy_route_eye_toggle_keeps_inline_contours_visible():
    """A legacy route has no artifact; toggling only gates its inline frontiers."""
    inline = [[(48.0, -124.0), (48.1, -123.9)]]
    route = Routing("legacy", [], inline, True)
    route.result_ref = None
    route.isochrones = inline
    native = FakeNative()
    window = fake_window(route, native, active_route=route)

    def no_load(_route):
        raise AssertionError("legacy routes must not read an artifact")

    route.load_result = no_load

    PreviewWindow._sync_route_isochrones(window)
    assert window.map.frames[-1] == ([(48.0, -124.0), (48.1, -123.9)],)
    assert native.contours_visible is True

    route.isochrones_visible = False
    PreviewWindow._sync_route_isochrones(window)
    assert window.map.frames[-1] == ()
    assert native.contours_visible is False
    assert route.visible is True

    route.isochrones_visible = True
    PreviewWindow._sync_route_isochrones(window)
    assert window.map.frames[-1] == ([(48.0, -124.0), (48.1, -123.9)],)
    assert native.contours_visible is True


def test_trace_visibility_applies_to_the_owning_map_only():
    route = route_with_summary(isochrones=False)
    route.traces_visible = True
    native = FakeNative(CALCULATION)
    # The log map never receives ``native_routing`` from the sync path. Give it
    # an unrelated overlay to prove that only the owning map is updated.
    other = FakeNative(CALCULATION)
    window = fake_window(route, native, active_route=route)
    window.log_map.native_routing = other

    PreviewWindow._sync_route_isochrones(window)

    # Hiding the isochrones must not hide the trace web, independently of the
    # contour gate.
    assert window.map.frames == [()]
    assert native.contours_visible is False
    assert native.traces_visible is True
    # Only the primary map owns the route's native overlay, so the second
    # canvas keeps its own overlay state untouched.
    assert window.log_map.native_routing is other
    assert other.contours_visible is None
    assert other.traces_visible is None


def test_trace_visibility_false_is_applied_to_native():
    route = route_with_summary(isochrones=True)
    route.traces_visible = False
    native = FakeNative(CALCULATION)
    window = fake_window(route, native, active_route=route)

    PreviewWindow._sync_route_isochrones(window)

    assert native.contours_visible is True
    assert native.traces_visible is False


def test_foreign_overlay_keeps_its_own_trace_visibility():
    route = route_with_summary(OTHER, isochrones=True)
    route.traces_visible = True
    native = FakeNative(CALCULATION)
    window = fake_window(route, native, active_route=route_with_summary())

    PreviewWindow._sync_route_isochrones(window)

    assert native.traces_visible is None
    assert window.map.native_routing is None


def test_hidden_isochrones_leave_visible_traces_drawn():
    trace = Trace("t-0", Point(48.0, -123.7), (Point(48.1, -123.6),))
    model = RoutingOverlayModel(_result(traces=(trace,)))
    model.set_traces_visible(True)
    model.set_contours_visible(False)

    kinds = {polyline.kind for polyline in model.visible_polylines()}
    assert "trace" in kinds

    model.set_traces_visible(False)
    assert all(polyline.kind != "trace" for polyline in model.visible_polylines())


def test_legacy_route_trace_toggle_persists_without_trace_geometry():
    route = Routing("legacy", [], [], True)
    assert route.traces_visible is False
    route.traces_visible = True

    restored = Routing.from_json(route.to_json())

    assert restored.is_legacy
    assert restored.traces_visible is True
    assert restored.result_ref is None
    assert restored.isochrones == []
    # No result artifact and no trace payload: the toggle only records intent.
    payload = restored.to_json()
    assert payload["traces_visible"] is True
    assert payload["isochrones"] == []
