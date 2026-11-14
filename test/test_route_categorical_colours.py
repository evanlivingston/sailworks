"""Sail-plan and propulsion route dimensions: payload, guards, menu reasons."""

from __future__ import annotations

from types import SimpleNamespace

import sailworks.core.storage as storage
from sailworks.core.geo.routing import ROUTE_COLOUR_MODES, Routing
from sailworks.core.geo.routingcollection import RoutingCollection
from sailworks.core.marine.routingrecord import (
    Capabilities,
    EngineIdentity,
    Leg,
    Outcome,
    Point,
    Provenance,
    Validation,
)
from sailworks.core.marine.routingresult import RoutingResult, canonical_utc
from sailworks.core.marine.routingresultstore import RoutingResultStore
from sailworks.core.scene import KIND_ROUTING
from sailworks.qt.scenepanel import ScenePanel, route_metric_reason
from sailworks.rendering import palettes
from sailworks.rendering.navigation import NavigationRenderer
from sailworks.rendering.style import ROUTE_SAIL_CHANGE_COLOUR, ROUTE_UNKNOWN_COLOUR

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000197"
EPOCH = 1767225600
TREE = "c" * 64


def _leg(leg_id, start, end, start_s, end_s, readout):
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


def _result(readouts):
    legs = tuple(
        _leg(
            f"leg-{index}",
            (48.0 + index * 0.1, -124.0),
            (48.1 + index * 0.1, -123.9),
            EPOCH + index * 900,
            EPOCH + (index + 1) * 900,
            readout,
        )
        for index, readout in enumerate(readouts)
    )
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
        legs=legs,
        layers=(),
        capabilities=Capabilities("synthetic", "absent", "sampled", 12, 1800),
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


def _legacy_route():
    return Routing(
        "legacy",
        points=[
            (48.0, -124.0, canonical_utc(EPOCH), 45.0, 12.0, 5.0, 10.0),
            (48.4, -123.6, canonical_utc(EPOCH + 900), 150.0, 12.0, 9.0, 200.0),
        ],
        isochrones=[],
    )


def test_sail_plan_is_a_persisted_route_dimension(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result(
        "native",
        _result(
            (
                {"profile_identity": "J1"},
                {"profile_identity": "J2"},
            )
        ),
    )
    assert "sail_plan" in ROUTE_COLOUR_MODES
    route.colour_by = "sail_plan"
    payload = route.to_json()
    assert payload["colour_by"] == "sail_plan"
    assert Routing.from_json(payload).colour_by == "sail_plan"


def test_sail_plan_payload_has_metric_colours_and_legend(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result(
        "native",
        _result(
            (
                {"profile_identity": "J1"},
                {"profile_identity": "J2"},
            )
        ),
    )
    renderer = _overlay(route)
    renderer.hl_routing = route.name
    route.colour_by = "sail_plan"

    overlay = renderer.route_overlay_data(EPOCH + 450)[0]

    cycle = palettes.METRICS["sail_plan"].cycle
    assert overlay["metric"] == "sail_plan"
    assert overlay["colour_by"] == "sail_plan"
    assert overlay["segment_colors"] == [list(cycle[0][:3]), list(cycle[1][:3])]
    assert overlay["point_colors"] == [
        list(cycle[0][:3]),
        list(cycle[1][:3]),
        list(cycle[1][:3]),
    ]
    assert overlay["legend"]["title"] == "Sail plan"
    # The plan swatches carry the recorded identities; the marker square is
    # still explained because every mode draws it on the route.
    assert overlay["legend"]["entries"] == [
        ["J1", list(cycle[0][:3])],
        ["J2", list(cycle[1][:3])],
        ["Unknown", list(ROUTE_UNKNOWN_COLOUR[:3])],
        ["Sail change", list(ROUTE_SAIL_CHANGE_COLOUR[:3])],
    ]


def test_sail_plan_colour_is_stable_across_reopen(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result(
        "native",
        _result(
            (
                {"profile_identity": "J2"},
                {"profile_identity": "J1"},
                {"profile_identity": "J2"},
            )
        ),
    )
    route.colour_by = "sail_plan"
    before = _overlay(route).route_overlay_data(EPOCH + 450)[0]["segment_colors"]

    restored = Routing.from_json(route.to_json())
    restored.collection = collection
    after = _overlay(restored).route_overlay_data(EPOCH + 450)[0]["segment_colors"]

    assert after == before
    # J2 appears first, so it owns the first cycle colour even after reopen.
    assert before[0] == list(palettes.METRICS["sail_plan"].cycle[0][:3])


def test_propulsion_payload_uses_recorded_modes(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result(
        "native",
        _result(
            (
                {"propulsion_mode": "Sail"},
                {"propulsion_mode": "Motor"},
                {"propulsion_mode": "MotorSail"},
            )
        ),
    )
    route.colour_by = "propulsion"
    renderer = _overlay(route)
    renderer.hl_routing = route.name
    overlay = renderer.route_overlay_data(EPOCH + 450)[0]

    categories = {
        key: colour for key, colour, _label in palettes.METRICS["propulsion"].categories
    }
    assert overlay["metric"] == "propulsion"
    assert overlay["segment_colors"] == [
        list(categories["sail"][:3]),
        list(categories["motor"][:3]),
        list(categories["motorsail"][:3]),
    ]
    assert [entry[0] for entry in overlay["legend"]["entries"]] == [
        "Sail",
        "Motor",
        "Motor sail",
        "Unknown",
    ]


def test_missing_categorical_values_render_unknown(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result(
        "native",
        _result(({}, {"profile_identity": None, "propulsion_mode": None})),
    )
    for mode in ("sail_plan", "propulsion"):
        route.colour_by = mode
        overlay = _overlay(route).route_overlay_data(EPOCH + 450)[0]
        assert overlay["segment_colors"] == [
            list(ROUTE_UNKNOWN_COLOUR[:3]),
            list(ROUTE_UNKNOWN_COLOUR[:3]),
        ], mode


def test_legacy_route_categorical_modes_render_unknown():
    route = _legacy_route()
    for mode in ("sail_plan", "propulsion"):
        route.colour_by = mode
        overlay = _overlay(route).route_overlay_data(EPOCH + 450)[0]
        assert overlay["metric"] == mode
        assert overlay["segment_colors"] == [list(ROUTE_UNKNOWN_COLOUR[:3])]


def test_categorical_colours_require_aligned_legs(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result(
        "native",
        _result(({"profile_identity": "J1"}, {"profile_identity": "J2"})),
    )
    # A mid-route point insert leaves the recorded readouts at the old count.
    route._leg_readouts = (
        {"profile_identity": "J1"},
        {"profile_identity": "J2"},
        {"profile_identity": "J1"},
    )
    route._leg_readouts_ref = route.result_ref
    route.colour_by = "sail_plan"

    overlay = _overlay(route).route_overlay_data(EPOCH + 450)[0]

    assert overlay["segment_colors"] == [
        list(ROUTE_UNKNOWN_COLOUR[:3]),
        list(ROUTE_UNKNOWN_COLOUR[:3]),
    ]


def test_propulsion_read_uses_the_cached_leg_path():
    route = Routing("lazy", result_ref=object(), result_summary={})
    route.collection = SimpleNamespace(result_store=object())
    calls = []

    def load(_store):
        calls.append(1)
        return SimpleNamespace(
            legs=(
                SimpleNamespace(readout={"propulsion_mode": "Sail"}),
                SimpleNamespace(readout={"propulsion_mode": "Motor"}),
            )
        )

    route.load_result = load
    assert route.leg_propulsion_modes() == ("Sail", "Motor")
    assert route.leg_propulsion_modes() == ("Sail", "Motor")
    assert len(calls) == 1


def test_menu_offers_every_registry_route_metric_with_reasons(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result(
        "native",
        _result(({"profile_identity": "J1"}, {"tack": "port"})),
    )
    legacy = _legacy_route()

    assert palettes.route_metric_ids() == ROUTE_COLOUR_MODES
    # Recorded dimensions are usable where the data exists.
    assert route_metric_reason(route, "speed") is None
    assert route_metric_reason(route, "wind_angle") is None
    assert route_metric_reason(route, "sail_plan") is None
    # Propulsion was not recorded in this result: disabled with a reason.
    reason = route_metric_reason(route, "propulsion")
    assert reason and "no recorded values" in reason

    # A legacy route has no artifact, so the recorded dimensions are explained
    # rather than hidden.
    for mode in ("tack", "sail_plan", "propulsion"):
        reason = route_metric_reason(legacy, mode)
        assert reason and "saved result" in reason


def test_scene_refuses_an_unavailable_dimension(tmp_path, monkeypatch):
    legacy = _legacy_route()
    repaints = []
    panel = SimpleNamespace(
        scene=SimpleNamespace(
            live_entry=lambda kind, key: SimpleNamespace(element=legacy)
        ),
        changed=SimpleNamespace(emit=lambda: repaints.append(("changed",))),
    )
    panel._colour_by_target = lambda node: ScenePanel._colour_by_target(panel, node)
    node = ("item", KIND_ROUTING, legacy.uid)

    ScenePanel._set_colour_by(panel, node, "sail_plan")
    assert legacy.colour_by == "speed"
    assert not repaints

    ScenePanel._set_colour_by(panel, node, "wind_angle")
    assert legacy.colour_by == "wind_angle"
    assert repaints
