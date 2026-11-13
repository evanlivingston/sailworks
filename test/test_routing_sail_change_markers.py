"""Sail-plan change markers drawn from recorded per-leg data."""

from __future__ import annotations

from types import SimpleNamespace

import sailworks.core.storage as storage
from sailworks.core.geo.routing import Routing
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
from sailworks.rendering.navigation import NavigationRenderer, sail_change_indices
from sailworks.rendering.style import ROUTE_SAIL_CHANGE_COLOUR

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000193"
EPOCH = 1767225600
TREE = "b" * 64


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


def _result(legs, calculation=CALCULATION):
    return RoutingResult(
        calculation_id=calculation,
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


def _legs(identities):
    """Three-leg route with one recorded plan identity per leg."""
    points = ((48.0, -124.0), (48.5, -123.5), (48.8, -123.0), (49.0, -122.5))
    legs = []
    for index, identity in enumerate(identities):
        readout = None if identity is None else {"profile_identity": identity}
        legs.append(
            _leg(
                f"leg-{index + 1}",
                points[index],
                points[index + 1],
                EPOCH + 900 * index,
                EPOCH + 900 * (index + 1),
                readout,
            )
        )
    return tuple(legs)


def _collection(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    collection = RoutingCollection(
        result_store=RoutingResultStore(tmp_path / "artifacts")
    )
    collection.save = lambda: None
    return collection


def _overlay(*routes):
    return NavigationRenderer(SimpleNamespace(routingManager=list(routes)))


def test_detection_needs_two_known_differing_plans():
    # Identities are per leg; point i is the boundary between leg i-1 and i.
    assert sail_change_indices(("inshore", "offshore")) == (1,)
    assert sail_change_indices(("inshore", "inshore")) == ()
    assert sail_change_indices(("inshore", "offshore", "inshore")) == (1, 2)
    assert sail_change_indices(("inshore",)) == ()


def test_detection_treats_unknown_as_never_a_change():
    # The first leg has no preceding leg, so it can never be a boundary.
    assert sail_change_indices((None, "offshore")) == ()
    assert sail_change_indices(("inshore", None)) == ()
    assert sail_change_indices(("inshore", "", "offshore")) == ()
    assert sail_change_indices(("inshore", "offshore", None)) == (1,)
    assert sail_change_indices(()) == ()


def test_leg_profile_identities_come_from_recorded_readouts(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result(
        "native", _result(_legs(("inshore", None, "offshore")))
    )

    # Missing readouts stay unknown rather than becoming an invented plan.
    assert route.leg_profile_identities() == ("inshore", None, "offshore")


def test_payload_marks_every_visible_route_and_legends_the_highlighted_one(
    tmp_path, monkeypatch
):
    collection = _collection(tmp_path, monkeypatch)
    changing = collection.save_result(
        "changing", _result(_legs(("inshore", "offshore", "offshore")))
    )
    constant = collection.save_result(
        "constant",
        _result(
            _legs(("inshore", "inshore", "inshore")),
            calculation="6f1c1a1e-0000-4000-8000-000000000194",
        ),
    )
    renderer = _overlay(changing, constant)
    # Visible routes draw waypoint dots whatever the highlight; markers follow
    # that convention. Only the highlighted route carries a legend.
    renderer.hl_routing = changing.name

    payload = {route["name"]: route for route in renderer.route_overlay_data(EPOCH)}
    assert payload["changing"]["sail_changes"] == [1]
    assert payload["constant"]["sail_changes"] == []
    labels = [entry[0] for entry in payload["changing"]["legend"]["entries"]]
    assert "Sail change" in labels
    assert "legend" not in payload["constant"]
    assert payload["changing"]["sail_change_color"] == list(
        ROUTE_SAIL_CHANGE_COLOUR[:3]
    )


def test_hidden_route_payload_has_no_markers(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result("changing", _result(_legs(("inshore", "offshore"))))
    route.visible = False

    assert _overlay(route).route_overlay_data(EPOCH) == []


def test_legacy_route_has_no_sail_change_markers():
    route = Routing(
        "legacy",
        points=[
            (48.0, -124.0, canonical_utc(EPOCH), 45.0, 12.0, 5.0, 10.0),
            (48.5, -123.5, canonical_utc(EPOCH + 900), 45.0, 12.0, 5.0, 10.0),
        ],
    )

    payload = _overlay(route).route_overlay_data(EPOCH)[0]
    assert payload["sail_changes"] == []


def test_markers_rebuild_when_the_referenced_artifact_changes(tmp_path, monkeypatch):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result("native", _result(_legs(("inshore", "inshore"))))
    renderer = _overlay(route)
    assert renderer.route_overlay_data(EPOCH)[0]["sail_changes"] == []

    # The replacement artifact must still satisfy the route's verification
    # summary; same calculation, different legs, so only the recorded plans
    # differ and the test isolates readout-cache invalidation.
    replacement = collection.save_result(
        "replacement",
        _result(_legs(("inshore", "offshore"))),
    )
    route.result_ref = replacement.result_ref
    route.result_summary = replacement.result_summary
    route.touch()

    assert route.leg_profile_identities() == ("inshore", "offshore")
    assert renderer.route_overlay_data(EPOCH)[0]["sail_changes"] == [1]
