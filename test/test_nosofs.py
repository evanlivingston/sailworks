# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""NOS OFS provider against a tiny synthetic ROMS file (no network)."""

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from netCDF4 import Dataset

from gweatherrouting.core.marine import nosofs
from gweatherrouting.core.marine.fields import Area
from gweatherrouting.core.marine.nosofs import NOSOFSProvider

START = datetime(2026, 10, 15, 4, tzinfo=timezone.utc)


def make_roms(path, hours):
    eta, xi = 6, 8
    ds = Dataset(path, "w")
    ds.createDimension("ocean_time", 1)
    ds.createDimension("eta_rho", eta)
    ds.createDimension("xi_rho", xi)
    ds.createDimension("eta_u", eta)
    ds.createDimension("xi_u", xi - 1)
    ds.createDimension("eta_v", eta - 1)
    ds.createDimension("xi_v", xi)
    lat = 44 + 0.1 * np.arange(eta)[:, None] * np.ones(xi)
    lon = -126 + 0.1 * np.ones((eta, 1)) * np.arange(xi)
    for name, values in (
        ("lat_rho", lat),
        ("lon_rho", lon),
        ("angle", np.zeros((eta, xi))),
        ("mask_rho", np.ones((eta, xi))),
    ):
        var = ds.createVariable(name, "f8", ("eta_rho", "xi_rho"))
        var[:] = values
    time_var = ds.createVariable("ocean_time", "f8", ("ocean_time",))
    time_var.units = "seconds since 2026-10-15 00:00:00"
    time_var[:] = [hours * 3600]
    u = ds.createVariable("u_sur", "f4", ("ocean_time", "eta_u", "xi_u"))
    v = ds.createVariable("v_sur", "f4", ("ocean_time", "eta_v", "xi_v"))
    u[:] = 1.0
    v[:] = 0.5
    ds.close()


FAKE_FILES = {}


def fake_catalog(url):
    if url.endswith("MODELS/catalog.xml"):
        return set(), {"2026/catalog.xml"}
    if url.endswith("/2026/catalog.xml"):
        return set(), {"10/catalog.xml"}
    if url.endswith("/2026/10/catalog.xml"):
        return set(), {"15/catalog.xml"}
    if url.endswith("/2026/10/15/catalog.xml"):
        return {
            "NOAA/WCOFS/MODELS/2026/10/15/wcofs.t03z.20261015.2ds.f000.nc",
            "NOAA/WCOFS/MODELS/2026/10/15/wcofs.t03z.20261015.2ds.f001.nc",
            "NOAA/WCOFS/MODELS/2026/10/15/wcofs.t03z.20261015.2ds.f002.nc",
            "NOAA/WCOFS/MODELS/2026/10/15/wcofs.t03z.20261015.2ds.f004.nc",
            "NOAA/WCOFS/MODELS/2026/10/15/wcofs.t03z.20261015.2ds.f005.nc",
        }, set()
    return set(), set()


class FakeDataset:
    """netCDF4.open proxy routing each forecast hour to its own file."""

    def __init__(self, url, *args, **kwargs):
        import re

        hour = int(re.search(r"\.f(\d+)\.nc$", url).group(1))
        self._inner = Dataset(FAKE_FILES[hour], "r")

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self):
        self._inner.close()


class TestNOSOFSProvider(unittest.TestCase):
    def test_download_builds_regular_field(self):
        with tempfile.TemporaryDirectory() as directory:
            FAKE_FILES.clear()
            for hour in (0, 1, 2, 4, 5):
                path = str(Path(directory) / f"wcofs_f{hour:03d}.nc")
                make_roms(path, hour + 3)
                FAKE_FILES[hour] = path
            client = SimpleNamespace(cancel=threading.Event(), sources=[])
            with patch.object(nosofs, "_catalog", side_effect=fake_catalog):
                with patch.object(nosofs, "Dataset", FakeDataset):
                    field = NOSOFSProvider().download(
                        "wcofs",
                        Area(-125.9, 44.0, -125.3, 44.4),
                        START,
                        START + timedelta(hours=1),
                        client,
                    )
        self.assertEqual(
            field.times.tolist(),
            [(START + timedelta(hours=n)).timestamp() for n in (0, 1)],
        )
        self.assertEqual(
            field.east.shape, (2, field.latitudes.size, field.longitudes.size)
        )
        self.assertTrue(np.isfinite(field.east).all())
        # 1 m/s grid-east with zero rotation stays grid-east.
        self.assertAlmostEqual(float(np.nanmean(field.east)), 1.0, places=5)
        self.assertAlmostEqual(float(np.nanmean(field.north)), 0.5, places=5)

    def test_window_outside_grid_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            FAKE_FILES.clear()
            for hour in (0, 1, 2):
                path = str(Path(directory) / f"wcofs_f{hour:03d}.nc")
                make_roms(path, hour + 3)
                FAKE_FILES[hour] = path
            client = SimpleNamespace(cancel=threading.Event(), sources=[])
            with patch.object(nosofs, "_catalog", side_effect=fake_catalog):
                with patch.object(nosofs, "Dataset", FakeDataset):
                    with self.assertRaises(ValueError):
                        NOSOFSProvider().download(
                            "wcofs",
                            Area(-70, -40, -60, -30),
                            START,
                            START + timedelta(hours=1),
                            client,
                        )


if __name__ == "__main__":
    unittest.main()
