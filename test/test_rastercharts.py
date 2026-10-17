"""Raster chart coverage axis order and native-scale metadata."""

import math
import unittest
from os.path import join
from tempfile import TemporaryDirectory
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

from osgeo import gdal, ogr, osr

from gweatherrouting.core.enccharts import chart_scale_rank
from gweatherrouting.rendering.chartcollection import ChartCollection
from gweatherrouting.rendering.gdalrasterchart import (
    GDALRasterChart,
    GDALSingleRasterChart,
    dataset_bbox,
    header_scale,
    native_scale_from_bbox,
)
from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart

# Display metres-per-pixel that yields the given nominal scale at 96 dpi.
HARBOR_MPP = 5000 / 3779.527559  # ~1:5,000 harbor zoom
COASTAL_MPP = 40000 / 3779.527559  # ~1:40,000 coastal zoom


def make_raster(path, epsg, geotransform, size=50, metadata=None):
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
    if metadata:
        for key, value in metadata.items():
            dataset.SetMetadataItem(key, value)
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


def raster_chart(path, native_scale=None):
    """A GDALRasterChart carrying file metadata, without loading pixels.

    native_scale overrides the derived scale to stage selection scenarios at a
    chosen compilation scale; pass 0 to model a chart with no derivable scale.
    """
    chart = GDALRasterChart.__new__(GDALRasterChart)
    chart.path = path
    chart.ctype = "raster"
    chart.enabled = True
    info = GDALSingleRasterChart.get_file_info(path)
    chart.metadata = [info]
    chart._native_scale = info[4] if native_scale is None else native_scale
    return chart


def enc_chart(name, scale, lat, lon, half=1.0):
    """A minimal ENC whose coverage polygon contains (lat, lon)."""
    chart = GDALVectorChart.__new__(GDALVectorChart)
    chart.path = "/charts/" + name + ".000"
    chart.ctype = "vector"
    chart.enabled = True
    chart._native_scale = scale
    chart.vector_file = Mock()
    wkt = (
        f"POLYGON(({lon - half} {lat - half},{lon + half} {lat - half},"
        f"{lon + half} {lat + half},{lon - half} {lat + half},"
        f"{lon - half} {lat - half}))"
    )
    chart._coverage = [ogr.CreateGeometryFromWkt(wkt)]
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


class TestScaleDerivation(unittest.TestCase):
    """Raster native scale from headers and synthetic geotransforms."""

    def test_header_scale_parsing(self):
        self.assertEqual(header_scale({"CSCALE": "25000"}), 25000)
        self.assertEqual(header_scale({"scale": "12000"}), 12000)  # case-insensitive
        self.assertEqual(header_scale({"SCALE": "1:30000"}), 30000)  # 1:N notation
        self.assertEqual(header_scale({"SCALE": "abc"}), 0)
        self.assertEqual(header_scale({"SCALE": "-5"}), 0)
        self.assertEqual(header_scale({}), 0)
        self.assertEqual(header_scale(None), 0)

    def test_header_scale_beats_geotransform(self):
        with TemporaryDirectory() as directory:
            path = make_raster(
                join(directory, "hdr.tif"),
                4326,
                (-124.5, 0.001, 0, 48.5, 0, -0.001),
                size=100,
                metadata={"CSCALE": "25000"},
            )
            # The geotransform would derive ~279,000; CSCALE must win.
            self.assertEqual(GDALSingleRasterChart.get_file_info(path)[4], 25000)

    def test_derived_scale_scales_with_pixel_size(self):
        with TemporaryDirectory() as directory:
            # Same centre latitude (48.45) so only the pixel size differs.
            fine = make_raster(
                join(directory, "fine.tif"),
                4326,
                (-124.45, 0.001, 0, 48.5, 0, -0.001),
                size=100,
            )
            coarse = make_raster(
                join(directory, "coarse.tif"),
                4326,
                (-124.45, 0.002, 0, 48.55, 0, -0.002),
                size=100,
            )
            fine_scale = GDALSingleRasterChart.get_file_info(fine)[4]
            coarse_scale = GDALSingleRasterChart.get_file_info(coarse)[4]
            self.assertGreater(fine_scale, 0)
            self.assertAlmostEqual(coarse_scale, 2 * fine_scale, delta=2)

    def test_derived_scale_shrinks_toward_the_pole(self):
        with TemporaryDirectory() as directory:
            # Identical pixel degrees; only the latitude differs, so the scale
            # ratio must follow cos(latitude) (longitude metres shrink poleward).
            equator = make_raster(
                join(directory, "equator.tif"),
                4326,
                (-124.45, 0.001, 0, 0.5, 0, -0.001),
                size=100,
            )
            polar = make_raster(
                join(directory, "polar.tif"),
                4326,
                (-124.45, 0.001, 0, 60.5, 0, -0.001),
                size=100,
            )
            equator_scale = GDALSingleRasterChart.get_file_info(equator)[4]
            polar_scale = GDALSingleRasterChart.get_file_info(polar)[4]
            self.assertGreater(equator_scale, polar_scale)
            self.assertAlmostEqual(
                equator_scale / polar_scale,
                math.cos(math.radians(0.45)) / math.cos(math.radians(60.45)),
                delta=0.1,
            )

    def test_degenerate_footprint_has_no_scale(self):
        self.assertEqual(native_scale_from_bbox([(48.5, -124.5, 0)] * 2, 100), 0)
        self.assertEqual(native_scale_from_bbox([], 100), 0)
        self.assertEqual(native_scale_from_bbox([(48.5, -124.5, 0)] * 2, 0), 0)


class TestMetadataNormalisation(unittest.TestCase):
    """Stored file entries gain a scale; the layer exposes its native scale."""

    BOX = [(48.5, -124.5, 0.0), (48.0, -124.0, 0.0)]

    @staticmethod
    def _chart(metadata):
        chart = GDALRasterChart.__new__(GDALRasterChart)
        chart.path = "/charts/x"
        chart.ctype = "raster"
        chart.enabled = True
        chart.metadata = metadata
        return chart

    def test_legacy_entry_backfills_scale(self):
        chart = self._chart([("/a/old.tif", self.BOX, 100, 100)])
        chart.on_metadata_update()
        entry = chart.metadata[0]
        self.assertEqual(len(entry), 5)
        self.assertGreater(entry[4], 0)
        self.assertGreater(chart._native_scale, 0)

    def test_existing_scale_is_kept(self):
        chart = self._chart([("/a/new.tif", self.BOX, 100, 100, 30000)])
        chart.on_metadata_update()
        self.assertEqual(chart.metadata[0][4], 30000)
        self.assertEqual(chart._native_scale, 30000)

    def test_layer_uses_most_detailed_file(self):
        chart = self._chart(
            [
                ("/a/coastal.tif", self.BOX, 100, 100, 50000),
                ("/a/harbor.tif", self.BOX, 100, 100, 5000),
            ]
        )
        chart.on_metadata_update()
        self.assertEqual(chart._native_scale, 5000)

    def test_layer_without_any_scale_is_zero(self):
        # A degenerate footprint derives no scale, so the layer stays a fallback.
        chart = self._chart([("/a/flat.tif", [(48.5, -124.5, 0)] * 2, 100, 100, 0)])
        chart.on_metadata_update()
        self.assertEqual(chart._native_scale, 0)


class TestBestChartSelection(unittest.TestCase):
    """A raster now competes in best-chart queries based on its scale."""

    LAT, LON = 48.25, -124.25  # inside both the KAP and the ENC footprint

    def _kap(self, directory, native_scale=None):
        path = make_raster(
            join(directory, "kap.tif"),
            4326,
            (-124.5, 0.01, 0, 48.5, 0, -0.01),  # covers 48.0-48.5, -124.5..-124.0
        )
        return raster_chart(path, native_scale=native_scale)

    def test_harbor_kap_wins_at_harbor_zoom(self):
        with TemporaryDirectory() as directory:
            kap = self._kap(directory, native_scale=5000)
            enc = enc_chart("US3COAST", 40000, self.LAT, self.LON)
            self.assertIs(select(self.LAT, self.LON, HARBOR_MPP, [kap, enc]), kap)

    def test_enc_wins_when_zoomed_out(self):
        with TemporaryDirectory() as directory:
            kap = self._kap(directory, native_scale=5000)
            enc = enc_chart("US3COAST", 40000, self.LAT, self.LON)
            self.assertIs(select(self.LAT, self.LON, COASTAL_MPP, [kap, enc]), enc)

    def test_scale_less_raster_yields_to_a_scaled_chart(self):
        with TemporaryDirectory() as directory:
            kap = self._kap(directory, native_scale=0)
            enc = enc_chart("US3COAST", 40000, self.LAT, self.LON)
            self.assertIs(select(self.LAT, self.LON, HARBOR_MPP, [kap, enc]), enc)

    def test_scale_less_raster_is_still_selected_alone(self):
        with TemporaryDirectory() as directory:
            kap = self._kap(directory, native_scale=0)
            self.assertIs(select(self.LAT, self.LON, HARBOR_MPP, [kap]), kap)

    def test_scale_rank_is_unchanged_for_rasters(self):
        # The raster's scale feeds the same rank function every other chart uses.
        self.assertEqual(chart_scale_rank(5000, HARBOR_MPP)[0], 0)


if __name__ == "__main__":
    unittest.main()
