"""Coastal acceleration matches scalar sampling and exact OGR predicates."""

import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

import numpy as np
from osgeo import ogr

from gweatherrouting.core.marine.fields import FieldSelection
from gweatherrouting.gtk.charts.gshhs import GSHHSVectorChart
from test.test_marine_fields import START, field
from test.test_current_routing import setup_router, current_field
from gweatherrouting.core.marine.chs import DownloadCancelledError


class TestCoastalBatch(unittest.TestCase):
    def test_neighbor_batches_preserve_all_four_validity_hooks(self):
        router, _, _ = setup_router(current_field())
        router.points_validity = lambda ps: ps[:, 0] < 0.8
        router.point_validity = lambda a, b: b > -0.8
        router.lines_validity = lambda ls: ls[:, 3] < 0.7
        router.line_validity = lambda a, b, c, d: abs(c - a) < 0.6
        starts = np.array([[0, 0], [0.9, 0], [0, -0.9], [0, 0], [0, 0]])
        ends = np.array([[0.2, 0.2], [0, 0], [0, 0], [0, 0.9], [0.7, 0]])
        # Scalar hooks also receive ordinary lists on the reference path.
        router.points_validity = lambda ps: np.asarray(ps)[:, 0] < 0.8
        router.lines_validity = lambda ls: np.asarray(ls)[:, 3] < 0.7
        expected = [router.valid_current_neighbor(a, b) for a, b in zip(starts, ends)]
        self.assertEqual(
            router.valid_current_neighbors(starts, ends).tolist(), expected
        )

    def test_batched_fallback_observes_cancellation(self):
        value = replace(
            field(masked=True),
            latitudes=np.array([49, 49.004]),
            longitudes=np.array([-126, -125.996]),
        )
        selection = FieldSelection(
            [value], lambda *_: True, lambda a, b: np.ones(len(a), dtype=bool)
        )
        from gweatherrouting.core.marine.nearestbatch import resolve_nearest

        cancel = Mock(side_effect=DownloadCancelledError)
        with self.assertRaises(DownloadCancelledError):
            resolve_nearest(
                selection,
                np.array([[49.003, -125.997]]),
                np.array([START.timestamp()]),
                np.array([0]),
                cancel,
            )

    def test_batched_nearest_matches_scalar_source_corner_and_provenance(self):
        rng = np.random.default_rng(173)
        original = field(masked=True)
        fine = replace(
            original,
            latitudes=np.array([49, 49.004]),
            longitudes=np.array([-126, -125.996]),
        )
        east, north = fine.east.copy(), fine.north.copy()
        east[1] *= 3
        north[1] *= 3
        fine = replace(fine, east=east, north=north)
        other = replace(fine, east=east * 2, north=north * 2)
        positions = rng.uniform([48.999, -126.001], [49.005, -125.995], (250, 2))
        positions = np.vstack(
            (
                positions,
                [[49, -126], [49.004, -125.996], [49.002, -125.998], [49.003, 234.003]],
            )
        )
        times = [
            START + timedelta(seconds=float(t))
            for t in rng.uniform(0, 3600, len(positions))
        ]

        def valid(a, b):
            return a[0] < 49.0038 and b[0] > 49.002

        batched = Mock(side_effect=lambda a, b: [valid(x, y) for x, y in zip(a, b)])
        scalar = FieldSelection([fine, other], valid)
        batch = FieldSelection([fine, other], valid, batched)
        expected = [scalar.sample(*p, t) for p, t in zip(positions, times)]
        self.assertEqual(batch.sample_many(positions, times), expected)
        self.assertEqual(batch.used, scalar.used)
        self.assertEqual(batch.missing, scalar.missing)
        self.assertTrue(any(len(c.args[0]) > 1 for c in batched.call_args_list))

    def test_batched_nearest_does_not_bridge_missing_forecast_times(self):
        value = replace(
            field(masked=True),
            latitudes=np.array([49, 49.004]),
            longitudes=np.array([-126, -125.996]),
            times=np.array([START.timestamp(), START.timestamp() + 7200]),
        )
        scalar = FieldSelection([value], lambda *_: True)
        batch = FieldSelection(
            [value], lambda *_: True, lambda a, b: np.ones(len(a), dtype=bool)
        )
        points = [(49.003, -125.997)] * 3
        times = [START, START + timedelta(minutes=30), START + timedelta(hours=2)]
        self.assertEqual(
            batch.sample_many(points, times),
            [scalar.sample(*p, t) for p, t in zip(points, times)],
        )
        self.assertEqual(batch.missing, scalar.missing)

    def test_native_predicates_match_ogr_holes_boundaries_and_zero_length_segments(
        self,
    ):
        data = ogr.GetDriverByName("Memory").CreateDataSource("")
        layer = data.CreateLayer("land", geom_type=ogr.wkbPolygon)
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(
            ogr.CreateGeometryFromWkt(
                "POLYGON ((0 0, 2 0, 2 2, 0 2, 0 0), "
                "(0.5 0.5, 0.5 1.5, 1.5 1.5, 1.5 0.5, 0.5 0.5))"
            )
        )
        layer.CreateFeature(feature)
        provider = GSHHSVectorChart.__new__(GSHHSVectorChart)
        provider.lpvFile = data
        rng = np.random.default_rng(571)
        points = np.vstack(
            (rng.uniform(-1, 3, (300, 2)), [[0, 0], [0, 1], [1, 0.5], [1, 1], [2, 2]])
        )
        lines = np.vstack(
            (
                rng.uniform(-1, 3, (300, 4)),
                [
                    [0, 0, 0, 0],
                    [1, 1, 1, 1],
                    [0, 0, 2, 0],
                    [1, -1, 1, 3],
                    [1, 0.5, 1, 1.5],
                ],
            )
        )
        provider.use_native_geometry = False
        expected_points = provider.points_validity(points)
        expected_lines = provider.lines_validity(lines)
        provider.use_native_geometry = True
        self.assertEqual(provider.points_validity(points), expected_points)
        self.assertEqual(provider.lines_validity(lines), expected_lines)
        # An existing installation can still use exact OGR checks without Shapely.
        with patch("gweatherrouting.rendering.gshhs.shapely", None):
            self.assertEqual(provider.lines_validity(lines), expected_lines)
