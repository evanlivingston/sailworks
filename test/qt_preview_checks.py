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
from PySide6.QtWidgets import QApplication, QToolBar, QMessageBox

from gweatherrouting.core.datasource import NMEADataPacket
from gweatherrouting.core.storage import Storage

ORIGINAL_STORAGE_SAVE = Storage.save
ORIGINAL_STORAGE_LOAD = Storage.load_or_save_default
from gweatherrouting.core.polarmanager import PolarManager

ORIGINAL_POLAR_REFRESH = PolarManager.refresh_local_polars
from gweatherrouting.core.mapviewport import MapPoint, MapViewport
from gweatherrouting.qt.controller import SessionController
from gweatherrouting.qt.window import PreviewWindow
from gweatherrouting.qt.theme import apply_theme
from test.test_nmeasession import rmc
from test.test_polarperformance import water, wind


class QtPreviewChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

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
        self.controller.core.polar_manager.polars = ["first317.pol"]
        self.controller.core.polar_manager.get_path = lambda name: str(
            Path(__file__).parents[1] / "gweatherrouting/data/polars" / name
        )
        self.controller.timer.stop()  # Deterministic playback steps in tests.
        self.window = PreviewWindow(
            self.controller, workspace_path=self.path / "workspace-qt.json"
        )
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
        self.assertIs(panel.pages.currentWidget(), panel.results_page)
        self.assertEqual(
            self.window.map.route_speeds, tuple(p[5] or 0 for p in route.points)
        )
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

    def wait_for_data(self):
        deadline = time.monotonic() + 10
        while self.controller.data.task.busy and time.monotonic() < deadline:
            self.drain()
            time.sleep(0.005)
        self.drain()
        self.assertFalse(self.controller.data.task.busy)
        self.assertEqual(self.controller.data.error, "")

    def wind_fixture(self, name="wind.grib2"):
        import numpy as np
        from osgeo import gdal

        path = self.path / name
        dataset = gdal.GetDriverByName("GTiff").Create(
            str(path), 4, 3, 4, gdal.GDT_Float32
        )
        dataset.SetGeoTransform((-124, 1, 0, 50, 0, -1))
        for index, (hour, element, value) in enumerate(
            ((0, "UGRD", 2), (0, "VGRD", 4), (3, "UGRD", 8), (3, "VGRD", 10)), 1
        ):
            band = dataset.GetRasterBand(index)
            band.SetMetadata(
                {
                    "GRIB_ELEMENT": element,
                    "GRIB_FORECAST_SECONDS": str(hour * 3600),
                    "GRIB_REF_TIME": "1790812800",
                    "GRIB_CENTER": "test",
                }
            )
            band.WriteArray(np.full((3, 4), value, dtype=np.float32))
        band = dataset = None
        return path

    def test_qt_connection_edit_persists_legacy_n2k_offline_and_cancel_keeps_original(
        self,
    ):
        from gweatherrouting.qt.settingsdialog import ConnectionDialog
        from gweatherrouting.core.connectionmanager import ConnectionManagerStorage

        legacy = {
            "type": "serial",
            "protocol": "n2k_ascii",
            "direction": "in",
            "data-port": "/dev/example",
            "baudrate": 115200,
        }
        dialog = ConnectionDialog(self.window, legacy)
        self.assertFalse(dialog.direction.isEnabled())
        self.assertEqual(dialog.wire_format.currentData(), "n2k_ascii")
        dialog.reject()
        self.assertEqual(self.controller.connections.connections, [])
        dialog.save()
        self.assertEqual(dialog.result_config["format"], "n2k_ascii")
        self.window.show_settings()
        settings = self.window.settings_dialog
        settings.draft = [dialog.result_config]
        settings.reload()
        with (
            patch("gweatherrouting.core.storage.DATA_DIR", str(self.path)),
            patch.object(Storage, "save", ORIGINAL_STORAGE_SAVE),
            patch.object(self.controller.connections, "plug_all") as plug,
        ):
            ORIGINAL_STORAGE_LOAD(self.controller.connections.storage)
            settings.apply_connections()
            deadline = time.monotonic() + 5
            while self.controller.connection_task.busy and time.monotonic() < deadline:
                self.drain()
                time.sleep(0.005)
            self.drain()
            self.assertFalse(self.controller.connection_task.busy)
            payload = json.loads((self.path / "conn-manager.json").read_text())
            self.assertEqual(payload["connections"][0]["protocol"], "nmea2000")
            plug.assert_not_called()
            with patch.object(Storage, "load_or_save_default", ORIGINAL_STORAGE_LOAD):
                reloaded = ConnectionManagerStorage()
            self.assertEqual(reloaded.connections, settings.draft)
        self.assertFalse(self.controller.connections.polling_active)

    def test_forecast_library_import_toggle_duplicate_and_shared_disk_reload(self):
        from gweatherrouting.core.gribmanager import GribManagerStorage
        from gweatherrouting.qt.datalibrary import DataLibrary

        library_root = self.path / "library"
        library_root.mkdir()
        fixture = self.wind_fixture()
        data = self.controller.data
        with (
            patch("gweatherrouting.qt.datacontroller.GRIB_DIR", str(library_root)),
            patch("gweatherrouting.core.storage.DATA_DIR", str(self.path)),
            patch.object(Storage, "save", ORIGINAL_STORAGE_SAVE),
        ):
            ORIGINAL_STORAGE_LOAD(data.manager.storage)
            library = DataLibrary(self.controller, self.window)
            library.show()
            data.import_forecast(str(fixture))
            self.wait_for_data()
            first = data.local_forecasts[0]
            self.assertNotEqual(first.path, str(fixture))
            self.assertTrue(fixture.exists())
            self.assertTrue(
                library.forecasts.item(0, 0).checkState() == Qt.CheckState.Checked
            )
            enabled = list(data.manager.gribs)
            self.assertEqual(len(enabled), 1)
            data.set_forecast_enabled(first, False)
            self.assertFalse(data.manager.gribs)
            data.set_forecast_enabled(first, True)
            self.wait_for_data()
            self.assertEqual(len(data.manager.gribs), 1)
            data.import_forecast(str(fixture))
            self.wait_for_data()
            self.assertEqual(len(list(library_root.glob("*.grib2"))), 2)
            self.assertEqual(len(data.manager.gribs), 2)
            self.assertEqual(Path(first.path).read_bytes(), fixture.read_bytes())
            with patch.object(Storage, "load_or_save_default", ORIGINAL_STORAGE_LOAD):
                reloaded = GribManagerStorage()
            self.assertEqual(set(reloaded.opened), {g.name for g in data.manager.gribs})
            data.remove_forecast(first)
            self.assertFalse(Path(first.path).exists())
            self.assertTrue(fixture.exists())
            bad = self.path / "bad.grib2"
            bad.write_text("invalid data")
            data.import_forecast(str(bad))
            deadline = time.monotonic() + 5
            while data.task.busy and time.monotonic() < deadline:
                self.drain()
                time.sleep(0.005)
            self.drain()
            self.assertTrue(data.error)
            self.assertEqual(len(data.manager.gribs), 1)
            self.assertEqual(len(list(library_root.glob("*.grib2"))), 1)
            library.close()

    def test_chart_library_real_vector_and_raster_indexing_persistence_and_palette(
        self,
    ):
        from gweatherrouting.core.settings import SettingsManager

        data = self.controller.data
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
                                "type": "LineString",
                                "coordinates": [[-123.5, 48.2], [-123.4, 48.3]],
                            },
                        }
                    ],
                }
            )
        )
        with (
            patch("gweatherrouting.core.storage.DATA_DIR", str(self.path)),
            patch.object(Storage, "save", ORIGINAL_STORAGE_SAVE),
        ):
            ORIGINAL_STORAGE_LOAD(self.controller.settings)
            data.import_chart(str(path), "vector")
            self.wait_for_data()
            chart = self.controller.charts.charts[0]
            self.controller.settings.chartPalette = "dark"
            self.assertEqual(chart.drawer.palette, "dark")
            second_path = self.path / "second.geojson"
            second_path.write_bytes(path.read_bytes())
            data.import_chart(str(second_path), "vector")
            self.wait_for_data()
            second_chart = self.controller.charts.charts[1]
            data.set_chart_enabled(chart, False)
            self.assertEqual(
                [e["path"] for e in self.controller.settings.vectorCharts],
                [str(path), str(second_path)],
            )
            with patch.object(Storage, "load_or_save_default", ORIGINAL_STORAGE_LOAD):
                reloaded = SettingsManager()
            self.assertFalse(reloaded.vectorCharts[0]["enabled"])
            self.assertEqual(reloaded.vectorCharts[0]["path"], str(path))
            data.remove_chart(chart)
            self.assertTrue(path.exists())
            data.remove_chart(second_chart)
            self.assertFalse(self.controller.settings.vectorCharts)
            from osgeo import gdal, osr

            directory = self.path / "raster"
            directory.mkdir()
            raster = gdal.GetDriverByName("GTiff").Create(
                str(directory / "chart.tif"), 3, 3, 1
            )
            raster.SetGeoTransform((-124, 1, 0, 50, 0, -1))
            spatial = osr.SpatialReference()
            spatial.ImportFromEPSG(4326)
            raster.SetProjection(spatial.ExportToWkt())
            colors = gdal.ColorTable()
            colors.SetColorEntry(0, (0, 0, 0, 0))
            colors.SetColorEntry(1, (20, 100, 150, 255))
            raster.GetRasterBand(1).SetRasterColorTable(colors)
            raster.GetRasterBand(1).Fill(1)
            raster = None
            (directory / "readme.txt").write_text("Ignore sidecars")
            data.import_chart(str(directory), "raster")
            self.wait_for_data()
            self.assertEqual(
                len(self.controller.settings.rasterCharts[0]["metadata"]), 1
            )
            self.assertEqual(len(self.controller.charts.charts), 1)
            data.import_chart(str(self.path / "missing.geojson"), "vector")
            deadline = time.monotonic() + 5
            while data.task.busy and time.monotonic() < deadline:
                self.drain()
                time.sleep(0.005)
            self.drain()
            self.assertTrue(data.error)
            self.assertEqual(len(self.controller.charts.charts), 1)

    def test_provider_download_is_staged_and_committed_on_ui_thread(self):
        import shutil
        from gweatherrouting.core.gribsources import NOAAGFSSource

        data = self.controller.data
        fixture = self.wind_fixture()
        root = self.path / "forecasts"
        root.mkdir()
        threads = []
        data.forecast_loaded.connect(lambda _: threads.append(get_ident()))

        def download(source, identifier, directory, progress):
            self.assertNotEqual(get_ident(), get_ident_main)
            self.assertNotEqual(Path(directory), root)
            destination = Path(directory) / "download.grib2"
            shutil.copyfile(fixture, destination)
            progress(50)
            return str(destination)

        get_ident_main = get_ident()
        with (
            patch("gweatherrouting.qt.datacontroller.GRIB_DIR", str(root)),
            patch.object(NOAAGFSSource, "download", download),
        ):
            data.list_forecasts("NOAA GFS", (45, -125, 50, -120))
            self.wait_for_data()
            self.assertTrue(data.remote_forecasts)
            source, row = data.remote_forecasts[0]
            self.assertEqual(source.bounds, (45, -125, 50, -120))
            data.download_forecast((source, row))
            self.wait_for_data()
            self.assertEqual(threads, [get_ident_main])
            self.assertEqual(len(data.manager.gribs), 1)
            self.assertEqual(len(list(root.glob("*.grib2"))), 1)
            before = list(data.manager.gribs)
            with patch.object(
                NOAAGFSSource, "download", side_effect=OSError("network unavailable")
            ):
                data.download_forecast((source, row))
                deadline = time.monotonic() + 5
                while data.task.busy and time.monotonic() < deadline:
                    self.drain()
                    time.sleep(0.005)
                self.drain()
            self.assertIn("network unavailable", data.error)
            self.assertEqual(data.manager.gribs, before)

    def test_noaa_chart_discovery_and_download_share_the_chart_library(self):
        from gweatherrouting.core.enccharts import ENCCell

        data = self.controller.data
        cell = ENCCell(
            "US5TEST1",
            "Test harbor",
            20000,
            1024,
            [("E", [(48, -124), (49, -124), (49, -123), (48, -123)])],
        )
        outside = ENCCell(
            "US5TEST2",
            "Other harbor",
            20000,
            1024,
            [("E", [(0, 0), (1, 0), (1, 1), (0, 1)])],
        )
        path = self.path / "downloaded.geojson"
        path.write_text('{"type":"FeatureCollection","features":[]}')

        def load():
            data.catalog.cells = [cell, outside]

        with (
            patch.object(data.catalog, "load", side_effect=load),
            patch.object(data.catalog, "download", return_value=str(path)) as download,
        ):
            data.list_charts(48.5, -123.5)
            self.wait_for_data()
            self.assertEqual(data.remote_charts, [cell])
            data.download_chart(cell)
            self.wait_for_data()
            download.assert_called_once_with(cell)
            self.assertTrue(self.controller.charts.charts[0].enabled)
            self.assertEqual(
                self.controller.settings.vectorCharts[0]["path"], str(path)
            )

    def test_connection_start_and_reconnect_worker_keep_playback_and_close_safely(self):
        started, release = Event(), Event()
        controller = self.controller
        controller.offline = False

        def connect():
            started.set()
            release.wait(3)

        with (
            patch.object(controller.connections, "start_polling", side_effect=connect),
            patch.object(controller.connections, "stop_polling") as stop,
        ):
            controller.start_connections()
            self.assertTrue(started.wait(1))
            self.load()
            self.window.play.click()
            controller.session.advance(5)
            self.drain()
            self.assertTrue(controller.session.playing)
            self.assertEqual(self.window.plots.position, 5)
            release.set()
            controller.close()
            self.assertFalse(controller.connection_task.worker_thread.is_alive())
            stop.assert_called_once()
            self.drain()

    def test_data_worker_shutdown_drops_late_result_and_leaves_ui_responsive(self):
        data = self.controller.data
        started, release = Event(), Event()

        def slow(_):
            started.set()
            release.wait(3)
            return []

        data.run("forecast-list", "Loading…", slow)
        self.assertTrue(started.wait(1))
        self.window.views.setCurrentWidget(self.window.logs_view)
        self.drain()
        self.assertIs(self.window.views.currentWidget(), self.window.logs_view)
        self.controller.data.close()
        release.set()
        data.task.worker_thread.join(2)
        self.drain()
        self.assertEqual(data.remote_forecasts, [])

    def test_workspace_views_and_grouped_panels_preserve_playback_and_map(self):
        from PySide6.QtGui import QPalette

        log = self.load()
        self.assertIs(self.window.views.currentWidget(), self.window.logs_view)
        self.assertLess(
            self.app.palette().color(QPalette.ColorRole.Window).lightness(), 80
        )
        self.controller.session.set_playing(True)
        self.controller.session.advance(5)
        camera = (
            self.window.map._center_lat,
            self.window.map._center_lon,
            self.window.map._zoom,
        )
        self.window._show_panel(self.window.routing_dock)
        self.drain()
        self.assertIs(self.window.views.currentWidget(), self.window.map)
        self.assertEqual(
            self.window.tabifiedDockWidgets(self.window.nmea_dock),
            [self.window.routing_dock],
        )
        self.window._show_panel(self.window.nmea_dock)
        self.window.views.setCurrentWidget(self.window.logs_view)
        self.drain()
        self.assertEqual(self.controller.session.position, 5)
        self.assertTrue(self.controller.session.playing)
        self.assertEqual(
            len(self.window.plots.axes[0].lines[0].get_xdata()), len(log.points)
        )
        self.assertEqual(
            camera,
            (
                self.window.map._center_lat,
                self.window.map._center_lon,
                self.window.map._zoom,
            ),
        )
        self.assertLess(self.window.plots.figure.get_facecolor()[0], 0.2)
        self.assertEqual(self.controller.settings.chartPalette, "cm93")
        self.window.resize(1000, 650)
        self.window._show_panel(self.window.routing_dock)
        self.window.routing_panel.pages.setCurrentIndex(0)
        self.drain()
        setup_scroll = self.window.routing_panel.pages.widget(0)
        self.assertEqual(setup_scroll.horizontalScrollBar().maximum(), 0)
        self.assertFalse(self.window.routing_panel.calculate.visibleRegion().isEmpty())
        self.assertFalse(
            self.window.routing_panel.cancel_button.visibleRegion().isEmpty()
        )

    def restart_workspace(self, before_open=None, routes=()):
        path = self.window.workspace.store.path
        self.window.close()
        self.drain()
        if before_open is not None:
            before_open(path)
        controller = SessionController(
            offline=True,
            recording_directory=self.path / "recordings",
            load_charts=False,
        )
        controller.timer.stop()
        controller.core.polar_manager.polars_files = ["first317.pol"]
        controller.core.polar_manager.polars = ["first317.pol"]
        for route in routes:
            controller.core.routingManager.append(route)
        self.controller = controller
        self.window = PreviewWindow(controller, workspace_path=path)
        self.window.show()
        self.addCleanup(self.window.close)
        self.drain()
        return self.window.workspace

    def test_workspace_roundtrip_keeps_layout_view_and_options_without_instrument_history(
        self,
    ):
        from gweatherrouting.core.geo.routing import Routing
        from PySide6.QtCore import Qt

        old_routes = [
            Routing("first", [(48, -123, "10/01/2026, 12:00:00", 270, 14, 6, 90)]),
            Routing("second", [(49, -124, "10/01/2026, 13:00:00", 260, 12, 5, 80)]),
        ]
        for route in old_routes:
            self.controller.core.routingManager.append(route)
        panel = self.window.routing_panel
        panel.refresh_routes(old_routes[1])
        panel.coastlines.setChecked(False)
        panel.pages.setCurrentIndex(1)
        self.window.resize(1000, 650)
        self.window.addDockWidget(
            Qt.DockWidgetArea.LeftDockWidgetArea, self.window.routing_dock
        )
        self.window.nmea_dock.hide()
        self.window.findChild(QToolBar, "navigation").hide()
        self.window._show_panel(self.window.routing_dock)
        self.load()
        self.controller.session.speed = 5
        self.controller.session.set_streaming(False)
        self.controller.session.set_playing(True)
        self.controller.session.advance(1)
        self.window.views.setCurrentWidget(self.window.logs_view)
        (
            self.window.map._center_lat,
            self.window.map._center_lon,
            self.window.map._zoom,
        ) = (44.2, -127.1, 7.5)
        self.window.map.view_changed.emit()
        workspace = self.window.workspace
        workspace.save()
        payload = workspace.store.load()
        self.assertEqual(payload["routing"]["selected_route"], "second")
        self.assertNotIn("race.log", json.dumps(payload))
        for key in (
            "source",
            "log",
            "points",
            "cursor",
            "position",
            "recording",
            "playing",
        ):
            self.assertNotIn(key, payload["nmea"])
        with patch(
            "gweatherrouting.qt.workspace.workareas", return_value=[(0, 0, 1600, 1000)]
        ):
            self.restart_workspace(routes=old_routes)
        self.assertEqual(self.window.size().width(), 1000)
        self.assertEqual(self.window.size().height(), 650)
        self.assertTrue(self.window.nmea_dock.isHidden())
        toolbar = self.window.findChild(QToolBar, "navigation")
        self.assertTrue(toolbar.isHidden())
        toolbar.toggleViewAction().trigger()
        self.assertFalse(toolbar.isHidden())
        self.assertEqual(
            self.window.dockWidgetArea(self.window.routing_dock),
            Qt.DockWidgetArea.LeftDockWidgetArea,
        )
        self.assertIs(self.window.views.currentWidget(), self.window.logs_view)
        self.assertEqual(self.window.routing_panel.pages.currentIndex(), 1)
        self.assertEqual(self.window.routing_panel.routes.currentData().name, "second")
        self.assertFalse(self.window.routing_panel.coastlines.isChecked())
        self.assertAlmostEqual(self.window.map._center_lat, 44.2)
        self.assertAlmostEqual(self.window.map._center_lon, -127.1)
        self.assertAlmostEqual(self.window.map._zoom, 7.5)
        self.assertEqual(self.controller.session.speed, 5)
        self.assertFalse(self.controller.session.streaming)
        self.assertEqual(self.controller.session.source, "live")
        self.assertIsNone(self.controller.session.log)
        self.assertFalse(self.controller.session.playing)
        self.assertFalse(self.controller.session.recording)
        self.assertEqual(self.controller.points, [])
        self.assertEqual(self.controller.session.position, 0)

    def test_workspace_debounce_waits_for_drag_and_ignores_playback_cursor_updates(
        self,
    ):
        from PySide6.QtTest import QTest

        self.load()
        self.controller.session.set_playing(True)
        workspace = self.window.workspace
        workspace.timer.stop()
        workspace.last_saved = workspace.save_state()
        with patch.object(workspace.store, "save") as save:
            for _ in range(20):
                self.controller.session.advance(0.05)
            self.drain()
            QTest.qWait(850)
            self.assertEqual(save.call_count, 0)
            self.window.map.drag = QPointF(10, 10)
            for index in range(20):
                self.window.map._center_lon = -130 + index / 10
                self.window.map.view_changed.emit()
            QTest.qWait(850)
            self.assertEqual(save.call_count, 0)
            self.window.map.drag = None
            self.window.map.view_changed.emit()
            QTest.qWait(850)
            self.assertEqual(save.call_count, 1)
            workspace.changed()
            QTest.qWait(850)
            self.assertEqual(save.call_count, 1)

    def test_workspace_invalid_layout_and_removed_monitor_are_recoverable(self):
        from gweatherrouting.core.workspacestate import WorkspaceStateStore

        state = {
            "window": {"x": 9000, "y": -9000, "width": 5000, "height": 4000},
            "layout": "invalid base64!",
            "view": "unknown",
            "active_dock": "unknown",
            "map": {"latitude": True, "longitude": 900, "zoom": -20},
            "nmea": {"speed": True, "streaming": "yes"},
            "routing": {"polar": "missing", "algorithm": "missing", "page": 999},
            "docks": {
                "routing": {
                    "floating": True,
                    "visible": True,
                    "geometry": {"x": 6000, "y": 6000, "width": 450, "height": 500},
                }
            },
        }

        def write(path):
            WorkspaceStateStore(path).save(state)

        with patch(
            "gweatherrouting.qt.workspace.workareas", return_value=[(0, 0, 1280, 800)]
        ):
            self.restart_workspace(write)
        self.assertEqual(self.window.width(), 1280)
        self.assertEqual(self.window.height(), 800)
        self.assertTrue(self.window.routing_dock.isFloating())
        geometry = self.window.routing_dock.geometry()
        self.assertGreaterEqual(geometry.x(), 0)
        self.assertGreaterEqual(geometry.y(), 0)
        self.assertLessEqual(geometry.right(), 1280)
        self.assertLessEqual(geometry.bottom(), 800)
        self.assertAlmostEqual(self.window.map._center_lat, 48.4)
        self.assertEqual(self.window.map._center_lon, 180)
        self.assertEqual(self.window.map._zoom, 1)
        self.assertEqual(self.controller.session.speed, 1)
        self.assertTrue(self.controller.session.streaming)
        self.assertEqual(self.window.routing_panel.pages.currentIndex(), 0)
        self.assertIs(self.window.views.currentWidget(), self.window.map)
        self.window.workspace.reset_layout()
        self.drain()
        self.assertFalse(self.window.routing_dock.isFloating())
        self.assertFalse(self.window.nmea_dock.isHidden())
        self.assertFalse(self.window.routing_dock.isHidden())
        self.assertIn(
            self.window.routing_dock,
            self.window.tabifiedDockWidgets(self.window.nmea_dock),
        )

    def test_workspace_floating_dock_and_maximized_window_preserve_normal_size(self):
        self.window.resize(1000, 650)
        self.drain()
        self.window.routing_dock.setFloating(True)
        self.window.routing_dock.setGeometry(100, 100, 430, 500)
        self.window.showMaximized()
        self.drain()
        payload = self.window.workspace.save_state()
        self.assertTrue(payload["window"]["maximized"])
        self.assertEqual(payload["window"]["width"], 1000)
        with patch(
            "gweatherrouting.qt.workspace.workareas", return_value=[(0, 0, 1600, 1000)]
        ):
            self.restart_workspace()
        self.assertTrue(self.window.isMaximized())
        self.assertEqual(self.window.normalGeometry().width(), 1000)
        self.assertTrue(self.window.routing_dock.isFloating())
        self.assertEqual(self.window.routing_dock.width(), 430)
        self.assertEqual(self.window.routing_dock.height(), 500)
        self.window.showNormal()
        self.drain()
        self.assertEqual(self.window.width(), 1000)
        self.assertEqual(self.window.height(), 650)

    def test_workspace_failed_atomic_save_retains_good_file_and_retries(self):
        workspace = self.window.workspace
        workspace.save()
        previous = workspace.store.path.read_bytes()
        self.window.map._zoom += 1
        with patch(
            "gweatherrouting.core.workspacestate.os.replace",
            side_effect=OSError("disk full"),
        ):
            workspace.save()
        self.assertIn("disk full", workspace.error)
        self.assertEqual(workspace.store.path.read_bytes(), previous)
        workspace.save()
        self.assertEqual(workspace.error, "")
        self.assertNotEqual(workspace.store.path.read_bytes(), previous)
        self.assertFalse(list(self.path.glob(".workspace-*.tmp")))
        gtk = self.path / "workspace.json"
        gtk.write_text('{"version":1,"state":{"active_tab":"Polars"}}')
        self.window.close()
        self.assertEqual(json.loads(gtk.read_text())["state"]["active_tab"], "Polars")
        self.assertFalse(workspace.timer.isActive())

    def polar_library(self):
        from gweatherrouting.core.polarmanager import PolarManager

        refresh_patch = patch(
            "gweatherrouting.core.polarmanager.PolarManager.refresh_local_polars",
            ORIGINAL_POLAR_REFRESH,
        )
        refresh_patch.start()
        self.addCleanup(refresh_patch.stop)
        root = self.path / "polars"
        root.mkdir(exist_ok=True)
        for target in (
            "gweatherrouting.core.polarmanager.POLAR_DIR",
            "gweatherrouting.qt.polarcontroller.POLAR_DIR",
        ):
            mock = patch(target, str(root))
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch("gweatherrouting.core.storage.DATA_DIR", str(self.path))
        mock.start()
        self.addCleanup(mock.stop)
        self.controller.polars.close()
        with (
            patch.object(Storage, "load_or_save_default", ORIGINAL_STORAGE_LOAD),
            patch.object(Storage, "save", ORIGINAL_STORAGE_SAVE),
        ):
            manager = PolarManager()
        self.controller.core.polar_manager = manager
        from gweatherrouting.qt.polarcontroller import PolarController

        self.controller.polars = PolarController(self.controller)
        self.controller.polars.changed.connect(self.window.routing_panel.refresh_polars)
        self.controller.polars.changed.connect(
            self.window.polar_workspace.refresh_selector
        )
        self.controller.polars.use_requested.connect(self.window._use_polar)
        self.window.routing_panel.refresh_polars()
        from gweatherrouting.qt.polarlibrary import PolarLibrary

        self.window.polar_manager_panel = PolarLibrary(self.controller, self.window)
        self.window.polar_manager_dock.setWidget(self.window.polar_manager_panel)
        self.window.show_polars()
        panel = self.window.polar_manager_panel
        return root, panel

    def wait_for_polars(self, error=False):
        deadline = time.monotonic() + 8
        while self.controller.polars.task.busy and time.monotonic() < deadline:
            self.drain()
            time.sleep(0.005)
        self.drain()
        self.assertFalse(self.controller.polars.task.busy)
        if not error:
            self.assertFalse(self.controller.polars.error, self.controller.polars.error)

    def test_polar_library_native_import_selection_preview_table_and_shared_disk_reload(
        self,
    ):
        import shutil
        from gweatherrouting.core.polarmanager import PolarManager

        root, panel = self.polar_library()
        source = self.path / "custom.pol"
        shutil.copyfile(
            Path(__file__).parents[1] / "gweatherrouting/data/polars/first317.pol",
            source,
        )
        with (
            patch.object(Storage, "save", ORIGINAL_STORAGE_SAVE),
            patch(
                "gweatherrouting.qt.polarlibrary.QFileDialog.getOpenFileName",
                return_value=(str(source), ""),
            ) as picker,
        ):
            panel.import_button.click()
            self.wait_for_polars()
            picker.assert_called_once()
            self.assertEqual(panel.selected_name(), "custom.pol")
            self.assertIn(
                "custom.pol",
                [
                    self.window.routing_panel.polar.itemText(i)
                    for i in range(self.window.routing_panel.polar.count())
                ],
            )
            panel.preview_button.click()
            self.wait_for_polars()
            preview = panel.preview_dialog
            self.assertIsNotNone(preview)
            self.assertTrue(preview.isVisible())
            from gweatherrouting.core.polarfiles import read_polar

            polar = read_polar(source)
            self.assertEqual(preview.table.rowCount(), len(polar.twa))
            self.assertEqual(preview.table.columnCount(), len(polar.tws) + 1)
            self.assertEqual(
                float(preview.table.item(10, 3).text()), polar.speed_table[10][2]
            )
            line = preview.axis.lines[0]
            wind_index = next(i for i, wind in enumerate(polar.tws) if wind > 0)
            self.assertEqual(list(line.get_xdata()), polar.twa)
            self.assertEqual(
                list(line.get_ydata()), [row[wind_index] for row in polar.speed_table]
            )
            preview.close()
            panel.use_button.click()
            self.assertEqual(
                self.window.routing_panel.polar.currentText(), "custom.pol"
            )
            self.assertIs(self.window.views.currentWidget(), self.window.map)
            self.assertTrue(self.window.routing_panel.isVisible())
            panel.local.item(panel.names.index("custom.pol"), 0).setCheckState(
                Qt.CheckState.Unchecked
            )
            self.drain()
            self.assertNotIn(
                "custom.pol",
                [
                    self.window.routing_panel.polar.itemText(i)
                    for i in range(self.window.routing_panel.polar.count())
                ],
            )
            panel.local.item(panel.names.index("custom.pol"), 0).setCheckState(
                Qt.CheckState.Checked
            )
            self.wait_for_polars()
            with patch.object(Storage, "load_or_save_default", ORIGINAL_STORAGE_LOAD):
                reloaded = PolarManager()
            self.assertIn("custom.pol", reloaded.polars)
            self.assertTrue(source.exists())
            self.assertEqual((root / "custom.pol").read_bytes(), source.read_bytes())

    def test_polar_orc_filter_download_metadata_failure_and_installed_selection(self):
        from test.test_polarmanager import ORC_DATA, response

        root, panel = self.polar_library()
        entries = [
            ["USA/123", "Test yacht", "First 31.7"],
            ["CAN/456", "Other yacht", "J/120"],
        ]
        with patch(
            "gweatherrouting.core.polarfiles.requests.get",
            return_value=response(entries),
        ):
            panel.fetch_button.click()
            self.wait_for_polars()
        self.assertEqual(panel.catalog_filter.rowCount(), 2)
        panel.search.setText("first")
        self.drain()
        self.assertEqual(panel.catalog_filter.rowCount(), 1)
        panel.remote.selectRow(0)
        self.drain()
        self.assertTrue(panel.download_button.isEnabled())
        with (
            patch.object(Storage, "save", ORIGINAL_STORAGE_SAVE),
            patch(
                "gweatherrouting.core.polarfiles.requests.get",
                return_value=response(ORC_DATA),
            ),
        ):
            panel.download_button.click()
            self.wait_for_polars()
        self.assertTrue((root / "USA_123.pol").exists())
        self.assertEqual(
            self.controller.core.polar_manager.get_metadata("USA_123.pol")["name"],
            "Test yacht",
        )
        self.assertFalse(panel.download_button.isEnabled())
        panel.search.setText("other")
        panel.remote.selectRow(0)
        self.drain()
        import requests

        previous = list(self.controller.core.polar_manager.polars)
        with patch(
            "gweatherrouting.core.polarfiles.requests.get",
            side_effect=requests.ConnectionError("offline"),
        ):
            panel.download_button.click()
            self.wait_for_polars(error=True)
        self.assertIn("offline", self.controller.polars.error)
        self.assertEqual(self.controller.core.polar_manager.polars, previous)
        self.assertFalse((root / "CAN_456.pol").exists())

    def test_polar_changes_block_while_routing_and_remove_selected_polar_updates_selector(
        self,
    ):
        root, panel = self.polar_library()
        manager = self.controller.core.polar_manager
        selected = self.window.routing_panel.polar.currentText()
        self.controller.routing.busy = True
        self.controller.routing.changed.emit()
        self.assertFalse(panel.import_button.isEnabled())
        self.assertFalse(self.controller.polars.import_file(root / selected))
        self.controller.polars.remove(selected)
        self.assertTrue((root / selected).exists())
        self.controller.routing.busy = False
        self.controller.routing.changed.emit()
        with patch(
            "gweatherrouting.qt.polarlibrary.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            panel.local.selectRow(panel.names.index(selected))
            panel.remove_button.click()
        self.assertNotIn(selected, manager.polars)
        self.assertNotIn(selected, manager.polars_files)
        self.assertFalse((root / selected).exists())
        self.assertNotEqual(self.window.routing_panel.polar.currentText(), selected)
        self.assertFalse(self.window.routing_panel.calculate.isEnabled())

    def test_polar_worker_close_drops_late_install_without_enabling_it(self):
        root, panel = self.polar_library()
        started, release = Event(), Event()
        polars = self.controller.polars

        def slow(_):
            started.set()
            release.wait(3)
            return ("late.pol", None)

        self.assertTrue(polars.run("install", "Importing…", slow))
        self.assertTrue(started.wait(1))
        self.window.views.setCurrentWidget(self.window.logs_view)
        self.drain()
        self.assertIs(self.window.views.currentWidget(), self.window.logs_view)
        polars.close()
        release.set()
        polars.task.worker_thread.join(2)
        self.drain()
        self.assertNotIn("late.pol", polars.manager.polars)
        self.assertEqual(polars.error, "")

    def wait_for_polar_view(self):
        deadline = time.monotonic() + 5
        while (
            self.window.polar_workspace.chart.polar is None
            and time.monotonic() < deadline
        ):
            self.drain()
            time.sleep(0.005)
        self.drain()
        self.assertIsNotNone(
            self.window.polar_workspace.chart.polar,
            self.window.polar_workspace.status.text(),
        )

    def test_gtk_workspace_structure_polar_table_full_circle_and_context_toolbar(self):
        workspace = self.window.polar_workspace
        self.assertEqual(
            [self.window.workspace_tabs.tabText(i) for i in range(3)],
            ["Charts", "Logs", "Polars"],
        )
        self.window.workspace_tabs.setCurrentIndex(2)
        self.wait_for_polar_view()
        self.assertIs(self.window.views.currentWidget(), workspace)
        self.assertTrue(workspace.table.isVisible())
        self.assertTrue(workspace.chart.isVisible())
        self.assertTrue(workspace.chart.full_circle)
        self.assertFalse(self.window.polar_selector_dock.isHidden())
        self.assertFalse(self.window.performance_dock.isHidden())
        self.assertTrue(all(a.isVisible() for a in self.window.polar_toolbar_actions))
        self.assertFalse(any(a.isVisible() for a in self.window.chart_toolbar_actions))
        polar = workspace.chart.polar
        step = int(len(polar.twa) / 10) if len(polar.twa) > 20 else 1
        self.assertEqual(workspace.table.rowCount(), len(polar.twa[::step]))
        self.assertEqual(
            float(workspace.table.item(1, 2).text()), polar.speed_table[step][1]
        )
        self.window.grab()
        self.drain()
        xs = [
            x for ax, _, bx, _, _ in workspace.chart._curve_segments for x in (ax, bx)
        ]
        center = workspace.chart._chart_center[0]
        self.assertLess(min(xs), center)
        self.assertGreater(max(xs), center)
        self.window.workspace_tabs.setCurrentIndex(1)
        self.assertIs(self.window.views.currentWidget(), self.window.logs_view)
        self.assertTrue(all(a.isVisible() for a in self.window.log_toolbar_actions))
        self.assertFalse(any(a.isVisible() for a in self.window.polar_toolbar_actions))
        self.window.workspace_tabs.setCurrentIndex(0)
        self.assertTrue(all(a.isVisible() for a in self.window.chart_toolbar_actions))

    def test_gtk_performance_controls_use_shared_history_basis_band_and_clear_only_samples(
        self,
    ):
        from gweatherrouting.core.polarperformance import PerformanceSample

        workspace = self.window.polar_workspace
        self.window.show_polar_workspace()
        self.wait_for_polar_view()
        history = self.controller.session.active_history
        samples = [
            PerformanceSample(45, 10, 5, "stw"),
            PerformanceSample(315, 10, 6, "stw"),
            PerformanceSample(90, 14, 7, "stw"),
            PerformanceSample(60, 10, 8, "sog"),
            PerformanceSample(60, 100, 8, "stw"),
        ]
        with history.lock:
            history.samples.extend(samples)
            history.revision += 1
        controls = workspace.performance
        controls.refresh()
        self.assertEqual(workspace.chart.samples, tuple(samples[:3]))
        self.assertEqual(controls.count.text(), "3 / 4 STW samples in range")
        controls.wind.setCurrentIndex(controls.wind.findData(10))
        self.assertEqual(workspace.chart.samples, tuple(samples[:2]))
        self.assertEqual(
            workspace.chart.wind_filter, list(workspace.chart.polar.tws).index(10)
        )
        controls.basis.setCurrentIndex(1)
        self.assertEqual(workspace.chart.samples, (samples[3],))
        controls.show_points.setChecked(False)
        self.assertEqual(workspace.chart.samples, ())
        self.load()
        points = tuple(self.controller.points)
        source_log = self.controller.session.log
        controls.clear.click()
        self.assertEqual(self.controller.session.active_history.snapshot()[1], ())
        self.assertIs(self.controller.session.log, source_log)
        self.assertEqual(tuple(self.controller.points), points)

    def test_gtk_logs_layout_full_data_and_direction_switches_preserve_shared_playback(
        self,
    ):
        log = self.load()
        self.assertIs(self.window.views.currentWidget(), self.window.logs_view)
        self.assertIs(self.window.logs_view.widget(0), self.window.log_map)
        self.assertIs(self.window.logs_view.widget(1), self.window.plots)
        self.controller.session.set_playing(True)
        self.controller.session.advance(5)
        self.drain()
        self.assertEqual(self.window.log_map.position, 5)
        from gweatherrouting.core.timecontrol import naive_wall_time

        formatter = self.window.plots.axes[-1].xaxis.get_major_formatter()
        self.assertEqual(
            formatter(log.point_offsets[0], 0),
            naive_wall_time(log.points[0].time).strftime("%H:%M:%S"),
        )
        self.assertEqual(self.window.map.position, 5)
        self.assertEqual(self.window.log_map.points, self.window.map.points)
        self.assertEqual(len(self.window.log_map.points), len(log.points))
        chart_time = self.controller.time_control.time
        self.assertIsNot(self.window.log_map.time_control, self.window.map.time_control)
        self.window.log_toggles["aws"].setChecked(True)
        self.window.log_toggles["twa"].setChecked(True)
        groups = self.window.plots._plot_groups()
        self.assertEqual([field for field, _, _ in groups[1][1]], ["aws", "tws"])
        self.assertEqual(
            [field for field, _, _ in groups[-1][1]],
            ["awd", "awa", "twd", "twa", "hdg"],
        )
        from types import SimpleNamespace

        point = SimpleNamespace(awa=50, twa=70, hdg=350)
        self.assertEqual(self.window.plots._value(point, "awd"), 40)
        self.assertEqual(self.window.plots._value(point, "twd"), 60)
        self.window.log_toggles["depth"].setChecked(True)
        self.assertEqual(len(self.window.plots.axes), 4)
        self.assertEqual(self.controller.time_control.time, chart_time)
        self.assertEqual(self.controller.session.position, 5)
        self.assertTrue(self.controller.session.playing)
        self.window.show_polar_workspace()
        self.window.views.setCurrentWidget(self.window.logs_view)
        self.assertEqual(self.controller.session.position, 5)
        self.assertEqual(
            len(self.window.plots.axes[0].lines[0].get_xdata()), len(log.points)
        )

    def test_gtk_polar_preferences_log_map_and_panel_placement_roundtrip(self):
        workspace = self.window.polar_workspace
        self.window.show_polar_workspace()
        self.wait_for_polar_view()
        workspace.performance.basis.setCurrentIndex(1)
        workspace.performance.wind.setCurrentIndex(
            workspace.performance.wind.findData(12)
        )
        workspace.performance.show_points.setChecked(False)
        (
            self.window.log_map._center_lat,
            self.window.log_map._center_lon,
            self.window.log_map._zoom,
        ) = (41, -70, 8)
        self.window.log_toggles["depth"].setChecked(True)
        self.window.performance_dock.hide()
        self.window.polar_selector_dock.setFloating(True)
        self.window.polar_selector_dock.setGeometry(100, 100, 430, 400)
        self.restart_workspace()
        self.wait_for_polar_view()
        self.assertIs(self.window.views.currentWidget(), self.window.polar_workspace)
        self.assertTrue(self.window.polar_selector_dock.isFloating())
        self.assertTrue(self.window.performance_dock.isHidden())
        self.assertEqual(
            self.window.polar_workspace.performance.basis.currentIndex(), 1
        )
        self.assertEqual(self.window.polar_workspace.performance.wind.currentData(), 12)
        self.assertFalse(
            self.window.polar_workspace.performance.show_points.isChecked()
        )
        self.assertEqual(
            (
                self.window.log_map._center_lat,
                self.window.log_map._center_lon,
                self.window.log_map._zoom,
            ),
            (41, -70, 8),
        )
        self.assertTrue(self.window.log_toggles["depth"].isChecked())

    def test_gtk_workspace_header_stays_above_context_toolbar_for_old_qt_layout(self):
        from PySide6.QtWidgets import QMainWindow
        from gweatherrouting.core.workspacestate import WorkspaceStateStore

        old = QMainWindow()
        toolbar = QToolBar("Navigation", old)
        toolbar.setObjectName("navigation")
        old.addToolBar(toolbar)
        encoded = bytes(old.saveState(1).toBase64()).decode("ascii")
        old.close()

        def write(path):
            WorkspaceStateStore(path).save({"layout": encoded, "view": "Charts"})

        self.restart_workspace(write)
        self.drain()
        self.assertLess(
            self.window.workspace_toolbar.y(), self.window.navigation_toolbar.y()
        )
        self.window.show_polar_workspace()
        self.assertTrue(self.window.polar_selector_dock.toggleViewAction().isVisible())
        self.window.views.setCurrentWidget(self.window.map)
        self.assertTrue(self.window.polar_selector_dock.toggleViewAction().isVisible())
        self.assertTrue(self.window.performance_dock.toggleViewAction().isVisible())

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
