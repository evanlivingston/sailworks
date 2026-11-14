"""Per-route colour modes: persistence, leg alignment, unknown states, legend."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt

import sailworks.core.storage as storage
from sailworks.core.geo.routing import ISOCHRONE_COLOUR_MODES, Routing
from sailworks.core.geo.routingcollection import RoutingCollection
from sailworks.core.marine.routingrecord import (
    Capabilities,
    EngineIdentity,
    Leg,
    Outcome,
    Point,
    Provenance,
    SchemaError,
    Validation,
)
from sailworks.core.marine.routingresult import RoutingResult, canonical_utc
from sailworks.core.marine.routingresultstore import RoutingResultStore
from sailworks.core.scene import KIND_ROUTING
from sailworks.qt.scenepanel import ScenePanel
from sailworks.rendering.navigation import NavigationRenderer
from sailworks.rendering.style import (
    ROUTE_DOWNWIND_COLOUR,
    ROUTE_PORT_COLOUR,
    ROUTE_STARBOARD_COLOUR,
    ROUTE_UNKNOWN_COLOUR,
    ROUTE_UPWIND_COLOUR,
    route_legend_entries,
    route_speed_color,
)

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000193"
EPOCH = 1767225600
TREE = "b" * 64
UPWIND = {
    "true_wind_angle_degrees": 45.0,
    "speed_over_ground_knots": 5.0,
    "heading_degrees": 10.0,
    "tack": "port",
}
DOWNWIND = {
    "true_wind_angle_degrees": 150.0,
    "speed_over_ground_knots": 9.0,
    "heading_degrees": 200.0,
    "tack": "starboard",
}


def _leg(leg_id, start, end, start_s, end_s, readout=None):
    return Leg(
        leg_id,
        Point(*start),
        Point(*end),
        start_s,
        canonical_utc(start_s),
        end_s,
        canonical_utc(end_s),
        readout=readout,
    )


def _result(legs):
    return RoutingResult(
        calculation_id=CALCULATION,
        generation=1,
        sequence=1,
        status="complete",
        outcome=Outcome(True, True, Validation("passed", True)),
        engine_identity=EngineIdentity(
            "https://example.invalid/sailworks-native",
            "deadbeef",
            "1.2.3",
            "1.2",
            TREE,
            None,
        ),
        provenance=Provenance(canonical_utc(EPOCH), "recorded"),
        legs=tuple(legs),
        layers=(),
        capabilities=Capabilities("synthetic", "absent", "sampled", 12, 1800),
    )


def _legs(readouts=(UPWIND, DOWNWIND)):
    return (
        _leg("leg-1", (48.0, -124.0), (48.5, -123.5), EPOCH, EPOCH + 900, readouts[0]),
        _leg(
            "leg-2",
            (48.5, -123.5),
            (48.8, -123.0),
            EPOCH + 900,
            EPOCH + 2100,
            readouts[1],
        ),
    )


def _collection(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    collection = RoutingCollection(
        result_store=RoutingResultStore(tmp_path / "artifacts")
    )
    collection.save = lambda: None
    return collection


def _overlay(route):
    return NavigationRenderer(SimpleNamespace(routingManager=[route]))


def test_colour_mode_roundtrip_for_schema_and_legacy(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result("native", _result(_legs()))
    route.colour_by = "tack"
    payload = route.to_json()
    assert payload["colour_by"] == "tack"
    restored = Routing.from_json(payload)
    assert restored.colour_by == "tack"

    # A missing field is the historical speed rendering, never a new mode.
    payload.pop("colour_by")
    assert Routing.from_json(payload).colour_by == "speed"
    # Unsupported values fall back instead of breaking a saved collection.
    payload["colour_by"] = "wind-speed"
    assert Routing.from_json(payload).colour_by == "speed"

    legacy = Routing(
        "legacy",
        points=[(48.0, -124.0, canonical_utc(EPOCH), 45.0, 12.0, 5.0, 10.0)],
        isochrones=[],
    )
    assert legacy.colour_by == "speed"
    # The default mode is omitted so existing saved collections stay byte
    # identical and legacy payloads still roundtrip unchanged.
    assert "colour_by" not in legacy.to_json()
    legacy.colour_by = "wind_angle"
    legacy_payload = legacy.to_json()
    assert legacy_payload["colour_by"] == "wind_angle"
    assert Routing.from_json(legacy_payload).colour_by == "wind_angle"


def test_isochrone_colour_mode_roundtrip_and_invalid_fallback(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result("native", _result(_legs()))
    assert route.isochrone_colour_by == "uniform"
    # The default is omitted so existing collections stay byte identical.
    assert "isochrone_colour_by" not in route.to_json()

    route.isochrone_colour_by = "time"
    payload = route.to_json()
    assert payload["isochrone_colour_by"] == "time"
    restored = Routing.from_json(payload)
    assert restored.isochrone_colour_by == "time"
    # Route colour is independent of the isochrone portrayal.
    assert restored.colour_by == "speed"

    payload["isochrone_colour_by"] = "upwind"
    assert Routing.from_json(payload).isochrone_colour_by == "uniform"
    payload.pop("isochrone_colour_by")
    assert Routing.from_json(payload).isochrone_colour_by == "uniform"

    legacy = Routing(
        "legacy",
        points=[(48.0, -124.0, canonical_utc(EPOCH), 45.0, 12.0, 5.0, 10.0)],
        isochrones=[],
    )
    assert "isochrone_colour_by" not in legacy.to_json()
    legacy.isochrone_colour_by = "time"
    legacy_payload = legacy.to_json()
    assert legacy_payload["isochrone_colour_by"] == "time"
    assert Routing.from_json(legacy_payload).isochrone_colour_by == "time"


def test_isochrone_colour_change_bumps_revision_and_keeps_visibility(
    tmp_path, monkeypatch
):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result("native", _result(_legs()))
    revision = route.revision

    route.isochrone_colour_by = "time"
    assert route.revision > revision
    assert route.isochrones_visible is True
    assert route.colour_by == "speed"

    revision = route.revision
    route.isochrone_colour_by = "time"
    assert route.revision == revision


def test_scene_isochrone_action_persists_and_requests_repaint(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result("native", _result(_legs()))
    repaints = []
    panel = SimpleNamespace(
        scene=SimpleNamespace(
            live_entry=lambda kind, key: SimpleNamespace(element=route)
        ),
        _after_change=lambda *args: repaints.append(args),
        changed=SimpleNamespace(emit=lambda: repaints.append(("changed",))),
    )
    panel._colour_by_target = lambda node: ScenePanel._colour_by_target(panel, node)
    node = ("item", KIND_ROUTING, route.uid)

    ScenePanel._set_isochrone_colour_by(panel, node, "time")
    assert route.isochrone_colour_by == "time"
    assert route.isochrones_visible is True
    assert repaints

    revision = route.revision
    repaints.clear()
    ScenePanel._set_isochrone_colour_by(panel, node, "time")
    assert route.revision == revision
    assert not repaints


def test_legacy_route_offers_only_uniform_isochrone_mode(tmp_path, monkeypatch):
    from sailworks.qt.scenepanel import _isochrone_modes_for

    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result("native", _result(_legs()))
    legacy = Routing(
        "legacy",
        points=[(48.0, -124.0, canonical_utc(EPOCH), 45.0, 12.0, 5.0, 10.0)],
        isochrones=[],
    )

    assert _isochrone_modes_for(route) == ISOCHRONE_COLOUR_MODES
    assert _isochrone_modes_for(legacy) == ("uniform",)

    repaints = []
    panel = SimpleNamespace(
        scene=SimpleNamespace(
            live_entry=lambda kind, key: SimpleNamespace(element=legacy)
        ),
        changed=SimpleNamespace(emit=lambda: repaints.append(("changed",))),
    )
    panel._colour_by_target = lambda node: ScenePanel._colour_by_target(panel, node)
    node = ("item", KIND_ROUTING, legacy.uid)

    ScenePanel._set_isochrone_colour_by(panel, node, "time")
    assert legacy.isochrone_colour_by == "uniform"
    assert not repaints


def test_scene_isochrone_mode_repaints_both_canvases(tmp_path, monkeypatch):
    from sailworks.qt.window import PreviewWindow
    from sailworks.rendering.routingoverlay import RoutingOverlayModel

    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result("native", _result(_legs()))
    native = RoutingOverlayModel(_result(_legs()))

    class Canvas:
        def __init__(self):
            self.isochrone_colour_by = "uniform"
            self.native_routing = native
            self._retained_active = False

        def invalidate_navigation(self):
            pass

        def refresh_route_overlay(self):
            pass

        def invalidate_race_geometry(self):
            pass

        def update(self):
            pass

    window = SimpleNamespace(
        map=Canvas(),
        log_map=Canvas(),
        controller=SimpleNamespace(
            routing=SimpleNamespace(native_overlay=native),
            core=SimpleNamespace(
                routingManager=SimpleNamespace(get_active=lambda: route)
            ),
        ),
        _routing_active_route=route,
    )
    window._native_owns_route = lambda r, n: PreviewWindow._native_owns_route(
        window, r, n
    )

    # The menu action must repaint both canvases through the scene-change path,
    # not merely persist JSON.
    panel = SimpleNamespace(
        scene=SimpleNamespace(
            live_entry=lambda kind, key: SimpleNamespace(element=route)
        ),
        changed=SimpleNamespace(
            emit=lambda: PreviewWindow._sync_isochrone_colour_mode(window)
        ),
    )
    panel._colour_by_target = lambda node: ScenePanel._colour_by_target(panel, node)
    node = ("item", KIND_ROUTING, route.uid)

    ScenePanel._set_isochrone_colour_by(panel, node, "time")

    assert route.isochrone_colour_by == "time"
    assert window.map.isochrone_colour_by == "time"
    assert window.log_map.isochrone_colour_by == "time"
    from sailworks.rendering.routingoverlay import routing_overlay_data

    payload = routing_overlay_data(
        window.map.native_routing, window.map.isochrone_colour_by
    )
    assert payload["isochrone_colour_by"] == "time"


def test_independent_native_result_keeps_presented_isochrone_mode(
    tmp_path, monkeypatch
):
    from sailworks.qt.window import PreviewWindow
    from sailworks.rendering.routingoverlay import RoutingOverlayModel

    native = RoutingOverlayModel(_result(_legs()))

    class Canvas:
        def __init__(self):
            self.isochrone_colour_by = "time"
            self.native_routing = native
            self._retained_active = False

    window = SimpleNamespace(
        map=Canvas(),
        log_map=Canvas(),
        controller=SimpleNamespace(
            routing=SimpleNamespace(native_overlay=native),
            core=SimpleNamespace(
                routingManager=SimpleNamespace(get_active=lambda: None)
            ),
        ),
        _routing_active_route=None,
    )
    window._native_owns_route = lambda r, n: PreviewWindow._native_owns_route(
        window, r, n
    )

    PreviewWindow._sync_isochrone_colour_mode(window)

    assert window.map.isochrone_colour_by == "time"
    assert window.log_map.isochrone_colour_by == "time"


def test_segment_colours_follow_leg_alignment(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result("native", _result(_legs()))
    route.colour_by = "wind_angle"
    overlay = _overlay(route).route_overlay_data(EPOCH + 450)[0]

    # Point i is leg i's start; segment j runs from point j to j + 1 and
    # therefore belongs to leg j.
    assert overlay["segment_colors"] == [
        list(ROUTE_UPWIND_COLOUR[:3]),
        list(ROUTE_DOWNWIND_COLOUR[:3]),
    ]
    assert overlay["point_colors"] == [
        list(ROUTE_UPWIND_COLOUR[:3]),
        list(ROUTE_DOWNWIND_COLOUR[:3]),
        # The final endpoint carries the last leg's class.
        list(ROUTE_DOWNWIND_COLOUR[:3]),
    ]


def test_missing_metrics_render_as_distinct_unknown(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result(
        "native",
        _result(_legs(({"speed_over_ground_knots": 5.0}, None))),
    )
    route.colour_by = "wind_angle"
    overlay = _overlay(route).route_overlay_data(EPOCH + 450)[0]
    assert overlay["segment_colors"] == [
        list(ROUTE_UNKNOWN_COLOUR[:3]),
        list(ROUTE_UNKNOWN_COLOUR[:3]),
    ]

    route.colour_by = "tack"
    overlay = _overlay(route).route_overlay_data(EPOCH + 450)[0]
    assert overlay["segment_colors"] == [
        list(ROUTE_UNKNOWN_COLOUR[:3]),
        list(ROUTE_UNKNOWN_COLOUR[:3]),
    ]
    # Unknown is grey, not silently the first palette entry.
    assert list(ROUTE_UNKNOWN_COLOUR[:3]) != list(ROUTE_PORT_COLOUR[:3])


def test_tack_mode_uses_recorded_tack_not_signed_twa(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    # Port tack with a downwind TWA: inference from the angle would be wrong.
    readouts = (
        dict(UPWIND, true_wind_angle_degrees=150.0, tack="port"),
        dict(DOWNWIND, true_wind_angle_degrees=150.0, tack="starboard"),
    )
    route = collection.save_result("native", _result(_legs(readouts)))
    route.colour_by = "tack"
    overlay = _overlay(route).route_overlay_data(EPOCH + 450)[0]
    assert overlay["segment_colors"] == [
        list(ROUTE_PORT_COLOUR[:3]),
        list(ROUTE_STARBOARD_COLOUR[:3]),
    ]

    missing = dict(DOWNWIND)
    missing.pop("tack")
    readouts = (dict(UPWIND, tack="port-tack"), missing)
    route = collection.save_result("native", _result(_legs(readouts)))
    route.colour_by = "tack"
    overlay = _overlay(route).route_overlay_data(EPOCH + 450)[0]
    assert overlay["segment_colors"] == [
        list(ROUTE_PORT_COLOUR[:3]),
        list(ROUTE_UNKNOWN_COLOUR[:3]),
    ]


def test_speed_mode_keeps_destination_sampling(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result("native", _result(_legs()))
    assert route.colour_by == "speed"
    overlay = _overlay(route).route_overlay_data(EPOCH + 450)[0]
    points = overlay["points"]
    assert overlay["segment_colors"] == [
        list(route_speed_color(points[index + 1][2])[:3])
        for index in range(len(points) - 1)
    ]
    assert overlay["point_colors"] == [
        list(route_speed_color(point[2])[:3]) for point in points
    ]


def test_legend_payload_matches_palette_for_highlighted_route(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result("native", _result(_legs()))
    renderer = _overlay(route)
    renderer.hl_routing = route.name
    route.colour_by = "wind_angle"
    overlay = renderer.route_overlay_data(EPOCH + 450)[0]
    legend = overlay["legend"]
    assert legend["title"] == "Upwind / Downwind"
    labels = [entry[0] for entry in legend["entries"]]
    assert any("90" in label for label in labels)
    entries = dict((label, colour) for label, colour in legend["entries"])
    assert entries["Upwind (<90° TWA)"] == list(ROUTE_UPWIND_COLOUR[:3])
    assert entries["Downwind (≥90° TWA)"] == list(ROUTE_DOWNWIND_COLOUR[:3])
    assert entries["Unknown"] == list(ROUTE_UNKNOWN_COLOUR[:3])
    assert legend["entries"] == [
        [label, list(colour[:3])]
        for label, colour in route_legend_entries("wind_angle")
    ]

    # Only the highlighted saved routing carries a legend.
    renderer.hl_routing = None
    assert "legend" not in renderer.route_overlay_data(EPOCH + 450)[0]


def test_legend_survives_a_highlighted_route_without_drawn_segments():
    # QML draws the legend before its geometry guard, so the payload must not
    # drop it for a route whose points cannot be drawn.
    route = Routing("short", points=[(48.0, -124.0)], isochrones=[])
    renderer = _overlay(route)
    renderer.hl_routing = "short"
    overlay = renderer.route_overlay_data(EPOCH)[0]
    assert overlay["segment_colors"] == []
    # The overlay JSON drops alpha and emits plain [r, g, b] lists, the same
    # contract as point_colors/segment_colors; QML applies its own alpha.
    assert overlay["legend"]["entries"] == [
        [label, list(colour[:3])] for label, colour in route_legend_entries("speed")
    ]
    assert overlay["legend"]["entries"][-1][0] == "12+ kt"


def test_colour_mode_change_invalidates_route_caches(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result("native", _result(_legs()))
    renderer = _overlay(route)
    before = renderer.route_overlay_data(EPOCH + 450)[0]["segment_colors"]
    revision = route.revision

    route.colour_by = "tack"
    assert route.revision > revision
    after = renderer.route_overlay_data(EPOCH + 450)[0]
    assert after["colour_by"] == "tack"
    assert after["segment_colors"] != before

    # The viewport cache key includes the revision, so it rebuilds on change.
    from sailworks.qt.viewportmap import _overlay_routes

    class _Manager(list):
        revision = 0

    host = SimpleNamespace(
        controller=SimpleNamespace(
            core=SimpleNamespace(routingManager=_Manager([route])),
        ),
        navigation=renderer,
    )
    assert _overlay_routes(host, EPOCH)[2][0][1] == route.revision


def test_scene_colour_action_persists_style_and_requests_repaint(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result("native", _result(_legs()))
    repaints = []
    panel = SimpleNamespace(
        scene=SimpleNamespace(
            live_entry=lambda kind, key: SimpleNamespace(element=route)
        ),
        _after_change=lambda *args: repaints.append(args),
        changed=SimpleNamespace(emit=lambda: repaints.append(("changed",))),
    )
    panel._colour_by_target = lambda node: ScenePanel._colour_by_target(panel, node)
    node = ("item", KIND_ROUTING, route.uid)

    ScenePanel._set_colour_by(panel, node, "wind_angle")
    assert route.colour_by == "wind_angle"
    assert repaints

    # Re-selecting the same mode is a no-op and does not bump the revision.
    revision = route.revision
    repaints.clear()
    ScenePanel._set_colour_by(panel, node, "wind_angle")
    assert route.revision == revision
    assert not repaints


class _ToggleRow:
    """The small part of a QTreeWidgetItem the isochrone toggle reads."""

    def __init__(self, node, checked):
        self._node = node
        self._checked = checked

    def data(self, _column, _role):
        return self._node

    def checkState(self, _column):
        return Qt.CheckState.Checked if self._checked else Qt.CheckState.Unchecked


def test_toggling_non_active_route_keeps_selection_and_persists_flag():
    route = Routing(
        "other",
        points=[(48.0, -124.0, canonical_utc(EPOCH), 45.0, 12.0, 5.0, 10.0)],
        isochrones=[],
    )
    saved = []
    signals = []
    panel = SimpleNamespace(
        syncing=False,
        scene=SimpleNamespace(
            live_entry=lambda kind, key: SimpleNamespace(element=route)
        ),
        _save_visible_elements=lambda kinds: saved.append(kinds),
        changed=SimpleNamespace(emit=lambda: signals.append("changed")),
        routing_visibility_changed=SimpleNamespace(
            emit=lambda *args: signals.append("visibility")
        ),
        selected=SimpleNamespace(emit=lambda *args: signals.append("selected")),
    )
    node = ("routing-isochrones", KIND_ROUTING, route.uid)

    ScenePanel._item_changed(panel, _ToggleRow(node, False), 1)

    assert route.isochrones_visible is False
    assert saved and "visibility" in signals
    # Toggling another route must not select it: the isochrones gate only
    # reads the flag, and the active route stays whatever the navigation
    # panel already shows.
    assert "selected" not in signals
    # The flag survives a collection roundtrip, so it is not display state.
    restored = Routing.from_json(route.to_json())
    assert restored.isochrones_visible is False
    # The window's gate applies the flag whenever this route is selected.
    assert (restored.visible and restored.isochrones_visible) is False


def test_failed_tack_read_is_cached_until_explicit_refresh():
    route = Routing("lazy", result_ref=object(), result_summary={})
    route.collection = SimpleNamespace(result_store=object())
    calls = []

    def unavailable(_store):
        calls.append(1)
        raise OSError("artifact unavailable")

    route.load_result = unavailable
    route.colour_by = "tack"

    assert route.leg_tacks() == ()
    assert len(calls) == 1
    assert route.contour_error
    # Repeated frames must not retry artifact I/O.
    assert route.leg_tacks() == ()
    assert len(calls) == 1
    # An explicit mode change is the retry point; a second failure is sticky.
    route.colour_by = "wind_angle"
    assert route.leg_tacks() == ()
    assert len(calls) == 2


def test_cached_tacks_reload_when_result_ref_is_replaced():
    route = Routing("lazy", result_ref=object(), result_summary={})
    route.collection = SimpleNamespace(result_store=object())
    calls = []
    result = SimpleNamespace(
        legs=(SimpleNamespace(readout={"tack": "port"}),),
    )

    def load(_store):
        calls.append(1)
        return result

    route.load_result = load
    assert route.leg_tacks() == ("port",)
    assert route.leg_tacks() == ("port",)
    assert len(calls) == 1

    # A replaced artifact reference invalidates the cached leg tacks.
    route.result_ref = object()
    assert route.leg_tacks() == ("port",)
    assert len(calls) == 2


def test_tack_colours_require_aligned_legs(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result("native", _result(_legs()))
    # A mid-route waypoint insert leaves the recorded per-leg readouts at the
    # old count. The colours must go unknown rather than shift onto the wrong
    # legs, exactly like the sail markers disable on a mismatch.
    route._leg_readouts = (
        {"tack": "port"},
        {"tack": "starboard"},
        {"tack": "port"},
    )
    route._leg_readouts_ref = route.result_ref
    route.colour_by = "tack"

    overlay = _overlay(route).route_overlay_data(EPOCH + 450)[0]

    assert overlay["segment_colors"] == [
        list(ROUTE_UNKNOWN_COLOUR[:3]),
        list(ROUTE_UNKNOWN_COLOUR[:3]),
    ]


def test_failed_isochrone_read_is_cached_until_explicit_refresh():
    route = Routing("lazy", result_ref=object(), result_summary={})
    route._isochrones = None
    route.collection = SimpleNamespace(result_store=object())
    calls = []

    def unavailable(_store):
        calls.append(1)
        raise OSError("artifact unavailable")

    route.load_result = unavailable

    # Repeated frames and visibility toggles must not retry artifact I/O.
    for _toggle in range(3):
        assert route.isochrones == ()
        route.isochrones_visible = not route.isochrones_visible
    assert route.isochrones == ()
    assert len(calls) == 1
    assert "artifact unavailable" in (route.contour_error or "")

    # An explicit refresh clears the failure marker so the next access retries
    # the read exactly once. A retry that fails again keeps the route marked
    # unavailable rather than masking the still-missing artifact.
    route.refresh_isochrones()
    assert route.contour_error
    assert route.isochrones == ()
    assert len(calls) == 2
    assert "artifact unavailable" in (route.contour_error or "")

    # Only a read that actually yields frontiers clears the recorded error.
    def available(_store):
        calls.append(1)
        return SimpleNamespace(
            layers=(
                SimpleNamespace(
                    contours=(
                        SimpleNamespace(
                            points=(SimpleNamespace(lat=48.0, lon=-124.0),),
                        ),
                    ),
                ),
            )
        )

    route.load_result = available
    route.refresh_isochrones()
    assert route.isochrones == [[(48.0, -124.0)]]
    assert route.contour_error is None
    assert len(calls) == 3


def test_profile_identity_falls_back_to_recorded_sail_plan():
    route = Routing("plans", result_ref=object(), result_summary={})
    route.collection = SimpleNamespace(result_store=object())
    route.load_result = lambda _store: SimpleNamespace(
        legs=(
            SimpleNamespace(readout={"sail_plan": 2}),
            SimpleNamespace(readout={"sail_plan": 3.0}),
            SimpleNamespace(readout={"sail_plan": 3, "profile_identity": "J1"}),
        )
    )

    # Absent identity text falls back to the recorded plan selector, so a plan
    # change still marks; recorded identity text wins when present.
    assert route.leg_profile_identities() == ("2", "3", "J1")


def test_profile_identity_stays_unknown_without_identity_or_sail_plan():
    route = Routing("plans", result_ref=object(), result_summary={})
    route.collection = SimpleNamespace(result_store=object())
    route.load_result = lambda _store: SimpleNamespace(
        legs=(
            SimpleNamespace(readout={}),
            SimpleNamespace(readout={"sail_plan": None}),
            SimpleNamespace(readout={"sail_plan": True}),
            SimpleNamespace(readout={"sail_plan": "2"}),
        )
    )

    assert route.leg_profile_identities() == (None, None, None, None)


def test_isochrones_visible_requires_a_boolean():
    payload = Routing("route", [], [], True).to_json()
    payload.pop("isochrones_visible", None)
    assert Routing.from_json(payload).isochrones_visible is True

    for value in ("false", 1):
        payload = Routing("route", [], [], True).to_json()
        payload["isochrones_visible"] = value
        with pytest.raises(SchemaError):
            Routing.from_json(payload)
