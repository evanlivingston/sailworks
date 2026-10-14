"""Run in a separate process so GTK and Qt never own the same GUI event loop."""

import json
import math
import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Thread, get_ident, Event
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import QApplication

from gweatherrouting.core.datasource import NMEADataPacket
from gweatherrouting.core.storage import Storage

ORIGINAL_STORAGE_SAVE = Storage.save
ORIGINAL_STORAGE_LOAD = Storage.load_or_save_default
from gweatherrouting.core.mapviewport import MapPoint, MapViewport
from gweatherrouting.qt.controller import SessionController
from gweatherrouting.qt.window import PreviewWindow
from test.test_nmeasession import rmc
from test.test_polarperformance import water, wind


class QtPreviewChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        for target in (
            "gweatherrouting.core.storage.Storage.load_or_save_default",
            "gweatherrouting.core.storage.Storage.save",
            "gweatherrouting.core.gribmanager.GribManager.refresh_local_gribs",
            "gweatherrouting.core.polarmanager.PolarManager.refresh_local_polars",
        ):
            mock = patch(target)
            mock.start()
            self.addCleanup(mock.stop)
        self.controller = SessionController(
            offline=True,
            recording_directory=self.path / "recordings",
            load_charts=False,
        )
        self.controller.core.polar_manager.polars_files = ["first317.pol"]
        self.controller.core.polar_manager.get_path = lambda name: str(
            Path(__file__).parents[1] / "gweatherrouting/data/polars" / name
        )
        self.controller.timer.stop()  # Deterministic playback steps in tests.
        self.window = PreviewWindow(self.controller)
        self.window.show()
        self.addCleanup(self.window.close)
        self.drain()

    def drain(self):
        self.app.processEvents()
        self.app.processEvents()

    def load(self, lines=None):
        path = self.path / "race.log"
        path.write_text(
            "\n".join(
                map(
                    str,
                    lines
                    or [rmc("120000"), water(), wind(), rmc("120010"), rmc("120020")],
                )
            )
        )
        self.controller.load_path(path)
        deadline = time.monotonic() + 5
        while self.controller.session.loading and time.monotonic() < deadline:
            self.drain()
            time.sleep(0.005)
        self.assertFalse(self.controller.session.loading)
        self.assertFalse(self.controller.session.error)
        self.drain()
        return self.controller.session.log

    def test_full_plot_play_pause_restart_eof_and_source_visibility(self):
        self.assertFalse(self.window.speed_group.isVisible())
        log = self.load()
        self.assertTrue(self.window.speed_group.isVisible())
        self.assertEqual(
            len(self.window.plots.axes[0].lines[0].get_xdata()), len(log.points)
        )
        self.window.play.click()
        self.controller.session.advance(5)
        self.drain()
        self.assertTrue(self.controller.session.playing)
        self.assertEqual(self.window.plots.position, 5)
        self.assertEqual(tuple(self.controller.points), log.points)
        for cursor in self.window.plots.cursors:
            self.assertEqual(list(cursor.get_xdata()), [5, 5])
        self.window.stop.click()
        self.assertFalse(self.controller.session.playing)
        self.assertEqual(self.window.plots.position, 5)
        self.window.restart.click()
        self.assertEqual(self.window.plots.position, 0)
        self.window.play.click()
        self.controller.session.advance(100)
        self.assertFalse(self.controller.session.playing)
        self.assertEqual(self.window.plots.position, log.duration)
        self.window.source.setCurrentIndex(0)
        self.assertFalse(self.window.speed_group.isVisible())
        self.assertEqual(self.controller.points, [])
        self.window.source.setCurrentIndex(1)
        self.assertEqual(tuple(self.controller.points), log.points)
        self.window.nmea_dock.setFloating(True)
        self.drain()
        self.assertTrue(self.window.nmea_dock.isFloating())
        self.assertEqual(tuple(self.controller.points), log.points)

    def test_recording_persists_for_both_stop_controls_and_shutdown(self):
        paths = []
        for stop_name in ("record", "stop"):
            self.window.record.click()
            self.assertTrue(self.controller.session.recording)
            packets = [
                NMEADataPacket.parse(str(x))
                for x in (rmc("120000"), water(), wind(), rmc("120010"))
            ]
            self.controller.connections.dispatch("data", packets)
            self.drain()
            path = self.controller.session.recording_path
            paths.append(path)
            self.assertEqual(len(path.read_text().splitlines()), len(packets))
            points = tuple(self.controller.points)
            getattr(self.window, stop_name).click()
            self.assertFalse(self.controller.session.recording)
            self.assertEqual(tuple(self.controller.points), points)
            self.assertIn("saved automatically", self.window.status.text())
        self.assertNotEqual(*paths)
        self.window.close()
        for path in paths:
            self.assertTrue(path.is_file())

    def test_worker_packets_are_queued_and_stale_batches_are_discarded(self):
        packets = [
            NMEADataPacket.parse(str(x))
            for x in (rmc("120000"), water(), wind(), rmc("120010"))
        ]
        thread = Thread(
            target=self.controller.connections.dispatch, args=("data", packets)
        )
        thread.start()
        thread.join()
        self.assertEqual(self.controller.points, [])
        self.drain()
        self.assertEqual(len(self.controller.points), 2)
        self.controller._reset(None)
        thread = Thread(
            target=self.controller.connections.dispatch, args=("data", packets)
        )
        thread.start()
        thread.join()
        self.controller.session.select_source("log")
        self.drain()
        self.assertEqual(self.controller.points, [])

    def test_failed_async_load_retains_previous_log(self):
        previous = self.load()
        self.controller.load_path(self.path / "missing.log")
        deadline = time.monotonic() + 5
        while self.controller.session.loading and time.monotonic() < deadline:
            self.drain()
            time.sleep(0.005)
        self.assertIs(self.controller.session.log, previous)
        self.assertIn("Could not load log", self.window.status.text())
        self.assertEqual(tuple(self.controller.points), previous.points)

    def test_shared_projection_cairo_buffer_and_pan(self):
        canvas = self.window.map
        viewport = MapViewport(canvas)
        point = MapPoint(48.6, -123.1)
        x, y = canvas.convert_geographic_to_screen(point)
        self.assertEqual((x, y), viewport.convert_geographic_to_screen(point))
        lat, lon = canvas.convert_screen_to_geographic(x, y).get_degrees()
        self.assertAlmostEqual(lat, 48.6, places=10)
        self.assertAlmostEqual(lon, -123.1, places=10)
        image = canvas._base_image()
        self.assertFalse(image.isNull())
        self.assertEqual(
            image.width(), math.ceil(canvas.width() * canvas.devicePixelRatioF())
        )
        before = canvas._center_lon
        canvas.mousePressEvent(
            SimpleNamespace(
                button=lambda: Qt.MouseButton.LeftButton,
                position=lambda: QPointF(100, 100),
            )
        )
        canvas.mouseMoveEvent(SimpleNamespace(position=lambda: QPointF(130, 100)))
        canvas.mouseReleaseEvent(None)
        self.assertLess(canvas._center_lon, before)
        self.assertIsNone(canvas.drag)

    def test_coastline_polygon_hole_is_preserved(self):
        path = self.path / "coast.geojson"
        path.write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {},
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [
                                        [-133, 38],
                                        [-113, 38],
                                        [-113, 58],
                                        [-133, 58],
                                        [-133, 38],
                                    ],
                                    [
                                        [-124, 48],
                                        [-123, 48],
                                        [-123, 49],
                                        [-124, 49],
                                        [-124, 48],
                                    ],
                                ],
                            },
                        }
                    ],
                }
            )
        )
        from gweatherrouting.qt.mapcanvas import TrackMap

        self.controller.charts.load_vector_layer(str(path))
        canvas = TrackMap(self.controller)
        canvas.resize(600, 400)
        image = canvas._base_image()
        center = image.pixelColor(image.width() // 2, image.height() // 2)
        self.assertGreater(center.blue(), center.red())  # Hole remains sea.
        x, y = canvas.convert_geographic_to_screen(MapPoint(48.4, -121.4))
        factor = canvas.devicePixelRatioF()
        land = image.pixelColor(round(x * factor), round(y * factor))
        self.assertGreater(land.red(), land.blue())
        canvas.close()

    def test_full_core_delivers_worker_notifications_on_qt_thread(self):
        core = self.controller.core
        self.assertIs(self.controller.session, core.nmea_session)
        events = []
        core.connect(
            "boat_position", lambda boat: events.append((get_ident(), boat.latitude))
        )
        worker = Thread(
            target=lambda: self.controller.connections.dispatch(
                "data", [NMEADataPacket.parse(str(rmc("120000")))]
            )
        )
        worker.start()
        worker.join()
        self.assertFalse(events)
        self.drain()
        self.assertEqual(events, [(get_ident(), core.boat_info.latitude)])
        self.assertIn("48.11730", self.window.instruments.text())
        events.clear()
        worker = Thread(
            target=lambda: self.controller.connections.dispatch(
                "data", [NMEADataPacket.parse(str(rmc("120010")))]
            )
        )
        worker.start()
        worker.join()
        core._reset_nmea(None)
        self.drain()
        self.assertFalse(events)  # Queued position predates the source reset.

    def test_qt_overlay_completes_without_glib_and_shutdown_discards_callbacks(self):
        import cairo

        from gweatherrouting.qt.mapcanvas import TrackMap

        canvas = TrackMap(self.controller)
        self.addCleanup(canvas.close)
        renderer = self.controller.charts._enc_renderer
        started, release = Event(), Event()
        self.addCleanup(release.set)

        def render(job):
            started.set()
            release.wait(2)
            surface = cairo.ImageSurface(
                cairo.FORMAT_ARGB32,
                job.viewport.width * job.factor,
                job.viewport.height * job.factor,
            )
            surface.set_device_scale(job.factor, job.factor)
            cr = cairo.Context(surface)
            cr.set_source_rgb(1, 0, 0)
            cr.paint()
            return surface

        renderer._render = render
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 600, 400)
        renderer.draw(
            canvas,
            cairo.Context(surface),
            (("synthetic.000", 12000, ()),),
            "cm93",
        )
        self.assertTrue(started.wait(1))
        self.assertIsNone(renderer._states[canvas].frame)
        release.set()
        deadline = time.monotonic() + 3
        while renderer._running and time.monotonic() < deadline:
            self.drain()
            time.sleep(0.005)
        self.assertFalse(renderer._running)
        self.assertIsNotNone(renderer._states[canvas].frame)
        events = []
        self.controller.scheduler.submit(events.append, "queued before close")
        self.window.close()
        self.controller.scheduler.submit(events.append, "submitted after close")
        self.drain()
        self.assertFalse(events)
        self.assertFalse(renderer._states)
        self.assertFalse(self.controller.charts.maps)

    def test_production_enc_renders_ogr_dataset_with_owned_qt_pixels(self):
        import cairo
        from osgeo import ogr
        from gweatherrouting.rendering.encrenderer import ENCOverlayRenderer
        from gweatherrouting.rendering.overlayrenderer import RenderJob

        path = str(self.path / "enc.gpkg")
        dataset = ogr.GetDriverByName("GPKG").CreateDataSource(path)
        layer = dataset.CreateLayer("LNDARE", geom_type=ogr.wkbPolygon)
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(
            ogr.CreateGeometryFromWkt(
                "POLYGON((-124 48,-123 48,-123 49,-124 49,-124 48))"
            )
        )
        layer.CreateFeature(feature)
        feature = layer = dataset = None
        viewport = MapViewport(self.window.map)
        job = RenderJob(("cm93", 1, (path,)), viewport, ((path, 0, ()),), 1, "cm93")
        surface = ENCOverlayRenderer(self.controller.scheduler.submit)._render(job)
        self.assertIsInstance(surface, cairo.ImageSurface)
        surface.flush()
        x, y = viewport.convert_geographic_to_screen(MapPoint(48.4, -123.4))
        offset = round(y) * surface.get_stride() + round(x) * 4
        self.assertEqual(
            bytes(surface.get_data())[offset : offset + 4], bytes((122, 185, 201, 255))
        )

    def test_raster_decode_runs_in_loader_without_spawn_and_keeps_palette(self):
        import numpy as np
        from osgeo import gdal, osr
        from gweatherrouting.rendering.gdalrasterchart import GDALSingleRasterChart

        path = str(self.path / "raster.tif")
        dataset = gdal.GetDriverByName("GTiff").Create(path, 2, 2, 1, gdal.GDT_Byte)
        reference = osr.SpatialReference()
        reference.ImportFromEPSG(4326)
        dataset.SetProjection(reference.ExportToWkt())
        dataset.SetGeoTransform((-124, 0.1, 0, 49, 0, -0.1))
        band = dataset.GetRasterBand(1)
        colors = gdal.ColorTable()
        colors.SetColorEntry(1, (11, 22, 33, 255))
        colors.SetColorEntry(2, (44, 55, 66, 255))
        band.SetRasterColorTable(colors)
        band.WriteArray(np.array([[1, 2], [2, 1]], dtype=np.uint8))
        band = dataset = None
        chart = GDALSingleRasterChart(path)
        chart.surface.flush()
        self.assertEqual(
            bytes(chart.surface.get_data())[:8],
            bytes((11, 22, 33, 255, 44, 55, 66, 255)),
        )
        self.assertEqual(chart.surface.get_width(), 2)

    def test_forecast_and_palette_invalidate_chart_image(self):
        from datetime import timedelta

        old_time = self.controller.time_control.time
        self.window.forecast.setDateTime(old_time + timedelta(hours=3))
        self.assertEqual(
            self.controller.time_control.time, old_time + timedelta(hours=3)
        )
        self.window.palette_combo.setCurrentText("dark")
        self.assertEqual(self.controller.settings.chartPalette, "dark")
        self.drain()
        self.assertIsNotNone(self.window.map.background)

    def wait_for_route(self):
        deadline = time.monotonic() + 10
        while self.controller.routing.busy and time.monotonic() < deadline:
            self.drain()
            time.sleep(0.005)
        self.assertFalse(self.controller.routing.busy)
        self.drain()

    def test_routing_panel_calculates_saves_selects_and_fits_real_engine_result(self):
        from test.test_routingjob import ConstantWind

        self.controller.core.grib_manager.gribs = [ConstantWind()]
        panel = self.window.routing_panel
        panel.coastlines.setChecked(False)
        panel.set_location("start", (0, 0))
        panel.set_location("destination", (0, 0.15))
        panel.calculate.click()
        self.assertTrue(self.controller.routing.busy)
        self.assertFalse(panel.calculate.isEnabled())
        self.wait_for_route()
        self.assertFalse(self.controller.routing.error)
        self.assertEqual(len(self.controller.core.routingManager), 1)
        route = self.controller.core.routingManager[0]
        self.assertIs(self.controller.core.routingManager.get_active(), route)
        self.assertEqual(panel.waypoints.rowCount(), len(route.points))
        self.assertEqual(
            self.window.map.route, tuple((p[0], p[1]) for p in route.points)
        )
        self.assertIn("saved", panel.status.text())
        panel.waypoints.setCurrentCell(0, 0)
        self.assertEqual(self.window.map.selected_route_point, 0)
        self.assertTrue(panel.calculate.isEnabled())
        self.assertNotEqual(self.window.map._zoom, 6)
        panel.calculate.click()
        self.wait_for_route()
        self.assertEqual(len(self.controller.core.routingManager), 2)
        self.assertNotEqual(route.name, self.controller.core.routingManager[1].name)

    def test_calculated_route_persists_in_the_shared_gtk_compatible_collection(self):
        from test.test_routingjob import ConstantWind
        from gweatherrouting.core.geo.routingcollection import RoutingCollection

        self.controller.core.grib_manager.gribs = [ConstantWind()]
        panel = self.window.routing_panel
        panel.coastlines.setChecked(False)
        panel.set_location("start", (0, 0))
        panel.set_location("destination", (0, 0.15))
        with (
            patch.object(Storage, "save", ORIGINAL_STORAGE_SAVE),
            patch.object(Storage, "load_or_save_default", ORIGINAL_STORAGE_LOAD),
            patch("gweatherrouting.core.storage.DATA_DIR", str(self.path)),
        ):
            self.controller.core.routingManager = RoutingCollection()
            panel.calculate.click()
            self.wait_for_route()
            self.assertFalse(self.controller.routing.error)
            payload = json.loads((self.path / "routing.json").read_text())["data"]
            self.assertEqual(len(payload["elements"]), 1)
            self.assertEqual(payload["activeElement"], payload["elements"][0]["name"])
            restored = RoutingCollection()
            self.assertEqual(
                [tuple(point) for point in restored[0].points],
                self.controller.core.routingManager[0].points,
            )
            self.assertEqual(restored[0].provenance["currents"], "disabled")

    def test_routing_cancel_and_close_keep_completed_routes_and_drop_late_results(self):
        from test.test_routingjob import ConstantWind
        from gweatherrouting.core.routingjob import CalculatedRoute

        self.controller.core.grib_manager.gribs = [ConstantWind()]
        from gweatherrouting.core.geo.routing import Routing

        existing = Routing("existing", [(0, 0, "2026-10-01", 0, 1, 1, 0)])
        self.controller.core.routingManager.append(existing)
        self.window.routing_panel.refresh_routes(existing)
        started, release = Event(), Event()
        self.addCleanup(release.set)

        def slow(*args):
            started.set()
            release.wait(3)
            return CalculatedRoute(((0, 0, "2026-10-01", 0, 1, 1, 0),), (), {})

        with patch(
            "gweatherrouting.qt.routingcontroller.calculate_route", side_effect=slow
        ):
            panel = self.window.routing_panel
            panel.calculate.click()
            self.assertTrue(started.wait(1))
            panel.cancel_button.click()
            self.assertTrue(self.controller.routing.cancel_event.is_set())
            release.set()
            self.wait_for_route()
            self.assertEqual(len(self.controller.core.routingManager), 1)
            self.assertIs(self.controller.core.routingManager[0], existing)
            self.assertEqual(panel.status.text(), "Routing cancelled")
            started.clear()
            release.clear()
            panel.calculate.click()
            self.assertTrue(started.wait(1))
            self.window.close()
            release.set()
            self.controller.routing.worker_thread.join(2)
            self.drain()
            self.assertEqual(len(self.controller.core.routingManager), 1)
            self.assertIs(self.controller.core.routingManager[0], existing)

    def test_routing_missing_wind_and_invalid_positions_leave_results_unchanged(self):
        panel = self.window.routing_panel
        panel.calculate.click()
        self.assertIn("forecast", panel.status.text())
        self.assertEqual(len(self.controller.core.routingManager), 0)
        panel.set_location("start", (0, 0))
        panel.set_location("destination", (0, 0))
        panel.calculate.click()
        self.assertIn("different", panel.status.text())
        self.assertFalse(self.controller.routing.busy)
        from test.test_routingjob import ConstantWind
        from weatherrouting import RoutingNoWindError

        self.controller.core.grib_manager.gribs = [ConstantWind()]
        panel.set_location("destination", (0, 0.15))
        with patch(
            "gweatherrouting.qt.routingcontroller.calculate_route",
            side_effect=RoutingNoWindError(),
        ):
            panel.calculate.click()
            self.wait_for_route()
        self.assertIn("forecast", panel.status.text())
        self.assertTrue(panel.calculate.isEnabled())
        self.assertEqual(len(self.controller.core.routingManager), 0)

    def test_frontend_does_not_import_gtk(self):
        self.assertNotIn("gi.repository.Gtk", sys.modules)
        self.assertFalse(
            any(name.startswith("gweatherrouting.gtk") for name in sys.modules)
        )
        self.assertFalse(hasattr(self.window, "save_button"))
        self.assertFalse(hasattr(self.window, "clear_button"))
        self.assertTrue(self.window.grab().width())


if __name__ == "__main__":
    unittest.main()
