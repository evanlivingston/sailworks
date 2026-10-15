# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""ROMS staggered-grid math on small synthetic grids (no network)."""

import unittest

import numpy as np

from gweatherrouting.core.marine.fields import Area
from gweatherrouting.core.marine.roms import (
    destagger_to_rho,
    locate,
    output_grid,
    prep_window,
    regular_field,
    rotate_to_east_north,
    sample_rho,
)


def rectilinear():
    eta, xi = 5, 6
    lat = 40 + 0.1 * np.arange(eta)[:, None] * np.ones(xi)
    lon = -130 + 0.1 * np.ones((eta, 1)) * np.arange(xi)
    return lat, lon


class TestDestagger(unittest.TestCase):
    def test_uniform_stays_uniform(self):
        u = np.full((4, 5), 2.0)
        v = np.full((3, 6), 3.0)
        u_rho, v_rho, water = destagger_to_rho(u, v)
        self.assertEqual(u_rho.shape, (4, 6))
        self.assertEqual(v_rho.shape, (4, 6))
        np.testing.assert_allclose(u_rho, 2.0)
        np.testing.assert_allclose(v_rho, 3.0)
        self.assertIsNone(water)

    def test_masks_average_to_water(self):
        u = np.ones((2, 3))
        v = np.ones((1, 4))
        _, _, water = destagger_to_rho(
            u, v, mask_u=np.ones((2, 3)), mask_v=np.ones((1, 4))
        )
        np.testing.assert_allclose(water, 1.0)
        _, _, dry = destagger_to_rho(
            u, v, mask_u=np.ones((2, 3)), mask_v=np.zeros((1, 4))
        )
        np.testing.assert_allclose(dry, 0.0)


class TestRotate(unittest.TestCase):
    def test_zero_angle_is_identity(self):
        east, north = rotate_to_east_north(1.0, 2.0, 0.0)
        self.assertAlmostEqual(east, 1.0)
        self.assertAlmostEqual(north, 2.0)

    def test_right_angle_swaps_axes(self):
        east, north = rotate_to_east_north(1.0, 0.0, np.pi / 2)
        self.assertAlmostEqual(east, 0.0)
        self.assertAlmostEqual(north, 1.0)


class TestLocateAndSample(unittest.TestCase):
    def test_cell_centre_is_half_indices(self):
        lat, lon = rectilinear()
        fe, fx = locate(lat, lon, [40.05], [-129.95])
        self.assertAlmostEqual(fe[0], 0.5, places=6)
        self.assertAlmostEqual(fx[0], 0.5, places=6)

    def test_outside_grid_is_nan(self):
        lat, lon = rectilinear()
        fe, fx = locate(lat, lon, [50.0], [-100.0])
        self.assertTrue(np.isnan(fe[0]))

    def test_bilinear_matches_corners(self):
        field = np.arange(30, dtype=float).reshape(5, 6)
        self.assertAlmostEqual(sample_rho(field, [0.0], [0.0])[0], field[0, 0])
        self.assertAlmostEqual(sample_rho(field, [0.5], [0.5])[0], 3.5)

    def test_missing_corners_yield_nan(self):
        field = np.ones((5, 6))
        field[0, 0] = np.nan
        self.assertTrue(np.isnan(sample_rho(field, [0.25], [0.25])[0]))

    def test_masked_nodes_stay_dry(self):
        lat, lon = rectilinear()
        east = np.ones((5, 6))
        north = np.zeros((5, 6))
        water = np.ones((5, 6))
        water[0, 0] = 0.0
        got_east, _ = regular_field(lat, lon, east, north, water, [40.0], [-130.0])
        self.assertTrue(np.isnan(got_east[0]))

    def test_prep_window_rejects_remote_areas(self):
        lat, lon = rectilinear()
        with self.assertRaises(ValueError):
            prep_window(lat, lon, Area(-70, -40, -60, -30))

    def test_output_grid_covers_area(self):
        lats, lons = output_grid(Area(-126, 47, -125, 48), step_deg=0.5)
        self.assertEqual((lats[0], lats[-1]), (47.0, 48.0))
        self.assertEqual((lons[0], lons[-1]), (-126.0, -125.0))


if __name__ == "__main__":
    unittest.main()
