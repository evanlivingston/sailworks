"""Mean-sea-level pressure isobars on the weather GRIB display."""

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest

import cairo
import numpy as np
from osgeo import gdal

from gweatherrouting.core.grib import Grib
from gweatherrouting.rendering.windpainter import WindPainter
from gweatherrouting.rendering.windrenderer import DisplayGrib, WindOverlayRenderer


def make_grib(path, with_pressure=True):
    """6x5 grid with a spatial pressure gradient so contours exist."""
    bands = [(0, "UGRD", 2), (0, "VGRD", 4), (3, "UGRD", 8), (3, "VGRD", 10)]
    if with_pressure:
        pressure = np.fromfunction(
            lambda y, x: 101300 + x * 200 + y * 100, (5, 6), dtype=np.float32
        )
        bands += [(0, "PRMSL", pressure), (3, "PRMSL", pressure + 300)]
    dataset = gdal.GetDriverByName("GTiff").Create(
        path, 6, 5, len(bands), gdal.GDT_Float32
    )
    dataset.SetGeoTransform((-124.5, 1, 0, 50.5, 0, -1))
    for index, (hour, element, value) in enumerate(bands, 1):
        band = dataset.GetRasterBand(index)
        band.SetMetadata(
            {"GRIB_ELEMENT": element, "GRIB_FORECAST_SECONDS": str(hour * 3600)}
        )
        array = (
            value
            if isinstance(value, np.ndarray)
            else np.full((5, 6), value, dtype=np.float32)
        )
        band.WriteArray(array)
    band = None
    dataset = None
    return Grib(path, "gfs", "test", [], datetime(2026, 1, 1), 3)


def fake_viewport():
    return SimpleNamespace(
        get_scale=lambda: 350.0,
        render_cancelled=lambda: False,
        convert_geographic_to_screen_batch=lambda lats, lons: (
            (np.asarray(lons) + 124) * 40 + 10,
            (50 - np.asarray(lats)) * 40 + 10,
        ),
    )


def pressure_columns():
    """Regular 4x3 grid, pressure rising eastward across 4 hPa levels."""
    return [
        [(48.0 + row, -124.0 + col, 1000.0 + col * 4 + row) for row in range(3)]
        for col in range(4)
    ]


class TestPressureData(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = make_grib(str(Path(temporary.name) / "gfs.grib2"))

    def test_pressure_interpolates_between_forecast_hours_in_hpa(self):
        bounds = ((46, -124), (50, -119))
        forecast = self.source.start_time + timedelta(hours=1)
        data = self.source.get_pressure(forecast, bounds)
        self.assertEqual(len(data), 30)
        # Hour 0 value plus one third of the 300 Pa hourly rise, in hPa.
        # Row zero is the northern edge (lat 50) of the test grid.
        lat, lon, hpa = next(point for point in data if point[:2] == (50, -124))
        self.assertEqual((lat, lon), (50, -124))
        self.assertAlmostEqual(hpa, (101300 + 100) / 100, places=4)

    def test_pressure_without_bands_returns_none(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        wind_only = make_grib(
            str(Path(temporary.name) / "wind.grib2"), with_pressure=False
        )
        forecast = wind_only.start_time + timedelta(hours=1)
        self.assertIsNone(wind_only.get_pressure(forecast, ((46, -124), (50, -119))))
        self.assertEqual(wind_only._get_available_pressure_hours(), [])

    def test_display_crop_reads_pressure_without_global_scan(self):
        display = DisplayGrib(self.source)
        bounds = ((48.9, -123.1), (49.1, -121.9))
        forecast = self.source.start_time + timedelta(hours=1)
        self.assertEqual(
            display.get_pressure(forecast, bounds),
            self.source.get_pressure(forecast, bounds),
        )
        self.assertEqual(len(display.sampling.arrays), 2)

    def test_display_pressure_cache_is_bounded(self):
        display = DisplayGrib(self.source)
        bounds = ((46, -124), (50, -119))
        display._find_pressure_band_for_time = lambda hour: SimpleNamespace(
            ReadAsArray=lambda: np.ones((5, 6))
        )
        for hour in range(6):
            display._get_pressure_data(hour, bounds)
        self.assertEqual(len(display._pressure_arrays), 4)


class TestPressureLoading(unittest.TestCase):
    def test_newest_forecast_wins_per_coordinate(self):
        renderer = WindOverlayRenderer(WindPainter)
        old = SimpleNamespace(start_time=datetime(2026, 1, 1))
        new = SimpleNamespace(start_time=datetime(2026, 1, 1, 6))
        old_points = [(0.0, 0.0, 1000.0), (1.0, 0.0, 1002.0), (0.0, 1.0, 1004.0)]
        new_points = [(0.0, 0.0, 1010.0), (1.0, 0.0, 1012.0)]
        renderer._worker_grib = lambda source: SimpleNamespace(
            get_pressure=lambda *_: new_points if source is new else old_points
        )
        job = SimpleNamespace(
            viewport=SimpleNamespace(
                get_bbox=lambda: (
                    SimpleNamespace(get_degrees=lambda: (-1, -1)),
                    SimpleNamespace(get_degrees=lambda: (2, 2)),
                )
            ),
            palette=(datetime(2026, 1, 1),),
            charts=((old,), (new,)),
            cancel=Event(),
        )
        self.assertEqual(
            WindOverlayRenderer._load_pressure(renderer, job),
            [[(0.0, 0.0, 1010.0), (1.0, 0.0, 1012.0)], [(0.0, 1.0, 1004.0)]],
        )

    def test_isobars_render_without_loading_wind(self):
        from unittest.mock import Mock

        painter = Mock()
        renderer = WindOverlayRenderer(lambda *_: painter)
        renderer._load_wind = Mock(side_effect=AssertionError("Wind disabled"))
        renderer._load_pressure = Mock(return_value=pressure_columns())
        job = SimpleNamespace(
            viewport=SimpleNamespace(width=300, height=200),
            palette=(datetime(2026, 1, 1), 0.4, False, True, False, 8, 0.6, False),
            cancel=Event(),
            factor=1,
        )
        self.assertIsNotNone(renderer._render(job))
        renderer._load_wind.assert_not_called()
        renderer._load_pressure.assert_called_once_with(job)
        self.assertEqual(painter.draw.call_args.args[2], [])
        self.assertEqual(painter.draw.call_args.args[3], pressure_columns())

    def test_show_isobars_handles_legacy_palettes(self):
        self.assertTrue(
            WindOverlayRenderer._show_isobars(SimpleNamespace(palette=(1, 2, 3)))
        )
        self.assertFalse(
            WindOverlayRenderer._show_isobars(SimpleNamespace(palette=(1, 2, 3, False)))
        )
        self.assertTrue(
            WindOverlayRenderer._show_isobars(SimpleNamespace(palette=(1, 2, 3, True)))
        )


class TestIsobarPainting(unittest.TestCase):
    def setUp(self):
        self.view = fake_viewport()
        self.settings = SimpleNamespace(
            gribArrowOpacity=0.8, gribArrowOnGround=True, gribIsobarsVisible=True
        )

    def paint(self, pressure):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 300, 200)
        WindPainter(self.settings).draw(self.view, cairo.Context(surface), [], pressure)
        surface.flush()
        return bytes(surface.get_data())

    def test_isobars_draw_lines_and_labels(self):
        self.assertTrue(any(self.paint(pressure_columns())))

    def test_isobars_hidden_by_setting(self):
        self.settings.gribIsobarsVisible = False
        self.assertFalse(any(self.paint(pressure_columns())))

    def test_custom_spacing_snaps_to_requested_interval(self):
        levels = WindPainter._isobar_levels(np.array([1001, 1015]), 8)
        np.testing.assert_array_equal(levels, [1000, 1008, 1016])

    def test_zero_isobar_opacity_hides_lines_and_labels(self):
        self.settings.gribIsobarOpacity = 0
        self.assertFalse(any(self.paint(pressure_columns())))

    def test_labels_can_be_hidden_without_hiding_contours(self):
        from unittest.mock import patch

        self.settings.gribIsobarLabels = False
        with patch.object(WindPainter, "_draw_isobar_labels") as labels:
            self.assertTrue(any(self.paint(pressure_columns())))
        labels.assert_not_called()

    def test_missing_pressure_is_a_no_op(self):
        self.assertFalse(any(self.paint(None)))
        self.assertFalse(any(self.paint([])))

    def test_ragged_columns_do_not_crash(self):
        columns = pressure_columns()
        columns.append([(49.5, -120.0, 1016.0)])
        self.assertTrue(any(self.paint(columns)))

    def test_flat_pressure_draws_no_lines(self):
        flat = [
            [(48.0 + row, -124.0 + col, 1013.0) for row in range(3)] for col in range(4)
        ]
        self.assertFalse(any(self.paint(flat)))


if __name__ == "__main__":
    unittest.main()
