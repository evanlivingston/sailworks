"""Hull Outlines toggle from the Scene Collection through the window sync.

The regression these guard: the Outlines row is checked but the sync rewrites
the model flag from the Navigation panel selection, so the payload never gains
``outlines``; or the retained single-colour item keeps drawing the contours and
paints them under the hull strokes. Outlines are also independent of the Fill,
the contour eye and the trace web: each toggle must stand alone.
"""

from __future__ import annotations

import os
from types import MethodType, SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt

import sailworks.qt.viewportmap as viewportmap
from sailworks.core.geo.routing import Routing
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
from sailworks.qt.scenepanel import (
    KIND_ROUTING,
    NAME_COLUMN,
    NODE_ROLE,
    VISIBLE_COLUMN,
    ScenePanel,
)
from sailworks.qt.viewportmap import (
    _isochrone_outlines_visible,
    _routing_overlay,
    _viewport_overlay_key,
)
from sailworks.qt.window import PreviewWindow
from sailworks.rendering.palettes import ISOCHRONE_FILL_ALPHA
from sailworks.rendering.routingoverlay import (
    RoutingOverlayModel,
    isochrone_contours_on_canvas,
    routing_overlay_data,
)

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000197"
OTHER = "6f1c1a1e-0000-4000-8000-000000000198"
NOON_UTC = 1618171200


def _result(contours):
    leg = Leg(
        "leg-0",
        Point(48.2, -123.7),
        Point(48.4, -123.5),
        NOON_UTC,
        "2021-04-11T20:00:00Z",
        NOON_UTC + 900,
        "2021-04-11T20:15:00Z",
        {"wind_east_kn": Observation(10.0, "kn", "reported")},
    )
    layer = Layer("layer-0", NOON_UTC, canonical_utc(NOON_UTC), False, contours)
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
        legs=(leg,),
        layers=(layer,),
        capabilities=Capabilities("main", "absent", "sampled", 12, 1800),
        traces=(),
    )


def closed_overlay_model():
    square = Contour(
        "c-closed",
        True,
        (
            Point(48.0, -124.0),
            Point(48.0, -123.5),
            Point(48.5, -123.5),
            Point(48.5, -124.0),
        ),
    )
    return RoutingOverlayModel(_result((square,)))


def saved_route(calculation_id=CALCULATION):
    route = Routing("route", [], [], True)
    route.result_summary = {"calculation_id": calculation_id}
    return route


class ToggleRow:
    """The row fields ``_item_changed`` reads for a checkbox node."""

    def __init__(self, node, checked):
        self._node = node
        self._checked = checked

    def data(self, column, role):
        return self._node if (column, role) == (NAME_COLUMN, NODE_ROLE) else None

    def checkState(self, column):
        return Qt.CheckState.Checked if self._checked else Qt.CheckState.Unchecked


class FakeMap:
    """A canvas that resolves both flags through the real viewport helpers."""

    def __init__(self):
        self.native_routing = None
        self.payloads = []
        self.invalidations = 0

    def set_isochrones(self, frontiers):
        self.frontiers = tuple(frontiers)

    def invalidate_navigation(self):
        self.invalidations += 1

    def refresh_route_overlay(self):
        model = self.native_routing
        if model is None:
            return
        self.payloads.append(
            routing_overlay_data(
                model,
                "uniform",
                viewportmap._isochrone_fill_visible(self, model),
                _isochrone_outlines_visible(self, model),
            )
        )

    def update(self):
        pass


def scene_panel(route, emitted):
    return SimpleNamespace(
        syncing=False,
        scene=SimpleNamespace(
            live_entry=lambda kind, key: SimpleNamespace(element=route)
        ),
        _save_visible_elements=lambda kinds: None,
        changed=SimpleNamespace(emit=lambda: None),
        routing_visibility_changed=SimpleNamespace(emit=emitted.append),
        selected=SimpleNamespace(emit=lambda *args: None),
    )


def fake_window(route, native, *, active_route=None):
    window = SimpleNamespace(
        navigation_panel=SimpleNamespace(route=route, refresh=lambda: None),
        controller=SimpleNamespace(
            routing=SimpleNamespace(
                busy=False,
                native_overlay=native,
                native_selection=None,
            )
        ),
        map=FakeMap(),
        log_map=FakeMap(),
        _routing_active_route=active_route,
    )
    for name in (
        "_native_owns_route",
        "_refresh_route_isochrone_row",
        "_sync_isochrone_colour_mode",
        "_sync_route_isochrones",
        "_routing_visibility_changed",
    ):
        setattr(window, name, MethodType(getattr(PreviewWindow, name), window))
    return window


def toggle(panel, route, node, checked):
    ScenePanel._item_changed(
        panel, ToggleRow((node, KIND_ROUTING, route.uid), checked), VISIBLE_COLUMN
    )


def test_scene_outlines_toggle_reaches_the_payload_and_repaints():
    model = closed_overlay_model()
    route = saved_route()
    window = fake_window(route, model, active_route=route)
    # A saved route restored from the scene has no Navigation panel selection;
    # the row is the only place the toggle names its route.
    window.navigation_panel.route = None
    emitted = []
    panel = scene_panel(route, emitted)

    toggle(panel, route, "routing-outlines", True)

    assert route.isochrone_outlines_visible is True
    assert emitted == [route]
    # The colour-mode sync runs first on a scene change and must not clear a
    # flag whose owner is only known to the row.
    PreviewWindow._sync_isochrone_colour_mode(window)
    assert model.outlines_visible is False
    PreviewWindow._routing_visibility_changed(window, emitted[-1])
    assert model.outlines_visible is True
    assert window.map.isochrone_outlines_visible is True

    payload = window.map.payloads[-1]
    assert payload["outlines"]
    assert payload["outlines"][0]["layer_id"] == "layer-0"
    # Stroking a boundary is not filling it.
    assert "fills" not in payload
    assert window.map.invalidations >= 1

    toggle(panel, route, "routing-outlines", False)

    assert route.isochrone_outlines_visible is False
    PreviewWindow._routing_visibility_changed(window, emitted[-1])
    assert model.outlines_visible is False
    assert "outlines" not in window.map.payloads[-1]


def test_outlines_are_independent_of_fill_eye_and_traces():
    model = closed_overlay_model()
    route = saved_route()
    window = fake_window(route, model, active_route=route)
    emitted = []
    panel = scene_panel(route, emitted)

    toggle(panel, route, "routing-outlines", True)
    PreviewWindow._routing_visibility_changed(window, emitted[-1])

    # Outlines alone never set the fill, the eye or the traces.
    assert model.outlines_visible is True
    assert model.fill_visible is False
    assert model.contours_visible is True
    assert model.traces_visible is False
    assert route.isochrone_fill_visible is False
    assert "fills" not in window.map.payloads[-1]

    # The fill can join without clearing the outlines.
    toggle(panel, route, "routing-fill", True)
    PreviewWindow._routing_visibility_changed(window, emitted[-1])
    assert model.fill_visible is True
    assert model.outlines_visible is True
    both = window.map.payloads[-1]
    assert both["fills"] and both["outlines"]
    assert both["fills"][0]["colour"][-1] == ISOCHRONE_FILL_ALPHA
    # The stroke stays opaque; only the interior carries fill alpha.
    assert len(both["outlines"][0]["colour"]) == 3

    # Turning the outlines off leaves the fill untouched.
    toggle(panel, route, "routing-outlines", False)
    PreviewWindow._routing_visibility_changed(window, emitted[-1])
    assert model.fill_visible is True
    assert model.outlines_visible is False
    assert "outlines" not in window.map.payloads[-1]
    assert window.map.payloads[-1]["fills"]

    # Hiding the contours through the eye drops both, because neither has a
    # frontier to belong to.
    toggle(panel, route, "routing-isochrones", False)
    PreviewWindow._routing_visibility_changed(window, emitted[-1])
    assert model.contours_visible is False
    hidden = window.map.payloads[-1]
    assert "fills" not in hidden
    assert "outlines" not in hidden


def test_scene_outlines_toggle_never_strokes_a_foreign_overlay():
    model = closed_overlay_model()
    presented = saved_route(CALCULATION)
    toggled = saved_route(OTHER)
    window = fake_window(presented, model, active_route=presented)
    emitted = []
    panel = scene_panel(toggled, emitted)

    toggle(panel, toggled, "routing-outlines", True)

    assert toggled.isochrone_outlines_visible is True
    PreviewWindow._routing_visibility_changed(window, emitted[-1])

    # The overlay was presented for another result, so the toggled route's
    # flag must not leak onto it.
    assert model.outlines_visible is False
    assert window.map.isochrone_outlines_visible is False
    assert "outlines" not in window.map.payloads[-1]


def _overlay_host(model, *, fill=False, outlines=False, retained=False):
    return SimpleNamespace(
        native_routing=model,
        isochrone_colour_by="uniform",
        isochrone_fill_visible=fill,
        isochrone_outlines_visible=outlines,
        _retained_active=retained,
    )


def test_outlines_suppress_retained_contours_without_double_drawing():
    model = closed_overlay_model()
    model.set_contours_visible(True)
    canvas_mode = isochrone_contours_on_canvas

    # The retained item draws one colour, so it must drop contours whenever the
    # canvas owns the shapes. That predicate is what the filter reads.
    assert canvas_mode("uniform", False, False) is False
    assert canvas_mode("uniform", False, True) is True
    assert canvas_mode("uniform", True, False) is True
    assert canvas_mode("uniform", True, True) is True

    retained = _overlay_host(model, outlines=True, retained=True)
    model.set_outlines_visible(True)
    assert _isochrone_outlines_visible(retained) is True
    stroked = _routing_overlay(retained)
    # Retained suppresses the duplicates but keeps one copy of the frontiers,
    # never two: the outlines need them and the retained item does not draw
    # them.
    assert stroked["retained"] is True
    assert stroked["outlines"]
    assert stroked["traces"] == []
    assert stroked["path"] == []

    # Fill and outlines together still publish the frontiers once.
    model.set_fill_visible(True)
    both = _routing_overlay(
        _overlay_host(model, fill=True, outlines=True, retained=True)
    )
    plain = _routing_overlay(_overlay_host(model, outlines=True, retained=True))
    assert [frontier["segments"] for frontier in both["frontiers"]] == [
        frontier["segments"] for frontier in plain["frontiers"]
    ]

    # Back on the retained single-colour path the canvas copy is dropped, so
    # nothing is drawn twice.
    model.set_fill_visible(False)
    model.set_outlines_visible(False)
    quiet = _routing_overlay(_overlay_host(model, retained=True))
    assert quiet["frontiers"] == []
    assert "outlines" not in quiet


def test_outlines_toggle_changes_the_overlay_cache_key(monkeypatch):
    # The helpers the viewport key calls reach the whole overlay state; stub
    # them so the key depends only on the isochrone flags under test.
    for name in ("_overlay_pois", "_overlay_tracks", "_overlay_ais"):
        monkeypatch.setattr(viewportmap, name, lambda host: ((),))
    monkeypatch.setattr(viewportmap, "_overlay_routes", lambda host, now: ((),))

    def host(model, *, fill=False, outlines=False):
        return SimpleNamespace(
            _view_state=SimpleNamespace(
                points=(),
                offsets=None,
                route_plan=(),
                route_preview=(),
                selected_route_point=-1,
                position=None,
            ),
            controller=SimpleNamespace(
                core=SimpleNamespace(boat_info=None), chart_errors=None
            ),
            time_control=SimpleNamespace(time=SimpleNamespace(timestamp=lambda: 0)),
            native_routing=model,
            isochrone_frame=None,
            isochrone_error=None,
            isochrone_colour_by="uniform",
            isochrone_fill_visible=fill,
            isochrone_outlines_visible=outlines,
            _retained_active=False,
            dashboard=None,
            dashboard_visible=False,
            move_callback=None,
            _measure_points=(),
            _measure_preview_point=None,
        )

    model = closed_overlay_model()
    plain = _viewport_overlay_key(host(model))
    assert _viewport_overlay_key(host(model)) == plain
    # The model owns both flags. A host mirror that disagrees cannot change the
    # payload, so it must not change the key either.
    assert _viewport_overlay_key(host(model, fill=True, outlines=True)) == plain

    model.set_outlines_visible(True)
    outlined = _viewport_overlay_key(host(model))
    assert outlined != plain
    model.set_fill_visible(True)
    assert _viewport_overlay_key(host(model)) != outlined


def test_model_flag_overrides_a_stale_host_mirror():
    model = closed_overlay_model()
    # A host left over from before the toggle says both are on; the model owns
    # the state and says both are off. The stale mirror must not win.
    stale = _overlay_host(model, fill=True, outlines=True)
    assert viewportmap._isochrone_fill_visible(stale, model) is False
    assert _isochrone_outlines_visible(stale, model) is False

    model.set_fill_visible(True)
    model.set_outlines_visible(True)
    bare = _overlay_host(model, fill=False, outlines=False)
    assert viewportmap._isochrone_fill_visible(bare, model) is True
    assert _isochrone_outlines_visible(bare, model) is True


def test_legacy_model_without_flags_falls_back_to_the_host():
    legacy = SimpleNamespace()
    host = SimpleNamespace(
        native_routing=legacy,
        isochrone_fill_visible=True,
        isochrone_outlines_visible=True,
    )
    assert viewportmap._isochrone_fill_visible(host) is True
    assert _isochrone_outlines_visible(host) is True
