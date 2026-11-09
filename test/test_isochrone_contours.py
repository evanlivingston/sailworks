"""Isochrone contour sidecar, geometry and persistence regressions."""

import io
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import weatherrouting
from pyproj import Geod

from sailworks.core.geo.routing import Routing
from sailworks.core.isochrones import (
    CONTOURS_VERSION,
    CandidateSample,
    ContourRun,
    IsochroneContours,
    IsochroneSlice,
    build_contour_slice,
    hit_test_contours,
    legacy_contours,
)
from sailworks.core.marine.windcontours import contour_observing_router
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


def test_only_the_active_leg_is_rendered_but_all_legs_are_retained():
    contours = IsochroneContours(
        (
            IsochroneSlice(0, 1, "old", (ContourRun(((0.0, 0.0), (0.0, 0.1))),)),
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
    assert [run["valid_time"] for run in data["contours"]["runs"]] == ["old", "new"]
    assert len(contours.slices) == 2
    assert contours.active_leg == 1
    assert [item.valid_time for item in contours.active().slices] == ["new"]


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


def test_scalar_and_numpy_contours_agree_on_topology():
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
        assert len(left.runs) == len(right.runs)
        for a, b in zip(left.runs, right.runs):
            assert a.closed == b.closed
            assert len(a.points) == len(b.points)
            np.testing.assert_allclose(a.points, b.points, atol=1e-11, rtol=0)


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
