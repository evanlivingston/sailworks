"""Live Fill toggle from the Scene Collection through the window sync.

The regression these cover: the toggle handler stored the flag on the scene
route, but the window sync recomputed it from the Navigation panel selection
and wrote ``False`` back to the overlay model, so the published payload never
gained ``fills``.
"""

from __future__ import annotations

import os
from types import MethodType, SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt

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
from sailworks.qt.window import PreviewWindow
from sailworks.rendering.routingoverlay import RoutingOverlayModel, routing_overlay_data

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000148"
OTHER = "6f1c1a1e-0000-4000-8000-000000000149"


def _result(contours):
    """Minimal complete result carrying one layer with the given contours."""
    leg = Leg(
        "leg-0",
        Point(48.2, -123.7),
        Point(48.4, -123.5),
        1767225600,
        "2026-01-01T00:00:00Z",
        1767226500,
        "2026-01-01T00:15:00Z",
        {"wind_east_kn": Observation(10.0, "kn", "reported")},
    )
    epoch_s = 1767226500
    layer = Layer("layer-0", epoch_s, canonical_utc(epoch_s), False, contours)
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
        provenance=Provenance("2026-01-01T00:00:00Z", "recorded"),
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
        fill = bool(
            model.fill_visible or getattr(self, "isochrone_fill_visible", False)
        )
        self.payloads.append(routing_overlay_data(model, "uniform", fill))

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


def toggle_fill(panel, route, checked):
    node = ("routing-fill", KIND_ROUTING, route.uid)
    ScenePanel._item_changed(panel, ToggleRow(node, checked), VISIBLE_COLUMN)


def test_scene_fill_toggle_publishes_fills_for_the_owned_saved_route():
    model = closed_overlay_model()
    route = saved_route()
    window = fake_window(route, model, active_route=route)
    # A saved route restored from the scene has no Navigation panel selection;
    # the row is the only place the toggle names its route.
    window.navigation_panel.route = None
    emitted = []
    panel = scene_panel(route, emitted)

    toggle_fill(panel, route, True)

    assert route.isochrone_fill_visible is True
    assert emitted == [route]
    # ``_scene_changed`` runs before the routing visibility signal, so it must
    # not clear a fill whose owner is only known to the row.
    PreviewWindow._sync_isochrone_colour_mode(window)
    assert model.fill_visible is False
    PreviewWindow._routing_visibility_changed(window, emitted[-1])
    assert model.fill_visible is True

    payload = window.map.payloads[-1]
    assert payload["fills"]
    assert 0 < payload["fills"][0]["colour"][-1] <= 0.5
    assert window.map.invalidations >= 1

    toggle_fill(panel, route, False)

    assert route.isochrone_fill_visible is False
    PreviewWindow._routing_visibility_changed(window, emitted[-1])
    assert model.fill_visible is False
    assert "fills" not in window.map.payloads[-1]


def test_scene_fill_toggle_never_fills_a_foreign_overlay():
    model = closed_overlay_model()
    presented = saved_route(CALCULATION)
    toggled = saved_route(OTHER)
    window = fake_window(presented, model, active_route=presented)
    emitted = []
    panel = scene_panel(toggled, emitted)

    toggle_fill(panel, toggled, True)

    assert toggled.isochrone_fill_visible is True
    PreviewWindow._routing_visibility_changed(window, emitted[-1])

    # The overlay was presented for another result, so the toggled route's
    # flag must not leak onto it.
    assert model.fill_visible is False
    assert "fills" not in window.map.payloads[-1]
