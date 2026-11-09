"""Isochrone contour sidecar, geometry and persistence regressions."""

import io
import math
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import weatherrouting
from pyproj import Geod
from shapely.geometry import MultiLineString

from sailworks.core.geo.routing import Routing
from sailworks.core.isochrones import (
    CONTOURS_VERSION,
    CandidateSample,
    ContourRun,
    IsochroneContours,
    IsochroneSlice,
    _split_antimeridian,
    build_contour_slice,
    hit_test_contours,
    legacy_contours,
)
from sailworks.core.marine.windcontours import contour_observing_router
from sailworks.core.routingjob import isochrone_step_hours
from sailworks.rendering.isochrones import (
    IsochroneFrame,
    hit_test_isochrone_frame,
    isochrone_overlay_data,
)
from test.test_current_routing import START, current_field, setup_router

GEOD = Geod(ellps="WGS84")


def weather_router():
    polar = weatherrouting.Polar(
        "unused",
        io.StringIO("TWA 0 5 10 20\n0 0 0 0 0\n45 0 2 4 6\n90 0 3 5 8\n180 0 2 4 7\n"),
    )

    class Grib:
        def get_wind_at(self, *_):
            return 0, 10

    algorithm = contour_observing_router(
        weatherrouting.list_routing_algorithms()[0]["class"]
    )
    return weatherrouting.Routing(
        algorithm,
        polar,
        [(0, 0.1), (0, 0.5)],
        Grib(),
        start_datetime=datetime(2026, 10, 11, 12, tzinfo=timezone.utc),
        start_position=(0, 0),
    )


def test_sidecar_roundtrip_and_unknown_version_degrade():
    run = ContourRun(((0, 0), (0, 0.1)), False, ((0, 1), (0, 2)))
    item = IsochroneSlice(2, 4, "2026-10-11T13:00:00+00:00", (run,))
    contours = IsochroneContours((item,))

    restored = IsochroneContours.from_json(contours.to_json())
    assert restored == contours
    assert restored.version == CONTOURS_VERSION
    assert restored.slices[0].runs[0].points == ((0.0, 0.0), (0.0, 0.1))

    future = dict(contours.to_json(), version=CONTOURS_VERSION + 1)
    unavailable = IsochroneContours.from_json(future)
    assert unavailable.slices == ()
    assert unavailable.version == CONTOURS_VERSION + 1
    assert unavailable.representation == "unavailable"
    assert unavailable.to_json() == future

    earlier = dict(contours.to_json(), version=CONTOURS_VERSION - 1)
    old_route = Routing.from_json(
        dict(Routing("old", [(0, 0)], []).to_json(), isochrone_contours=earlier)
    )
    assert old_route.contours.representation == "unavailable"
    assert old_route.to_json()["isochrone_contours"] == earlier


def test_legacy_route_serialization_never_invents_topology():
    legacy = Routing("Legacy", [(0, 0)], [])
    value = legacy.to_json()
    assert "isochrone_contours" not in value
    assert Routing.from_json(value).contours is None

    saved = legacy_contours([[(0, 0), (0, 1)]])
    assert saved.representation == "legacy-frontier"
    assert all(len(run.points) == 1 for run in saved.runs)


def test_route_persists_and_restores_contours_and_unknown_sidecars_load():
    run = ContourRun(((0, 0), (0, 0.1)))
    contours = IsochroneContours((IsochroneSlice(0, 1, "t", (run,)),))
    route = Routing("Contoured", [(0, 0)], [], contours=contours)
    restored = Routing.from_json(route.to_json())
    assert restored.contours == contours

    value = route.to_json()
    value["isochrone_contours"]["version"] = 99
    loaded = Routing.from_json(value)
    assert loaded.contours.representation == "unavailable"
    assert loaded.points == route.points
    assert loaded.to_json()["isochrone_contours"] == value["isochrone_contours"]


def test_saved_collection_round_trip_preserves_the_sidecar(tmp_path, monkeypatch):
    import json

    import sailworks.core.storage as storage
    from sailworks.core.geo.routingcollection import RoutingCollection

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    contours = IsochroneContours(
        (
            IsochroneSlice(
                0,
                1,
                "t",
                (ContourRun(((0.0, 0.0), (0.0, 0.1))),),
            ),
        )
    )
    route = dict(
        name="contoured",
        points=[[0.0, 0.0]],
        visible=True,
        uid="route-uid",
        provenance=None,
        isochrones=[],
        isochrone_contours=contours.to_json(),
    )
    (tmp_path / "routing.json").write_text(
        json.dumps({"data": {"elements": [route], "activeElement": "contoured"}})
    )
    collection = RoutingCollection()
    assert collection[0].contours == contours
    collection.save()
    saved = json.loads((tmp_path / "routing.json").read_text())
    assert saved["data"]["elements"][0]["isochrone_contours"] == contours.to_json()


def test_missing_heading_slot_is_not_bridged():
    candidates = [
        CandidateSample(0.10, 0.00, 0, 0),
        CandidateSample(0.10, 0.01, 0, 1),
        CandidateSample(0.10, 0.03, 0, 3),
    ]
    item = build_contour_slice([(0, 0)], candidates, leg_id=0, step=1, valid_time="t")
    assert item.representation == "sampled-envelope"
    assert item.runs
    for run in item.runs:
        for first, second in zip(run.points, run.points[1:]):
            assert not (abs(first[1] - 0.01) < 1e-3 and abs(second[1] - 0.03) < 1e-3)
            assert not (abs(second[1] - 0.01) < 1e-3 and abs(first[1] - 0.03) < 1e-3)


def test_nearby_fans_do_not_connect_across_missing_heading_slots():
    candidates = [
        CandidateSample(0.10, 0.00, 0, 0),
        CandidateSample(0.10, 0.01, 0, 1),
        CandidateSample(0.10, 0.03, 0, 3),
        CandidateSample(0.10, 0.04, 0, 4),
    ]
    item = build_contour_slice([(0, 0)], candidates, step=1)

    assert len(item.runs) == 2
    spans = [
        (min(point[1] for point in run.points), max(point[1] for point in run.points))
        for run in item.runs
    ]
    assert all(high < 0.02 or low > 0.02 for low, high in spans)


def test_intersected_parent_spokes_never_become_contour_edges():
    # The second parent's spokes cross the first fan's boundary. Their
    # exposed pieces do not touch a parent vertex, but remain ancestry edges.
    candidates = [
        CandidateSample(0.10, -0.10, 0, 0),
        CandidateSample(0.10, 0.10, 0, 1),
        CandidateSample(0.15, -0.02, 1, 0),
        CandidateSample(0.15, 0.02, 1, 1),
    ]
    item = build_contour_slice([(0, 0), (0.05, 0)], candidates, step=1)

    points = [point for run in item.runs for point in run.points]
    assert points
    assert (0.0, 0.0) not in points
    assert (0.05, 0.0) not in points


def test_overlapping_fans_keep_simple_runs():
    from shapely.geometry import LineString, MultiLineString

    parents = [(0.0, 0.0), (0.0, 0.02)]
    candidates = []
    for parent, offset in ((0, 0.0), (1, 2.0)):
        for heading, angle in enumerate(range(0, 61, 5)):
            radians = np.radians(angle + offset)
            candidates.append(
                CandidateSample(
                    0.10 * math.cos(radians),
                    0.02 * parent + 0.10 * math.sin(radians),
                    parent,
                    heading,
                )
            )
    item = build_contour_slice(parents, candidates, step=1)

    assert item.runs
    segments = MultiLineString(
        [
            [
                (longitude, latitude)
                for latitude, longitude in (
                    run.points + run.points[:1] if run.closed else run.points
                )
            ]
            for run in item.runs
            if len(run.points) >= 2
        ]
    )
    assert segments.is_simple
    assert all(
        LineString(
            [
                (longitude, latitude)
                for latitude, longitude in (
                    run.points + run.points[:1] if run.closed else run.points
                )
            ]
        ).is_simple
        for run in item.runs
        if len(run.points) >= 2
    )


def test_invalid_segments_split_runs_instead_of_bridging():
    candidates = [
        CandidateSample(0.10, 0.00, 0, 0),
        CandidateSample(0.10, 0.01, 0, 1),
        CandidateSample(0.10, 0.02, 0, 2),
    ]
    item = build_contour_slice(
        [(0, 0)],
        candidates,
        leg_id=0,
        step=1,
        valid_time="t",
        lines_validity=lambda lines: [False] * len(lines),
    )
    assert item.runs
    assert all(len(run.points) == 1 for run in item.runs)


def test_antimeridian_runs_do_not_create_world_spanning_edges():
    candidates = [
        CandidateSample(0.00, 179.90, 0, 0),
        CandidateSample(0.02, 179.95, 0, 1),
        CandidateSample(0.04, -179.95, 0, 2),
        CandidateSample(0.06, -179.90, 0, 3),
    ]
    item = build_contour_slice(
        [(0.0, 179.85)], candidates, leg_id=0, step=1, valid_time="t"
    )
    assert item.runs
    for run in item.runs:
        for first, second in zip(run.points, run.points[1:]):
            assert abs(first[1] - second[1]) < 180
            _, _, metres = GEOD.inv(first[1], first[0], second[1], second[0])
            assert metres < 50_000

    pieces = _split_antimeridian(
        [(0.0, 179.9), (0.01, -179.9), (0.02, -179.8)], closed=True
    )
    assert len(pieces) == 2
    assert all(not flag for _, flag in pieces)
    assert not _split_antimeridian(
        [(0.0, -179.9), (0.01, -179.8), (0.02, 179.9)], closed=True
    )[0][1]


def test_hit_test_only_uses_displayed_edges_and_breaks():
    contours = IsochroneContours(
        (
            IsochroneSlice(
                0,
                1,
                "t",
                (
                    ContourRun(((0.0, 0.0), (0.0, 0.1))),
                    ContourRun(((0.0, 1.0), (0.0, 1.1))),
                ),
            ),
        )
    )
    assert hit_test_contours(contours, 0.0, 0.05, 5_000) is not None
    assert hit_test_contours(contours, 0.0, 0.55, 5_000) is None
    assert hit_test_contours(contours, 0.0, 1.05, 5_000) is not None

    frame = IsochroneFrame((), (), (), contours)
    assert hit_test_isochrone_frame(frame, 0.0, 0.55, 5_000) is None
    hit = hit_test_isochrone_frame(frame, 0.0, 0.05, 5_000)
    assert hit is not None and hit.valid_time == "t"


def test_completed_leg_keeps_the_same_visible_contours():
    contours = IsochroneContours(
        (
            IsochroneSlice(0, 1, "first", (ContourRun(((0.0, 0.0), (0.0, 0.1))),)),
            IsochroneSlice(0, 2, "last", (ContourRun(((0.1, 0.0), (0.1, 0.1))),)),
            IsochroneSlice(1, 1, "new", (ContourRun(((1.0, 0.0), (1.0, 0.1))),)),
        )
    )
    frame = SimpleNamespace(
        isochrones=(),
        path=(),
        exclusions=(),
        contours=contours,
        selected=None,
    )
    data = isochrone_overlay_data(frame)
    assert [run["valid_time"] for run in data["contours"]["runs"]] == [
        "first",
        "last",
        "new",
    ]
    assert [
        item.step for item in IsochroneContours(contours.slices[:2]).displayed()
    ] == [
        1,
        2,
    ]
    assert contours.displayed().slices[:2] == contours.slices[:2]
    assert len(contours.slices) == 3
    assert contours.active_leg == 1
    assert [item.valid_time for item in contours.active().slices] == ["new"]
    hit = hit_test_isochrone_frame(frame, 0.0, 0.05, 1_000)
    assert hit is not None and (hit.leg_id, hit.step) == (0, 1)


def test_overlay_and_cairo_data_use_authoritative_runs():
    item = IsochroneSlice(
        0,
        1,
        "t",
        (
            ContourRun(((0.0, 0.0), (0.0, 0.1)), False),
            ContourRun(((0.0, 1.0), (0.0, 1.1)), True),
        ),
    )
    frame = SimpleNamespace(
        isochrones=(((0.0, 0.0), (0.0, 1.0)),),
        path=[],
        exclusions=[],
        contours=IsochroneContours((item,)),
        selected=None,
    )
    data = isochrone_overlay_data(frame)
    assert data["frontiers"] == []
    runs = data["contours"]["runs"]
    assert len(runs) == 2
    assert runs[1]["closed"] is True


def test_enabling_contours_does_not_change_frontiers_or_parents():
    results = []
    for enabled in (False, True):
        router, _, _ = setup_router(
            current_field(east=-1, end_east=2, resolution=500),
            wind=SimpleNamespace(get_wind_at=lambda *_: (0, 10), last_source="test"),
        )
        router.capture_contours = enabled
        previous = None
        for _ in range(3):
            previous = router.route(previous, START, 1, (0, 0), (0, 0.5))
        results.append(previous)
    assert not results[0].contours
    assert results[1].contours
    for left, right in zip(results[1].isochrones, results[0].isochrones):
        assert len(left) == len(right)
        for a, b in zip(left, right):
            assert a.prev_idx == b.prev_idx
            assert a.time == b.time
            np.testing.assert_allclose(a.pos, b.pos, atol=0, rtol=0)


def test_scalar_and_numpy_contours_agree_within_one_local_sample():
    results = []
    for arrays in (False, True):
        router, _, _ = setup_router(
            current_field(east=-1, end_east=2, resolution=500),
            wind=SimpleNamespace(get_wind_at=lambda *_: (0, 10), last_source="test"),
        )
        router.use_numpy = arrays
        previous = None
        for _ in range(3):
            previous = router.route(previous, START, 1, (0, 0), (0, 0.5))
        results.append(router.contour_slices)

    assert len(results[0]) == len(results[1])
    for left, right in zip(results[1], results[0]):
        assert (left.leg_id, left.step) == (right.leg_id, right.step)

        def geometry(item):
            # The parent paths differ slightly across the two routing
            # implementations. A cut at an arc intersection can split their
            # open runs differently; never reconnect them to match run counts.
            return MultiLineString(
                [
                    [
                        (longitude * 111_200, latitude * 111_200)
                        for latitude, longitude in (
                            run.points + run.points[:1] if run.closed else run.points
                        )
                    ]
                    for run in item.runs
                    if len(run.points) >= 2
                ]
            )

        scalar, numpy = geometry(right), geometry(left)
        assert scalar.hausdorff_distance(numpy) < 100  # Under one local sample.
        assert abs(scalar.length - numpy.length) < 600


def test_adaptive_isochrone_step_shortens_as_speed_rises():
    hourly = SimpleNamespace(isochrone_step="hourly")
    adaptive = SimpleNamespace(isochrone_step="adaptive")
    slow = SimpleNamespace(isochrones=[[SimpleNamespace(speed=3.0)]])
    fast = SimpleNamespace(isochrones=[[SimpleNamespace(speed=15.0)]])

    assert isochrone_step_hours(hourly) == 1.0
    slow_step = isochrone_step_hours(adaptive, slow)
    fast_step = isochrone_step_hours(adaptive, fast)
    assert 10 / 60 <= fast_step < slow_step <= 1.0


def test_contour_gap_option_controls_frontier_joins():
    frontier = [(0.0, 0.0), (0.0, 0.05)]
    bearings = [0.0, 1.0]
    split = build_contour_slice(
        [], [], frontier=frontier, frontier_bearings=bearings, gap_m=2200
    )
    joined = build_contour_slice(
        [], [], frontier=frontier, frontier_bearings=bearings, gap_m=6000
    )

    assert len(split.runs) == 2
    assert len(joined.runs) == 1
    assert len(joined.runs[0].points) == 2


def test_wind_only_adapter_preserves_frontiers_and_records_contours():
    observed = weather_router()
    reference_polar = weatherrouting.Polar(
        "unused",
        io.StringIO("TWA 0 5 10 20\n0 0 0 0 0\n45 0 2 4 6\n90 0 3 5 8\n180 0 2 4 7\n"),
    )

    class Grib:
        def get_wind_at(self, *_):
            return 0, 10

    reference = weatherrouting.Routing(
        weatherrouting.list_routing_algorithms()[0]["class"],
        reference_polar,
        [(0, 0.1), (0, 0.5)],
        Grib(),
        start_datetime=datetime(2026, 10, 11, 12, tzinfo=timezone.utc),
        start_position=(0, 0),
    )
    for _ in range(2):
        actual = observed.step()
        expected = reference.step()
        assert len(actual.isochrones[-1]) == len(expected.isochrones[-1])
        for left, right in zip(actual.isochrones[-1], expected.isochrones[-1]):
            np.testing.assert_allclose(left.pos, right.pos, atol=1e-12, rtol=0)

    assert observed.algorithm.contour_slices
    assert all(
        item.representation == "sampled-envelope"
        for item in observed.algorithm.contour_slices
    )
