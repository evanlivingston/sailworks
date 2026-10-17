"""Master display switches preserve per-file and routing choices."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gweatherrouting.common import resource_path
from gweatherrouting.core.settings import SettingsManager
from gweatherrouting.core.storage import Storage
from gweatherrouting.rendering.chartclasses import (
    CHART_CLASSES,
    CLASS_SETTINGS,
    chart_class,
)
from gweatherrouting.rendering.chartcollection import ChartCollection
from gweatherrouting.rendering.gdalrasterchart import GDALRasterChart
from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart
from gweatherrouting.rendering.gshhs import GSHHSVectorChart
from gweatherrouting.rendering.mbtilesrenderer import MBTilesChart
from gweatherrouting.core.mbtiles import MBTilesInfo
from gweatherrouting.rendering.vectordrawer.cm93chartdrawer import CM93ChartDrawer
from gweatherrouting.rendering.vectordrawer.osmchartdrawer import OSMChartDrawer
from gweatherrouting.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer
from test.map_helpers import MapCanvas


def vector(path, drawer):
    chart = GDALVectorChart.__new__(GDALVectorChart)
    chart.path, chart.drawer, chart.enabled = path, drawer.__new__(drawer), True
    chart._native_scale, chart._coverage_envelopes = 12000, ()
    return chart


class TestChartClasses(unittest.TestCase):
    def setUp(self):
        for name in ("load_or_save_default", "save"):
            guard = patch.object(Storage, name)
            guard.start()
            self.addCleanup(guard.stop)
        self.settings = SettingsManager()
        object.__setattr__(self.settings, "_Storage__init", True)
        self.manager = ChartCollection(self.settings)
        self.manager._draw_base = Mock()
        self.manager._enc_renderer = Mock()
        self.manager._mbtiles_renderer = Mock()
        self.manager._chs_renderer.draw = Mock()
        world = GSHHSVectorChart.__new__(GSHHSVectorChart)
        world.path, world.enabled = "/world", True
        self.charts = {
            "world": world,
            "enc": vector("harbor.000", S57ChartDrawer),
            "raster": GDALRasterChart("/raster", self.settings),
            "mbtiles": MBTilesChart(
                "/offline.mbtiles",
                self.settings,
                MBTilesInfo(
                    "/offline.mbtiles", "offline", (-123, 47, -122, 48), (10,)
                ).metadata(),
            ),
            "cm93": vector("/cm93", CM93ChartDrawer),
            "seamarks": vector("/seamarks", OSMChartDrawer),
            "vector": SimpleNamespace(path="other", enabled=True),
        }
        self.manager.charts = list(self.charts.values())

    def test_classification_and_defaults(self):
        for kind, key, _ in CHART_CLASSES:
            with self.subTest(kind=kind):
                self.assertEqual(chart_class(self.charts[kind]), kind)
                self.assertTrue(self.settings[key])
        countries = SimpleNamespace(
            path=resource_path("gweatherrouting", "data/countries.geojson")
        )
        self.assertEqual(chart_class(countries), "world")

    def test_each_switch_filters_rendering_without_rewriting_file_flags(self):
        for kind, key, _ in CHART_CLASSES:
            with self.subTest(kind=kind):
                self.settings[key] = False
                self.manager.do_draw(MapCanvas(), Mock())
                drawn_base = self.manager._draw_base.call_args.args[2]
                enc_specs = self.manager._enc_renderer.draw.call_args.args[2]
                mbtiles_specs = self.manager._mbtiles_renderer.draw.call_args.args[2]
                self.assertEqual(bool(mbtiles_specs), kind != "mbtiles")
                if kind == "enc":
                    self.assertFalse(enc_specs)
                else:
                    self.assertNotIn(self.charts[kind], drawn_base)
                    self.assertTrue(enc_specs)
                self.assertTrue(all(chart.enabled for chart in self.manager.charts))
                self.settings[key] = True

    def test_enabling_class_keeps_individually_disabled_files_disabled(self):
        chart = self.charts["enc"]
        chart.enabled = False
        self.settings.chartENCVisible = False
        self.settings.chartENCVisible = True
        self.manager.do_draw(MapCanvas(), Mock())
        self.assertFalse(self.manager._enc_renderer.draw.call_args.args[2])
        self.assertFalse(chart.enabled)

    def test_switch_invalidates_both_maps_and_cancels_disabled_enc(self):
        maps = [Mock(), Mock()]
        for canvas in maps:
            self.manager.add_map(canvas)
        self.settings["chartENCVisible"] = False
        for canvas in maps:
            canvas.invalidate_chart_sources.assert_called_once()
            self.manager._enc_renderer.forget.assert_any_call(canvas)
        self.manager._enc_renderer.forget.reset_mock()
        self.settings.chartWorldVisible = False
        self.manager._enc_renderer.forget.assert_not_called()

    def test_chs_is_independent_of_local_enc(self):
        self.settings.canadianCharts = False
        self.assertFalse(self.manager._chs_renderer.enabled)
        self.assertTrue(self.settings.chartENCVisible)
        self.settings.canadianCharts = True
        self.settings.chartENCVisible = False
        self.assertTrue(self.manager._chs_renderer.enabled)

    def test_mbtiles_switch_cancels_pending_display_work(self):
        canvas = Mock()
        self.manager.add_map(canvas)
        self.settings["chartMBTilesVisible"] = False
        self.manager._mbtiles_renderer.forget.assert_called_once_with(canvas)
        canvas.invalidate_chart_sources.assert_called_once()

    def test_hidden_enc_is_not_active_or_offered_for_download(self):
        self.manager.charts = [self.charts["enc"]]
        self.settings.chartENCVisible = False
        catalog = Mock()
        self.assertIsNone(self.manager.detailed_chart_at(48, -123, 3, enabled=True))
        self.assertEqual(
            self.manager.chart_suggestion_at(catalog, 48, -123, 3), (None, None)
        )
        catalog.chart_at.assert_not_called()

    def test_import_does_not_silently_disable_seamarks(self):
        self.manager.osmLayer = self.charts["seamarks"]
        with patch(
            "gweatherrouting.rendering.chartcollection.GDALVectorChart",
            return_value=self.charts["vector"],
        ):
            self.manager.load_vector_layer("other.geojson")
        self.manager.load_raster_layer("/new-raster")
        self.assertTrue(self.manager.osmLayer.enabled)

    def test_new_import_respects_a_disabled_class(self):
        self.settings.chartRasterVisible = False
        imported = self.manager.load_raster_layer("/new-raster")
        self.manager.do_draw(MapCanvas(), Mock())
        self.assertNotIn(imported, self.manager._draw_base.call_args.args[2])
        self.assertTrue(imported.enabled)

    def test_visibility_settings_do_not_remove_routing_providers(self):
        before = self.manager.get_line_point_validity_providers()
        self.assertIn(self.charts["world"], before)
        for key in CLASS_SETTINGS.values():
            self.settings[key] = False
        self.assertEqual(self.manager.get_line_point_validity_providers(), before)
        self.assertTrue(all(chart.enabled for chart in self.manager.charts))
