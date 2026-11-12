"""Native result polylines must keep the legacy point shape for every consumer."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import sailworks.core.storage as storage
from sailworks.core.geo.routingcollection import RoutingCollection, _result_points
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
from sailworks.rendering.navigation import NavigationRenderer, routing_summary

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000185"
EPOCH = 1767225600
TREE = "a" * 64
READOUT = {
    "true_wind_angle_degrees": 152.6,
    "true_wind_speed_knots": 12.5,
    "speed_over_ground_knots": 5.2,
    "speed_through_water_knots": 4.4,
    "heading_degrees": 43.5,
    "course_over_ground_degrees": 40.0,
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


def _legs(readout=READOUT):
    return (
        _leg("leg-1", (48.0, -124.0), (48.5, -123.5), EPOCH, EPOCH + 900, readout),
        _leg(
            "leg-2",
            (48.5, -123.5),
            (48.8, -123.0),
            EPOCH + 900,
            EPOCH + 2100,
            readout,
        ),
    )


def _collection(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    collection = RoutingCollection(
        result_store=RoutingResultStore(tmp_path / "artifacts")
    )
    collection.save = lambda: None
    return collection


def test_single_leg_point_tuple_carries_recorded_readout():
    leg = _leg("leg-1", (48.2, -123.7), (48.24, -123.66), EPOCH, EPOCH + 900, READOUT)

    assert _result_points(_result((leg,))) == [
        (
            48.2,
            -123.7,
            canonical_utc(EPOCH),
            152.6,
            12.5,
            5.2,
            43.5,
        ),
        (
            48.24,
            -123.66,
            canonical_utc(EPOCH + 900),
            152.6,
            12.5,
            5.2,
            43.5,
        ),
    ]


def test_speed_and_heading_fall_back_to_water_and_course_over_ground():
    leg = _leg(
        "leg-1",
        (48.2, -123.7),
        (48.24, -123.66),
        EPOCH,
        EPOCH + 900,
        {
            "speed_through_water_knots": 4.4,
            "course_over_ground_degrees": 40.0,
        },
    )

    points = _result_points(_result((leg,)))

    assert points[0][3:] == (None, None, 4.4, 40.0)


def test_missing_readout_leaves_fields_absent_not_zero():
    leg = _leg("leg-1", (48.2, -123.7), (48.24, -123.66), EPOCH, EPOCH + 900)

    points = _result_points(_result((leg,)))

    assert [point[:3] for point in points] == [
        (48.2, -123.7, canonical_utc(EPOCH)),
        (48.24, -123.66, canonical_utc(EPOCH + 900)),
    ]
    assert [point[3:] for point in points] == [(None,) * 4, (None,) * 4]


def test_multi_leg_route_uses_each_leg_start_and_the_final_end():
    points = _result_points(_result(_legs()))

    assert len(points) == 3
    assert [point[:3] for point in points] == [
        (48.0, -124.0, canonical_utc(EPOCH)),
        (48.5, -123.5, canonical_utc(EPOCH + 900)),
        (48.8, -123.0, canonical_utc(EPOCH + 2100)),
    ]
    assert [point[3:] for point in points] == [(152.6, 12.5, 5.2, 43.5)] * 3


def test_native_saved_route_has_summary_point_rows_and_speed_coloring(
    tmp_path, monkeypatch
):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QTableWidget

    from sailworks.qt.navigationpanel import NavigationPanel
    from sailworks.qt.window import PreviewWindow

    application = QApplication.instance() or QApplication([])
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result("native", _result(_legs()))

    assert routing_summary(route)
    assert route.points[0][2] == canonical_utc(EPOCH)
    assert route.points[-1][2] == canonical_utc(EPOCH + 2100)

    panel = SimpleNamespace(
        route=route,
        route_points=QTableWidget(0, 7),
        point_rounding=None,
    )
    NavigationPanel.refresh_route_points(panel)
    assert panel.route_points.rowCount() == 3
    assert panel.route_points.item(0, 1).text()
    assert panel.route_points.item(0, 4).text()
    assert panel.route_points.item(0, 5).text()
    assert panel.route_points.item(0, 6).text()

    calls = []

    class FakeMap:
        def __init__(self):
            self.navigation = SimpleNamespace(hl_routing=None)

        def set_route(self, coordinates, speeds):
            calls.append((coordinates, speeds))

    window = SimpleNamespace(
        map=FakeMap(),
        _sync_route_isochrones=lambda: None,
    )
    PreviewWindow._navigation_route_selected(window, route)

    assert calls[0][0] == (
        (48.0, -124.0),
        (48.5, -123.5),
        (48.8, -123.0),
    )
    assert calls[0][1] == (5.2, 5.2, 5.2)
    panel.route_points.deleteLater()
    application.processEvents()


def test_native_saved_route_filters_by_time_and_survives_missing_heading(
    tmp_path, monkeypatch
):
    collection = _collection(tmp_path, monkeypatch)
    route = collection.save_result("native", _result(_legs()))
    renderer = NavigationRenderer(SimpleNamespace(routingManager=[route]))

    overlay = renderer.route_overlay_data(EPOCH + 450)
    assert len(overlay) == 1
    assert len(overlay[0]["points"]) == 3
    assert overlay[0]["boat"] is not None

    # A missing readout still contributes timed geometry; it simply cannot
    # place the interpolated boat because no heading was recorded.
    bare = _leg("leg-1", (48.0, -124.0), (48.5, -123.5), EPOCH, EPOCH + 900)
    bare_route = collection.save_result("bare", _result((bare,)))
    bare_overlay = NavigationRenderer(
        SimpleNamespace(routingManager=[bare_route])
    ).route_overlay_data(EPOCH + 450)
    assert bare_overlay[0]["points"]
    assert bare_overlay[0]["boat"] is None
