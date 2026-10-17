"""Raster chart coverage axis order and native-scale metadata."""

import unittest
from os.path import join
from tempfile import TemporaryDirectory
from types import MethodType, SimpleNamespace

from osgeo import gdal, osr

from gweatherrouting.rendering.chartcollection import ChartCollection
from gweatherrouting.rendering.gdalrasterchart import (
    GDALRasterChart,
    GDALSingleRasterChart,
    dataset_bbox,
)


def make_raster(path, epsg, geotransform, size=50):
    """Write a paletted GeoTIFF with the given geotransform and CRS."""
    dataset = gdal.GetDriverByName("GTiff").Create(path, size, size, 1)
    dataset.SetGeoTransform(geotransform)
    spatial = osr.SpatialReference()
    spatial.ImportFromEPSG(epsg)
    dataset.SetProjection(spatial.ExportToWkt())
    colors = gdal.ColorTable()
    colors.SetColorEntry(0, (0, 0, 0, 0))
    colors.SetColorEntry(1, (10, 20, 30, 255))
    dataset.GetRasterBand(1).SetRasterColorTable(colors)
    dataset = None
    return path


def utm_geotransform(top_lat, top_lon, span_deg, size):
    """A UTM 10N geotransform whose top-left sits at (top_lat, top_lon)."""
    geographic = osr.SpatialReference()
    geographic.ImportFromEPSG(4326)
    utm = osr.SpatialReference()
    utm.ImportFromEPSG(32610)
    x, y, *_ = osr.CoordinateTransformation(geographic, utm).TransformPoint(
        top_lat, top_lon
    )
    import math

    metres = span_deg * 111320
    pixel = metres / size
    # Shorten longitude metres by cos(lat) so the footprint stays ~square.
    pixel *= math.cos(math.radians(top_lat))
    return (x, pixel, 0, y, 0, -metres / size)


def raster_chart(path, scale=None):
    """A GDALRasterChart carrying freshly read file metadata, without pixels loaded."""
    chart = GDALRasterChart.__new__(GDALRasterChart)
    chart.path = path
    chart.ctype = "raster"
    chart.enabled = True
    info = GDALSingleRasterChart.get_file_info(path)
    chart.metadata = [info[:4] if scale is None else info]
    if scale is not None:
        chart._native_scale = scale
    return chart


def select(lat, lon, scale, charts):
    """Run ChartCollection.detailed_chart_at over a standalone chart list."""
    manager = SimpleNamespace(charts=list(charts))
    manager.detailed_chart_at = MethodType(ChartCollection.detailed_chart_at, manager)
    return manager.detailed_chart_at(lat, lon, scale)


class TestBboxAxisOrder(unittest.TestCase):
    """dataset_bbox must report (lat, lon) for every raster CRS."""

    def test_geographic_raster_reports_lat_lon(self):
        with TemporaryDirectory() as directory:
            path = make_raster(
                join(directory, "geo.tif"),
                4326,
                (-124.5, 0.01, 0, 48.5, 0, -0.01),
            )
            bbox = dataset_bbox(gdal.Open(path))
            self.assertAlmostEqual(bbox[0][0], 48.5, places=3)
            self.assertAlmostEqual(bbox[0][1], -124.5, places=3)
            self.assertAlmostEqual(bbox[1][0], 48.0, places=3)
            self.assertAlmostEqual(bbox[1][1], -124.0, places=3)

    def test_projected_raster_reports_lat_lon(self):
        with TemporaryDirectory() as directory:
            path = make_raster(
                join(directory, "utm.tif"),
                32610,
                utm_geotransform(48.5, -124.5, 0.5, 50),
            )
            bbox = dataset_bbox(gdal.Open(path))
            self.assertAlmostEqual(bbox[0][0], 48.5, places=3)
            self.assertAlmostEqual(bbox[0][1], -124.5, places=3)

    def test_crs_types_agree_on_order(self):
        with TemporaryDirectory() as directory:
            geographic = make_raster(
                join(directory, "geo.tif"),
                4326,
                (-124.5, 0.01, 0, 48.5, 0, -0.01),
            )
            projected = make_raster(
                join(directory, "utm.tif"),
                32610,
                utm_geotransform(48.5, -124.5, 0.5, 50),
            )
            for path in (geographic, projected):
                corner = dataset_bbox(gdal.Open(path))[0]
                self.assertLessEqual(abs(corner[0]), 90, path)  # first slot is latitude
                self.assertGreater(abs(corner[1]), 90, path)  # second slot is longitude


class TestRasterCoverageSelection(unittest.TestCase):
    """detailed_chart_at finds a point inside a raster for either CRS."""

    LAT, LON = 48.25, -124.25  # interior of the 0.5deg x 0.5deg footprint

    def test_selects_point_inside_geographic_raster(self):
        with TemporaryDirectory() as directory:
            path = make_raster(
                join(directory, "geo.tif"),
                4326,
                (-124.5, 0.01, 0, 48.5, 0, -0.01),
            )
            chart = raster_chart(path)
            self.assertIs(select(self.LAT, self.LON, 20, [chart]), chart)

    def test_selects_point_inside_projected_raster(self):
        with TemporaryDirectory() as directory:
            path = make_raster(
                join(directory, "utm.tif"),
                32610,
                utm_geotransform(48.5, -124.5, 0.5, 50),
            )
            chart = raster_chart(path)
            self.assertIs(select(self.LAT, self.LON, 20, [chart]), chart)

    def test_point_outside_raster_is_not_selected(self):
        with TemporaryDirectory() as directory:
            path = make_raster(
                join(directory, "geo.tif"),
                4326,
                (-124.5, 0.01, 0, 48.5, 0, -0.01),
            )
            chart = raster_chart(path)
            self.assertIsNone(select(40.0, -124.25, 20, [chart]))


if __name__ == "__main__":
    unittest.main()
