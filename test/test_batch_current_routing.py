"""NumPy interpolation and propagation compared with the scalar reference."""

import math
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import numpy as np
from weatherrouting import IsoPoint
import weatherrouting

from gweatherrouting.core.marine.batchrouting import advance_headings
from gweatherrouting.core.marine.chs import DownloadCancelledError
from gweatherrouting.core.marine.fields import FieldSelection
from gweatherrouting.core.marine.routing import (
    InvalidMarineLegError,
    RoutingDataGapError,
)
from test.test_current_routing import START, current_field, setup_router


class TestBatchCurrents(unittest.TestCase):
    def test_batch_interpolation_matches_scalar_values_masks_and_boundaries(self):
        rng = np.random.default_rng(72)
        field = current_field()
        east = rng.normal(size=(2, 2, 2))
        east[0, 0, 0] = np.nan
        field = replace(field, east=east, north=rng.normal(size=(2, 2, 2)))
        points = [tuple(p) for p in rng.uniform(-1.2, 1.2, (300, 2))]
        points += [(1, 1), (-1, -1), (0, 180), (0, math.nan)]
        times = [
            START + timedelta(seconds=float(t))
            for t in rng.uniform(-100, 3700, len(points))
        ]
        times[-4:] = [START] * 4
        for value in (field, replace(field, times=field.times + np.array([0, 3600]))):
            actual = value.sample_many(points, times)
            expected = [value.sample(*p, t) for p, t in zip(points, times)]
            for a, b in zip(actual, expected):
                self.assertEqual(
                    (a.status, a.dataset_id, a.detail),
                    (b.status, b.dataset_id, b.detail),
                )
                np.testing.assert_allclose(a.values, b.values, rtol=1e-13, atol=1e-14)

    def test_batch_source_selection_matches_scalar_fallback(self):
        coarse = replace(
            current_field(east=2),
            latitudes=np.array([-2, 2]),
            longitudes=np.array([-2, 2]),
        )
        fields = [current_field(masked=True), coarse]
        scalar, batch = FieldSelection(fields), FieldSelection(fields)
        points = [(0, 0), (1, 1), (0, 3), (1.5, 1.5)]
        times = [START] * 4
        self.assertEqual(
            batch.sample_many(points, times),
            [scalar.sample(*p, t) for p, t in zip(points, times)],
        )
        self.assertEqual(batch.used, scalar.used)
        self.assertEqual(batch.missing, scalar.missing)

    def test_batch_trajectories_match_scalar_motion_and_provenance(self):
        for field in (
            current_field(east=1, north=-0.2, resolution=500),
            current_field(east=-2, end_east=3, resolution=500),
        ):
            router, _, _ = setup_router(field)
            point = IsoPoint((0, 0), time=START)
            headings = router.headings(point, (0, 0.5))
            scalar = [router.advance(point, 600, h, (0, 0.5)) for h in headings]
            batch, gaps = advance_headings(router, point, 600, headings, (0, 0.5))
            self.assertFalse(gaps)
            self.assertEqual(len(batch), len(scalar))
            for actual, expected in zip(batch, scalar):
                np.testing.assert_allclose(actual.pos, expected.pos, atol=1e-11, rtol=0)
                self.assertLessEqual(
                    abs((actual.time - expected.time).total_seconds()), 1e-5
                )
                self.assertAlmostEqual(actual.speed, expected.speed, places=8)
                self.assertEqual(
                    len(actual.marine_samples), len(expected.marine_samples)
                )
                for a, b in zip(actual.marine_samples, expected.marine_samples):
                    self.assertEqual(a["dataset_id"], b["dataset_id"])
                    self.assertEqual(a["wind_source"], b["wind_source"])
                    np.testing.assert_allclose(
                        (a["latitude"], a["longitude"]),
                        (b["latitude"], b["longitude"]),
                        atol=1e-11,
                        rtol=0,
                    )

    def test_nearest_corner_motion_and_provenance_match_scalar(self):
        field = replace(
            current_field(east=1, masked=True, resolution=500),
            latitudes=np.array([0, 0.004]),
            longitudes=np.array([0, 0.004]),
        )
        router, environment, _ = setup_router(field)
        environment.selection.neighbor_valid = lambda a, b: True
        point = IsoPoint((0.003, 0.003), time=START)
        headings = [0, math.pi / 2]
        scalar = [router.advance(point, 5, h, (0.01, 0.01)) for h in headings]
        batch, gaps = advance_headings(router, point, 5, headings, (0.01, 0.01))
        self.assertFalse(gaps)
        self.assertEqual(len(batch), len(scalar))
        for actual, expected in zip(batch, scalar):
            np.testing.assert_allclose(actual.pos, expected.pos, atol=1e-11, rtol=0)
            self.assertEqual(len(actual.marine_samples), len(expected.marine_samples))
            for a, b in zip(actual.marine_samples, expected.marine_samples):
                self.assertEqual(a["current_sampling"], ("nearest_wet_cell",))
                self.assertEqual(a["current_sampling"], b["current_sampling"])
                self.assertAlmostEqual(
                    a["current_sample_location"]["distance_m"],
                    b["current_sample_location"]["distance_m"],
                    places=4,
                )

    def test_batch_coast_checks_and_endpoint_gaps(self):
        router, env, _ = setup_router(current_field(resolution=500))
        # Reject segments crossing the east side of this synthetic channel.
        router.lines_validity = Mock(
            side_effect=lambda lines: [line[3] < 0.001 for line in lines]
        )
        point = IsoPoint((0, 0), time=START)
        headings = [-math.pi / 2, math.pi / 2]
        scalar = []
        for h in headings:
            try:
                scalar.append(router.advance(point, 600, h, (0, 0.5)))
            except InvalidMarineLegError:
                pass
        router.lines_validity.reset_mock()
        actual, gaps = advance_headings(router, point, 600, headings, (0, 0.5))
        self.assertFalse(gaps)
        self.assertEqual(len(actual), len(scalar))
        np.testing.assert_allclose(actual[0].pos, scalar[0].pos, atol=1e-11)
        self.assertTrue(
            any(len(c.args[0]) > 1 for c in router.lines_validity.call_args_list)
        )
        router.lines_validity = None
        point.time = START + timedelta(minutes=59)
        actual, gaps = advance_headings(router, point, 120, headings, (0, 0.5))
        self.assertFalse(actual)
        self.assertTrue(gaps)
        self.assertTrue(all(g.kind == "current" for g in gaps))
        env.cancel.set()
        with self.assertRaises(DownloadCancelledError):
            advance_headings(router, point, 120, headings, (0, 0.5))

    def test_missing_wind_is_reported_for_batch_candidates(self):
        wind = Mock(
            get_wind_at=Mock(side_effect=lambda t, *_: (0, 10) if t == START else None)
        )
        router, _, _ = setup_router(wind=wind)
        router.use_numpy = True
        with self.assertRaises(RoutingDataGapError) as error:
            router.route(None, START, 1, (0, 0), (0, 0.5))
        self.assertEqual(error.exception.gaps[0].kind, "wind")

    def test_complete_engine_arrival_matches_scalar_reference(self):
        results = []
        for use_numpy in (False, True):
            router, env, polar = setup_router(
                current_field(east=0.5, end_east=1, resolution=500)
            )
            router.use_numpy = use_numpy
            routing = weatherrouting.Routing(
                lambda *args: router,
                polar,
                [(0, 0), (0, 0.02)],
                env.wind,
                START,
            )
            for _ in range(6):
                result = routing.step()
                if routing.end:
                    break
            self.assertTrue(routing.end)
            results.append(result)
        self.assertLessEqual(
            abs((results[0].time - results[1].time).total_seconds()), 1e-5
        )
        self.assertEqual(len(results[0].path), len(results[1].path))
        for a, b in zip(results[0].path, results[1].path):
            np.testing.assert_allclose(a.pos, b.pos, rtol=0, atol=1e-11)

    def test_real_coastline_provider_accepts_batched_segments(self):
        from osgeo import ogr
        from gweatherrouting.rendering.gshhs import GSHHSVectorChart

        data = ogr.GetDriverByName("Memory").CreateDataSource("")
        layer = data.CreateLayer("island", geom_type=ogr.wkbPolygon)
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(
            ogr.CreateGeometryFromWkt(
                "POLYGON ((0.001 -0.01, 0.006 -0.01, 0.006 0.01, 0.001 0.01, 0.001 -0.01))"
            )
        )
        layer.CreateFeature(feature)
        provider = GSHHSVectorChart.__new__(GSHHSVectorChart)
        provider.lpvFile = data
        router, _, _ = setup_router(current_field(resolution=500))
        router.points_validity = provider.points_validity
        router.lines_validity = provider.lines_validity
        point = IsoPoint((0, 0), time=START)
        headings = [-math.pi / 2, math.pi / 2]
        scalar = []
        for heading in headings:
            try:
                scalar.append(router.advance(point, 600, heading, (0, 0.5)))
            except InvalidMarineLegError:
                pass
        actual, gaps = advance_headings(router, point, 600, headings, (0, 0.5))
        self.assertFalse(gaps)
        self.assertEqual(len(actual), 1)
        self.assertEqual(len(scalar), 1)
        np.testing.assert_allclose(actual[0].pos, scalar[0].pos, atol=1e-11, rtol=0)

    def test_coastline_repairs_invalid_land_and_checks_entire_segments(self):
        from osgeo import ogr
        from gweatherrouting.rendering.gshhs import GSHHSVectorChart

        data = ogr.GetDriverByName("Memory").CreateDataSource("")
        layer = data.CreateLayer("land", geom_type=ogr.wkbPolygon)
        for wkt in (
            "POLYGON ((1 1, 2 1, 2 2, 1 2, 1 1))",
            "POLYGON ((5 5, 7 7, 5 7, 7 5, 5 5))",
        ):
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
            layer.CreateFeature(feature)
        provider = GSHHSVectorChart.__new__(GSHHSVectorChart)
        provider.lpvFile = data
        points = [(1.5, 1.5), (0, 0), (5.25, 6), (6, 5.1)]
        expected = [False, True, False, True]
        self.assertEqual(provider.points_validity(points), expected)
        self.assertEqual(provider.points_validity(points), expected)
        self.assertEqual(
            provider.lines_validity([[0, 0, 3, 3], [0, 0, 0, 3], [4, 6, 8, 6]]),
            [False, True, False],
        )
        self.assertEqual(provider.points_validity([]), [])
        self.assertEqual(provider.lines_validity([]), [])

    def test_coastline_clearance_rejects_nearby_segment_in_metres(self):
        from osgeo import ogr
        from gweatherrouting.rendering.gshhs import GSHHSVectorChart

        data = ogr.GetDriverByName("Memory").CreateDataSource("")
        layer = data.CreateLayer("land", geom_type=ogr.wkbPolygon)
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(
            ogr.CreateGeometryFromWkt(
                "POLYGON ((0 -0.01, 0.01 -0.01, 0.01 0, 0 0, 0 -0.01))"
            )
        )
        layer.CreateFeature(feature)
        provider = GSHHSVectorChart.__new__(GSHHSVectorChart)
        provider.lpvFile = data
        line = [[0.001, -0.01, 0.001, 0.02]]
        self.assertEqual(provider.lines_validity(line), [True])
        provider.coastline_clearance_distance_m = 150
        self.assertEqual(provider.lines_validity(line), [False])
