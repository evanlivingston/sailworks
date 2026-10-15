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
        from gweatherrouting.core.marine.manager import MarineManager

        marine_factory = patch(
            "gweatherrouting.core.core.MarineManager",
            side_effect=lambda _path, **kwargs: MarineManager(
                self.path / "marine", **kwargs
            ),
        )
        marine_factory.start()
        self.addCleanup(marine_factory.stop)
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
        for canvas in (self.window.map, self.window.log_map):
            canvas.marine._renderer._land_mask.directory = self.path / "shoreline"
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

    def test_floating_panels_redock_and_reset_without_losing_content(self):
        from gweatherrouting.qt.dockchrome import DockTitleBar, DOCK_MIN_WIDTH

        dock = self.window.nmea_dock
        content = dock.widget()
        for _ in range(2):
            dock.setFloating(True)
            self.drain()
            self.assertIsInstance(dock.titleBarWidget(), DockTitleBar)
            self.assertTrue(dock.windowFlags() & Qt.WindowType.FramelessWindowHint)
            dock.resize(100, 400)
            self.assertGreaterEqual(dock.width(), DOCK_MIN_WIDTH)
            dock.setFloating(False)
            self.drain()
            self.assertIsNone(dock.titleBarWidget())
            self.assertIs(dock.widget(), content)
        dock.setFloating(True)
        self.window.reset_panels()
        self.drain()
        self.assertFalse(dock.isFloating())
        self.assertIs(dock.widget(), content)
        self.assertFalse(dock.isHidden())

    def test_floating_panel_child_coordinates_do_not_trigger_edge_resize(self):
        from PySide6.QtTest import QTest
        from PySide6.QtCore import QPoint
        from gweatherrouting.qt.dockchrome import _FloatingFrame

        dock = self.window.nmea_dock
        self.window._show_panel(dock)
        dock.setFloating(True)
        dock.resize(430, 600)
        self.drain()
        frame = dock.findChild(_FloatingFrame)
        child = self.window.record
        # The button's local left edge is in the middle of the panel.
        QTest.mousePress(child, Qt.MouseButton.LeftButton, pos=QPoint(2, 10))
        self.assertIsNone(frame._resize)
        QTest.mouseRelease(child, Qt.MouseButton.LeftButton, pos=QPoint(2, 10))
        QTest.mousePress(dock, Qt.MouseButton.LeftButton, pos=QPoint(1, 100))
        self.assertIsNotNone(frame._resize)
        QTest.mouseRelease(dock, Qt.MouseButton.LeftButton, pos=QPoint(1, 100))
        self.assertIsNone(frame._resize)

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

    def test_local_tcp_and_udp_feed_reaches_ui_and_timestamped_recording(self):
        import socket

        for transport in ("tcp", "udp"):
            with self.subTest(transport=transport):
                server = socket.socket(
                    socket.AF_INET,
                    socket.SOCK_STREAM if transport == "tcp" else socket.SOCK_DGRAM,
                )
                server.bind(("127.0.0.1", 0))
                server.settimeout(5)
                if transport == "tcp":
                    server.listen(1)
                stop = Event()
                failures = []

                def feed():
                    try:
                        if transport == "tcp":
                            connection, _ = server.accept()
                            with connection:
                                while not stop.is_set():
                                    payload = (rmc("120000") + "\r\n").encode("ascii")
                                    connection.sendall(payload[:12])
                                    connection.sendall(payload[12:])
                                    stop.wait(0.03)
                        else:
                            while not stop.is_set():
                                sources = tuple(
                                    self.controller.connections.sources.values()
                                )
                                if sources:
                                    server.sendto(
                                        (rmc("120000") + "\n").encode("ascii"),
                                        sources[0].s.getsockname(),
                                    )
                                stop.wait(0.03)
                    except (OSError, TimeoutError) as exc:
                        if (
                            not stop.is_set()
                            and self.controller.connections.polling_active
                        ):
                            failures.append(exc)

                self.controller.connections.storage.connections = [
                    {
                        "type": "network",
                        "protocol": "nmea0183",
                        "direction": "in",
                        "host": "127.0.0.1",
                        "port": server.getsockname()[1],
                        "network": transport,
                    }
                ]
                self.controller.offline = False
                worker = Thread(target=feed, daemon=True)
                worker.start()
                received = []
                callback = lambda boat: received.append((get_ident(), boat.latitude))
                self.controller.core.connect("boat_position", callback)
                try:
                    self.controller.session.select_source("live")
                    self.controller.session.start_recording()
                    recording = self.controller.session.recorder.path
                    self.controller.start_connections()
                    self.wait_for_routing_stage(lambda: bool(received))
                    self.assertAlmostEqual(received[-1][1], 48.1173)
                    self.assertEqual(received[-1][0], get_ident())
                    self.assertIn("48.11730", self.window.instruments.text())
                    self.assertIn("$GPRMC", recording.read_text())
                    self.assertGreater(
                        self.controller.connections.get_status()[0]["packets"], 0
                    )
                    self.controller.session.stop_recording()
                    before = len(self.controller.points)
                    self.controller.connections.stop_polling()
                    self.assertIsNone(self.controller.connections.thread)
                    self.assertGreaterEqual(len(self.controller.points), before)
                    self.assertTrue(recording.exists())
                finally:
                    self.controller.session.stop_recording()
                    self.controller.connections.stop_polling()
                    self.controller.core.disconnect("boat_position", callback)
                    stop.set()
                    server.close()
                    worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertFalse(failures)

    @unittest.skipUnless(sys.platform != "win32", "POSIX pseudo-terminal validation")
    def test_local_serial_feed_runs_through_production_reader_and_ui(self):
        import os
        import pty
        import tty

        master, slave = pty.openpty()
        tty.setraw(slave)
        stop = Event()

        def feed():
            while not stop.is_set():
                os.write(master, (rmc("120000") + "\r\n").encode("ascii"))
                stop.wait(0.03)

        worker = Thread(target=feed, daemon=True)
        self.controller.connections.storage.connections = [
            {
                "type": "serial",
                "protocol": "nmea0183",
                "direction": "in",
                "data-port": os.ttyname(slave),
                "baudrate": 4800,
            }
        ]
        self.controller.offline = False
        worker.start()
        try:
            self.controller.start_connections()
            self.wait_for_routing_stage(
                lambda: "48.11730" in self.window.instruments.text()
            )
            self.assertAlmostEqual(self.controller.core.boat_info.latitude, 48.1173)
            self.assertIn("48.11730", self.window.instruments.text())
            self.assertTrue(self.controller.connections.get_status()[0]["connected"])
        finally:
            self.controller.connections.stop_polling()
            stop.set()
            worker.join(2)
            os.close(master)
            os.close(slave)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(self.controller.connections.thread)

    def test_large_file_keeps_every_log_point_and_plot_value_during_playback(self):
        count = 30000
        path = self.path / "large.nmea"
        with path.open("w") as stream:
            for index in range(count):
                hours, seconds = divmod(index, 3600)
                minutes, seconds = divmod(seconds, 60)
                stream.write(rmc(f"{hours:02}{minutes:02}{seconds:02}") + "\n")
        self.controller.load_path(str(path))
        self.wait_for_routing_stage(lambda: not self.controller.session.loading)
        self.assertEqual(len(self.controller.session.log.entries), count)
        self.assertEqual(len(self.window.log_map.points), count)
        self.assertEqual(len(self.window.plots.points), count)
        for axis in self.window.plots.axes:
            self.assertEqual(len(axis.lines[0].get_xdata()), count)
        self.controller.session.set_playing(True)
        self.controller.session.advance(30)
        self.drain()
        self.assertEqual(len(self.window.plots.points), count)
        self.assertEqual(self.window.plots.position, 30)

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
        self.controller.time_control.set_time(old_time + timedelta(hours=3))
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

    def plan_test_waypoints(self):
        from gweatherrouting.core.geo.track import Track

        track = Track("Test course", [(0, 0, None), (0, 0.15, None)])
        self.controller.core.trackManager.append(track)
        panel = self.window.routing_panel
        panel.refresh_destinations()
        panel.destination.setCurrentText("Track: Test course")
        panel.start_mode.setCurrentText("First waypoint")

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
        self.assertEqual(
            self.window.navigation_panel.route_points.rowCount(), len(route.points)
        )
        self.assertEqual(
            self.window.map.isochrone_frame.isochrones, tuple(route.isochrones)
        )
        self.assertEqual(
            self.window.map.route, tuple((p[0], p[1]) for p in route.points)
        )
        self.assertIn("saved", panel.status.text())
        self.assertEqual(panel.pages.tabText(panel.pages.currentIndex()), "Plan")
        self.assertNotIn(
            "Results", [panel.pages.tabText(i) for i in range(panel.pages.count())]
        )
        self.assertEqual(
            self.window.map.route_speeds, tuple(p[5] or 0 for p in route.points)
        )
        self.window.navigation_panel.route_points.setCurrentCell(0, 0)
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

    def test_live_isochrones_reach_map_before_routing_finishes_and_render_off_thread(
        self,
    ):
        from gweatherrouting.core.routingjob import CalculatedRoute, RoutingPreview
        from test.test_routingjob import ConstantWind

        self.plan_test_waypoints()
        self.controller.core.grib_manager.gribs = [ConstantWind()]
        started, release = Event(), Event()
        self.addCleanup(release.set)
        frontiers = (((0, 0.02), (0.01, 0.02), (0.02, 0.02)),)

        def calculate(core, request, sources, providers, cancel, progress, **options):
            progress(25, RoutingPreview(((0, 0), (0.01, 0.01)), frontiers))
            started.set()
            release.wait(15)
            return CalculatedRoute(((0, 0, "2026-10-01", 0, 1, 1, 0),), (), {})

        with patch(
            "gweatherrouting.qt.routingcontroller.calculate_route",
            side_effect=calculate,
        ):
            self.window.routing_panel.calculate.click()
            self.assertTrue(started.wait(1))
            self.wait_for_routing_stage(
                lambda: self.window.map.isochrone_frame.isochrones == frontiers
            )
            self.assertTrue(self.controller.routing.busy)
            canvas = self.window.map
            canvas.fit_coordinates([(0, 0), (0.03, 0.03)])
            threads = []
            render = canvas.isochrones._render

            def tracked(job):
                threads.append(get_ident())
                return render(job)

            canvas.isochrones._render = tracked
            canvas._navigation_image()
            self.wait_for_routing_stage(
                lambda: canvas in canvas.isochrones._states
                and canvas.isochrones._states[canvas].frame is not None
                and canvas.isochrones._states[canvas].frame[0].viewport._center_lat
                == canvas._center_lat
                and canvas.isochrones._states[canvas].frame[0].viewport._center_lon
                == canvas._center_lon
                and canvas.isochrones._states[canvas].frame[0].viewport._zoom
                == canvas._zoom
            )
            self.assertTrue(threads)
            self.assertTrue(all(t != get_ident() for t in threads))
            frame = canvas.isochrones._states[canvas].frame[1]
            self.assertTrue(any(bytes(frame.get_data())))
            self.controller.routing.cancel()
            self.assertFalse(canvas.isochrone_frame.isochrones)
            self.controller.routing._apply_progress(
                self.controller.routing.generation, 50, (), frontiers
            )
            self.assertFalse(canvas.isochrone_frame.isochrones)
            release.set()
            self.wait_for_route()

    def test_routing_cancel_and_close_keep_completed_routes_and_drop_late_results(self):
        self.plan_test_waypoints()
        from test.test_routingjob import ConstantWind
        from gweatherrouting.core.routingjob import CalculatedRoute

        self.controller.core.grib_manager.gribs = [ConstantWind()]
        from gweatherrouting.core.geo.routing import Routing

        existing = Routing("existing", [(0, 0, "2026-10-01", 0, 1, 1, 0)])
        self.controller.core.routingManager.append(existing)
        self.window._select_saved_route(existing)
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
        self.plan_test_waypoints()
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
        for canvas in (self.window.map, self.window.log_map):
            canvas.marine._renderer._land_mask.directory = self.path / "shoreline"
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
        self.window._select_saved_route(old_routes[1])
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
        self.assertEqual(self.window.navigation_panel.route.name, "second")
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

    def test_navigation_tabs_tracks_pois_and_map_placement(self):
        from PySide6.QtTest import QTest
        from PySide6.QtCore import QPoint

        panel = self.window.navigation_panel
        self.assertEqual(
            [panel.tabs.tabText(i) for i in range(4)],
            ["Tracks", "Routings", "POIs", "AIS"],
        )
        self.assertFalse(panel.tabs.isTabVisible(3))
        self.assertFalse(self.window.navigation_dock.isHidden())
        self.window.views.setCurrentWidget(self.window.logs_view)
        self.assertTrue(self.window.navigation_dock.isHidden())
        self.window.views.setCurrentWidget(self.window.map)
        self.assertFalse(self.window.navigation_dock.isHidden())
        self.window.navigation_action.trigger()
        self.window.views.setCurrentWidget(self.window.polar_workspace)
        self.window.views.setCurrentWidget(self.window.map)
        self.assertTrue(self.window.navigation_dock.isHidden())
        self.window._new_track()
        track = self.controller.core.trackManager.get_active()
        self.assertIsNotNone(track)
        panel.add_point((48.4, -123.4))
        panel.add_point((48.5, -123.5))
        panel.track_points.setCurrentCell(0, 0)
        panel.edit_point("duplicate")
        self.assertEqual(
            track.points,
            [(48.4, -123.4, None), (48.5, -123.5, None), (48.4, -123.4, None)],
        )
        panel.track_points.setCurrentCell(1, 0)
        panel.edit_point("move_up")
        self.assertEqual(track.points[0][:2], (48.5, -123.5))
        panel.tracks.item(0, 0).setText("Race course")
        self.assertEqual(track.name, "Race course")
        panel.tracks.item(0, 3).setCheckState(Qt.CheckState.Unchecked)
        self.assertFalse(track.visible)
        self.window._navigation_request("poi", (48.45, -123.45))
        poi = panel.poi
        self.assertEqual(self.window.map.navigation.hl_poi, poi.name)
        self.assertGreater(self.window.routing_panel.destination.findText(poi.name), 0)
        panel.pois.item(0, 0).setText("Finish")
        self.assertEqual(poi.name, "Finish")
        self.assertGreater(self.window.routing_panel.destination.findText("Finish"), 0)
        panel.move_poi()
        self.assertIsNotNone(self.window.map.move_callback)
        QTest.keyClick(self.window.map, Qt.Key.Key_Escape)
        self.assertIsNone(self.window.map.move_callback)
        self.assertEqual(poi.position, (48.45, -123.45))
        panel.move_poi()
        location = self.window.map.convert_screen_to_geographic(120, 100).get_degrees()
        QTest.mouseClick(
            self.window.map, Qt.MouseButton.LeftButton, pos=QPoint(120, 100)
        )
        self.assertEqual(poi.position, location)
        self.assertIsNone(self.window.map.move_callback)
        panel.track_points.setCurrentCell(0, 0)
        panel.move_point()
        location = self.window.map.convert_screen_to_geographic(140, 110).get_degrees()
        QTest.mouseClick(
            self.window.map, Qt.MouseButton.LeftButton, pos=QPoint(140, 110)
        )
        self.assertEqual(track.points[0], (*location, None))
        panel.remove_poi()
        self.assertFalse(self.controller.core.poiManager.elements)
        destinations = self.window.routing_panel.destination
        self.assertEqual(
            [destinations.itemText(i) for i in range(destinations.count())],
            ["Choose track or POI…", "Track: " + track.name],
        )
        panel.remove_track()
        self.assertIsNone(self.controller.core.trackManager.get_active())
        self.assertEqual(panel.track_points.rowCount(), 0)
        self.assertIsNone(self.window.map.navigation.hl_poi)

    def test_navigation_gpx_native_picker_import_export_and_invalid_input(self):
        import gpxpy
        from gweatherrouting.qt.navigationpanel import gpx_document
        from gweatherrouting.core.geo.routing import Routing

        panel = self.window.navigation_panel
        source = self.path / "course.gpx"
        source.write_text(
            '<gpx version="1.1"><wpt lat="48.5" lon="-123.5"><name>Finish</name></wpt>'
            '<trk><name>Course</name><trkseg><trkpt lat="48.1" lon="-123.1"/>'
            '<trkpt lat="48.2" lon="-123.2"/></trkseg></trk></gpx>'
        )
        with patch(
            "gweatherrouting.qt.navigationpanel.QFileDialog.getOpenFileName",
            return_value=(str(source), ""),
        ) as picker:
            panel.import_gpx()
            picker.assert_called_once()
        self.wait_for_navigation()
        track = self.controller.core.trackManager.get_active()
        self.assertEqual(track.points, [[48.1, -123.1], [48.2, -123.2]])
        self.assertEqual(panel.pois.item(0, 0).text(), "Finish")
        panel.import_path(source)
        self.wait_for_navigation()
        self.assertEqual(len(self.controller.core.trackManager), 2)
        self.assertEqual(len({t.name for t in self.controller.core.trackManager}), 2)
        bad = self.path / "bad.gpx"
        bad.write_text('<gpx><wpt lat="91" lon="-123"/></gpx>')
        previous = self.controller.core.poiManager.to_json()
        panel.import_path(bad)
        self.wait_for_navigation()
        self.assertEqual(self.controller.core.poiManager.to_json(), previous)
        self.assertIn("Could not import", panel.status.text())
        route = Routing(
            "Route",
            [(48.1, -123.1, "2026-10-14T10:00:00", 45, 10, 5, 0)],
            collection=self.controller.core.routingManager,
        )
        self.controller.core.routingManager.append(route)
        panel.route = route
        for method, filename in (
            (panel.export_track, "track.gpx"),
            (panel.export_route, "route.gpx"),
            (panel.export_all, "all.gpx"),
            (panel.export_pfec, "pois.nmea"),
        ):
            destination = self.path / filename
            with patch(
                "gweatherrouting.qt.navigationpanel.QFileDialog.getSaveFileName",
                return_value=(str(destination), ""),
            ) as picker:
                method()
                picker.assert_called_once()
            if filename == "pois.nmea":
                self.assertEqual(
                    destination.read_text(),
                    self.controller.core.poiManager.to_nmea_fpec(),
                )
            else:
                parsed = gpxpy.parse(destination.read_text())
                self.assertEqual(
                    (len(parsed.tracks), len(parsed.routes), len(parsed.waypoints)),
                    {
                        "track.gpx": (1, 0, 0),
                        "route.gpx": (0, 1, 0),
                        "all.gpx": (2, 1, 2),
                    }[filename],
                )
        self.assertTrue(gpx_document().to_xml())

    def wait_for_navigation(self):
        deadline = time.monotonic() + 5
        while self.window.navigation_panel.task.busy and time.monotonic() < deadline:
            self.drain()
            time.sleep(0.005)
        self.assertFalse(self.window.navigation_panel.task.busy)
        self.drain()

    def test_navigation_ais_sorting_visibility_expiry_and_map(self):
        from gweatherrouting.core.aismanager import AISTarget, STALE_TIMEOUT
        from gweatherrouting.qt.navigationpanel import distance_nm

        panel = self.window.navigation_panel
        manager = self.controller.core.ais_manager
        self.controller.core.set_boat_position(48.4, -123.4)
        near = AISTarget(123456789, 48.41, -123.4, 90, 5, 90, "Near", 36)
        far = AISTarget(987654321, 49.4, -123.4, 180, 10, 180, "Far", 70)
        invalid = AISTarget(100000000)
        with manager._lock:
            manager._targets = {t.mmsi: t for t in (far, near, invalid)}
        panel.refresh_ais()
        self.assertTrue(panel.tabs.isTabVisible(3))
        self.assertEqual(panel.ais.item(0, 0).text(), str(near.mmsi))
        self.assertEqual(panel.ais.rowCount(), 2)
        self.assertEqual(self.window.log_map.ais_targets, (near, far))
        self.assertEqual(self.window.map.ais_targets, (near, far))
        self.assertAlmostEqual(distance_nm(0, 0, 1, 0), 60.04046, places=4)
        panel.fit_ais(0)
        self.assertEqual(self.window.map._center_lat, near.latitude)
        self.window.ais_action.setChecked(False)
        self.assertFalse(self.window.map.ais_visible)
        self.assertFalse(self.window.log_map.ais_visible)
        for target in (near, far, invalid):
            target.last_update = time.time() - STALE_TIMEOUT - 1
        panel.refresh_ais()
        self.assertFalse(panel.tabs.isTabVisible(3))
        self.assertFalse(self.window.map.ais_targets)

    def test_navigation_route_controls_and_collection_persistence(self):
        from gweatherrouting.core.geo.routing import Routing
        from gweatherrouting.core.geo.trackcollection import TrackCollection
        from gweatherrouting.core.geo.poicollection import POICollection

        panel = self.window.navigation_panel
        manager = self.controller.core.routingManager
        route = Routing(
            "Course",
            [
                (48.4, -123.4, "2026-10-14T10:00:00Z", 45, 10, 5, 0),
                (48.5, -123.5, "2026-10-14T11:00:00Z", 50, 12, 6, 20),
            ],
            collection=manager,
        )
        manager.append(route)
        panel.refresh()
        panel.routes.setCurrentCell(0, 0)
        self.assertIs(self.window.navigation_panel.route, route)
        self.assertEqual(self.window.map.navigation.hl_routing, "Course")
        self.assertEqual(panel.route_points.rowCount(), 2)
        panel.route_points.setCurrentCell(1, 0)
        self.assertEqual(self.window.map.selected_route_point, 1)
        panel.routes.item(0, 0).setText("Renamed")
        self.assertEqual(self.window.navigation_panel.route.name, "Renamed")
        panel.routes.item(0, 2).setCheckState(Qt.CheckState.Unchecked)
        self.assertFalse(route.visible)
        with (
            patch("gweatherrouting.core.storage.DATA_DIR", str(self.path)),
            patch.object(Storage, "save", ORIGINAL_STORAGE_SAVE),
        ):
            for collection in (
                self.controller.core.trackManager,
                self.controller.core.poiManager,
            ):
                ORIGINAL_STORAGE_LOAD(collection.storage)
            panel.new_track()
            panel.add_point((48.4, -123.4))
            panel.add_poi((48.5, -123.5))
            panel.move_poi()
            self.window.map.move_callback((48.6, -123.6))
            self.window.map.cancel_move()
            with patch.object(Storage, "load_or_save_default", ORIGINAL_STORAGE_LOAD):
                tracks, pois = TrackCollection(), POICollection()
            self.assertEqual(tracks.get_active().points, [[48.4, -123.4, None]])
            self.assertEqual(pois[0].position, [48.6, -123.6])
        panel.remove_route()
        self.assertFalse(manager.elements)
        self.assertFalse(self.window.map.route)

    def test_navigation_workspace_restore_and_late_import_shutdown(self):
        from threading import Event

        panel = self.window.navigation_panel
        self.window._show_panel(self.window.navigation_dock)
        panel.tabs.setCurrentIndex(2)
        self.window.ais_action.setChecked(False)
        self.window.workspace.save()
        state = self.window.workspace.store.load()
        self.assertEqual(state["navigation"]["tab"], "POIs")
        self.assertTrue(state["docks"]["chart-navigation"]["visible"])
        panel.tabs.setCurrentIndex(0)
        self.window.ais_action.setChecked(True)
        self.window.navigation_dock.hide()
        self.window.workspace.restoring = True
        self.window.workspace.restore(state)
        self.window.workspace.restoring = False
        self.assertEqual(panel.tabs.currentIndex(), 2)
        self.assertFalse(self.window.ais_action.isChecked())
        self.assertFalse(self.window.navigation_dock.isHidden())
        self.restart_workspace()
        panel = self.window.navigation_panel
        self.assertEqual(panel.tabs.currentIndex(), 2)
        self.assertFalse(self.window.ais_action.isChecked())
        self.assertFalse(self.window.navigation_dock.isHidden())
        event = Event()
        panel.task.start("Pending import", lambda _: event.wait(3))
        self.window.close()
        event.set()
        panel.task.worker_thread.join(2)
        self.drain()
        self.assertFalse(panel.timer.isActive())
        self.assertTrue(panel.task.closed)
        self.assertFalse(self.controller.core.trackManager.elements)

    def wait_for_cursor(self, done):
        deadline = time.monotonic() + 3
        while not done() and time.monotonic() < deadline:
            self.drain()
            time.sleep(0.005)
        self.assertTrue(done())

    def test_map_dashboard_tracks_core_playback_stop_and_source_reset(self):
        canvas = self.window.map
        self.assertTrue(canvas.dashboard_visible)
        self.load(
            [
                rmc("120000"),
                water(),
                wind(reference="R"),
                wind(),
                rmc("120010"),
                rmc("120020"),
            ]
        )
        self.controller.session.set_playing(True)
        self.controller.session.advance(15)
        self.window.views.setCurrentWidget(canvas)
        self.drain()
        canvas._dashboard_image()
        values = {g.label: g.value for g in canvas.dashboard._gauges}
        self.assertEqual(values["SOG"], self.controller.core.boat_info.sog)
        self.assertIsNotNone(values["AWS"])
        self.assertIsNotNone(values["TWS"])
        self.controller.session.stop()
        canvas._dashboard_image()
        self.assertEqual({g.label: g.value for g in canvas.dashboard._gauges}, values)
        background = canvas.background
        self.controller.core.boat_info.depth = 3.2
        self.controller.boat_changed.emit(self.controller.core.boat_info)
        self.drain()
        self.assertIs(canvas.background, background)
        canvas._dashboard_image()
        self.assertEqual(canvas.dashboard._gauges[-1].value, 3.2)
        self.controller.session.select_source("live")
        canvas._dashboard_image()
        self.assertTrue(all(g.value is None for g in canvas.dashboard._gauges))

    def test_map_display_controls_are_independent_and_survive_restart(self):
        self.window.grib_action.trigger()
        self.window.dashboard_action.trigger()
        self.assertFalse(self.window.map.wind_visible)
        self.assertFalse(self.window.map.dashboard_visible)
        self.window.views.setCurrentWidget(self.window.logs_view)
        self.assertTrue(self.window.grib_action.isChecked())
        self.assertTrue(self.window.dashboard_action.isChecked())
        self.assertTrue(self.window.dashboard_action.isVisible())
        self.window.workspace.save()
        self.restart_workspace()
        self.assertFalse(self.window.map.wind_visible)
        self.assertFalse(self.window.map.dashboard_visible)
        self.assertTrue(self.window.log_map.wind_visible)
        self.assertTrue(self.window.log_map.dashboard_visible)
        self.assertTrue(self.window.dashboard_action.isChecked())
        self.window.views.setCurrentWidget(self.window.polar_workspace)
        self.assertFalse(self.window.grib_action.isVisible())
        self.assertFalse(self.window.dashboard_action.isVisible())
        self.window.views.setCurrentWidget(self.window.map)
        self.assertFalse(self.window.dashboard_action.isChecked())
        self.window.grib_action.trigger()
        self.window.dashboard_action.trigger()
        self.assertTrue(self.window.map.dashboard_visible)

    def test_cursor_coordinates_and_hidden_or_moving_weather_never_sample(self):
        from unittest.mock import Mock

        canvas = self.window.map
        canvas._wind_cursor._sample = Mock(return_value=(180, 10))
        self.controller.core.grib_manager.gribs = [object()]
        self.controller.core.grib_manager.get_wind_at = Mock(
            side_effect=AssertionError("UI GRIB query")
        )
        canvas.set_wind_visible(False)
        canvas.mouseMoveEvent(SimpleNamespace(position=lambda: QPointF(100, 100)))
        self.assertIn("Latitude:", self.window.cursor_status.text())
        canvas.set_wind_visible(True)
        canvas.drag = QPointF(100, 100)
        canvas.mouseMoveEvent(SimpleNamespace(position=lambda: QPointF(110, 100)))
        canvas.mouseReleaseEvent(None)
        canvas._zoom_animating = True
        canvas.mouseMoveEvent(SimpleNamespace(position=lambda: QPointF(110, 100)))
        canvas._zoom_animating = False
        self.assertFalse(canvas._wind_cursor._timer)
        canvas._wind_cursor._sample.assert_not_called()
        self.controller.core.grib_manager.get_wind_at.assert_not_called()

    def test_cursor_debounces_and_reads_wind_off_thread_with_ui_delivery(self):
        from unittest.mock import Mock

        canvas = self.window.map
        self.controller.core.grib_manager.gribs = [object()]
        ui_thread = get_ident()
        workers, delivered = [], []

        def sample(*args):
            workers.append(get_ident())
            return (180, 10)

        canvas._wind_cursor._sample = Mock(side_effect=sample)
        canvas.cursor_readout.connect(
            lambda text: delivered.append((get_ident(), text))
        )
        for x in range(30):
            canvas.mouseMoveEvent(
                SimpleNamespace(position=lambda x=x: QPointF(100 + x, 100))
            )
        self.assertIn("Latitude:", self.window.cursor_status.text())
        self.assertNotIn("Wind", self.window.cursor_status.text())
        self.wait_for_cursor(lambda: "19.4 kts" in self.window.cursor_status.text())
        canvas._wind_cursor._sample.assert_called_once()
        self.assertTrue(self.window.cursor_status.isVisible())
        self.window.refresh()
        self.assertTrue(self.window.cursor_status.isVisible())
        self.assertNotEqual(workers[0], ui_thread)
        self.assertTrue(all(thread == ui_thread for thread, text in delivered))
        self.assertEqual(canvas.background[0][4], canvas._zoom)

    def test_cursor_stale_results_cancel_after_view_time_sources_visibility_and_leave(
        self,
    ):
        from unittest.mock import Mock
        from datetime import timedelta
        from PySide6.QtCore import QEvent

        canvas = self.window.map
        self.controller.core.grib_manager.gribs = [object()]
        canvas._wind_cursor.request = Mock()
        for change in (
            lambda: self.controller.time_control.set_time(
                self.controller.time_control.time + timedelta(hours=1)
            ),
            lambda: setattr(canvas, "_zoom", canvas._zoom + 1),
            lambda: self.controller.core.grib_manager.gribs.append(object()),
            lambda: canvas.set_wind_visible(False),
            lambda: canvas.leaveEvent(QEvent(QEvent.Type.Leave)),
            lambda: self.window.views.setCurrentWidget(self.window.logs_view),
        ):
            self.window.views.setCurrentWidget(canvas)
            canvas.set_wind_visible(True)
            canvas.mouseMoveEvent(SimpleNamespace(position=lambda: QPointF(120, 100)))
            callback = canvas._wind_cursor.request.call_args.args[-1]
            change()
            current = self.window.cursor_status.text()
            callback((180, 10))
            self.assertEqual(self.window.cursor_status.text(), current)

    def test_cursor_private_real_forecast_and_blocked_close_release_handles(self):
        from datetime import datetime
        from gweatherrouting.core.grib import Grib
        from gweatherrouting.rendering.windcursor import CursorGrib

        source = Grib(
            str(self.wind_fixture()), "wind", "test", [], datetime(2026, 1, 1), 3
        )
        self.controller.core.grib_manager.gribs = [source]
        self.controller.time_control.set_time(source.start_time)
        canvas = self.window.map
        canvas.fit_coordinates([(48.99, -123.01), (49.01, -122.99)])
        canvas.mouseMoveEvent(
            SimpleNamespace(
                position=lambda: QPointF(canvas.width() / 2, canvas.height() / 2)
            )
        )
        expected = source.get_wind_at(source.start_time, 49, -123)
        self.wait_for_cursor(lambda: "Wind" in self.window.cursor_status.text())
        self.assertIn(
            "%.1f kts" % (expected[1] * 1.94384), self.window.cursor_status.text()
        )
        private = canvas._wind_cursor._gribs[source]
        self.assertIsNot(private.dataset, source.dataset)
        started, release, done = Event(), Event(), Event()
        self.addCleanup(release.set)

        def slow(forecast, lat, lon):
            started.set()
            release.wait(3)
            done.set()
            return expected

        private.get_wind_at = slow
        canvas.mouseMoveEvent(
            SimpleNamespace(
                position=lambda: QPointF(canvas.width() / 2 + 1, canvas.height() / 2)
            )
        )
        self.wait_for_cursor(started.is_set)
        self.window.close()
        release.set()
        self.wait_for_cursor(lambda: done.is_set() and not canvas._wind_cursor._gribs)
        self.assertIsNone(private.dataset)
        self.assertTrue(canvas._wind_cursor._closed)
        self.assertIsNotNone(source.dataset)
        self.assertTrue(issubclass(type(private), CursorGrib))

    def timeline_range(self):
        from datetime import datetime

        start = datetime(2026, 1, 1, 12)
        self.controller.core.grib_manager.gribs = [
            SimpleNamespace(start_time=start, last_forecast=1)
        ]
        self.window.timeline.refresh()
        self.controller.time_control.set_time(start)
        return start

    def test_forecast_timeline_enabled_metadata_updates_and_post_2038_slider(self):
        from datetime import datetime, timedelta
        from gweatherrouting.core.marine.manager import MarineManager
        from gweatherrouting.qt.forecasttimeline import ForecastTimeline
        from test.test_marine_fields import field

        marine = MarineManager(self.path / "timeline-marine")
        gribs = SimpleNamespace(gribs=[])
        proxy = SimpleNamespace(
            time_control=self.controller.time_control,
            scheduler=self.controller.scheduler,
            core=SimpleNamespace(grib_manager=gribs, marine_manager=marine),
        )
        timeline = ForecastTimeline(proxy, self.window)
        self.addCleanup(timeline.dispose)
        self.assertFalse(timeline.slider.isEnabled())
        self.assertIn("Load wind", timeline.caption.text())
        current = field()
        marine.fields.add(current)
        self.drain()
        self.assertEqual(
            timeline.bounds, (float(current.times[0]), float(current.times[-1]))
        )
        marine.fields.enable(current.id, False)
        self.drain()
        self.assertIsNone(timeline.bounds)
        start = datetime(2040, 1, 1)
        gribs.gribs.append(SimpleNamespace(start_time=start, last_forecast=6))
        timeline.refresh()
        self.controller.time_control.set_time(start)
        self.assertEqual(timeline.slider.maximum(), 21600)
        timeline.slider.setValue(10800)
        timeline.slider.sliderReleased.emit()
        self.assertEqual(
            self.controller.time_control.time,
            start + timedelta(hours=3),
        )
        self.assertIn("2040", timeline.start_label.toolTip())
        gribs.gribs.clear()
        timeline.refresh()
        self.assertFalse(timeline.slider.isEnabled())

    def test_forecast_timeline_steps_playback_boundaries_and_now(self):
        from datetime import timedelta, datetime

        start = self.timeline_range()
        timeline = self.window.timeline
        self.assertEqual(timeline.seconds, 900)
        timeline.forward_button.click()
        self.assertEqual(
            self.controller.time_control.time, start + timedelta(minutes=15)
        )
        timeline.backward_button.click()
        self.assertEqual(self.controller.time_control.time, start)
        self.controller.time_control.set_time(start + timedelta(minutes=55))
        timeline.play_button.click()
        timer = timeline.play_timer.timerId()
        timeline.play_button.click()
        self.assertEqual(timeline.play_timer.timerId(), timer)
        timeline.tick()
        self.assertEqual(self.controller.time_control.time, start + timedelta(hours=1))
        self.assertFalse(timeline.playing)
        self.assertFalse(timeline.play_button.isChecked())
        timeline.step_combo.setCurrentText("30s")
        timeline.backward_button.click()
        self.assertEqual(
            self.controller.time_control.time, start + timedelta(minutes=59, seconds=30)
        )
        with patch(
            "gweatherrouting.core.timecontrol.datetime.datetime"
        ) as clock_datetime:
            clock_datetime.now.return_value = datetime(2026, 1, 5, 14, 32)
            timeline.now_button.click()
        self.assertEqual(self.controller.time_control.time, datetime(2026, 1, 5, 14))
        self.assertIn("outside", timeline.caption.text())
        self.assertEqual(timeline.slider.value(), timeline.slider.maximum())
        timeline.pause_button.click()
        self.assertFalse(timeline.playing)

    def test_forecast_timeline_scrubbing_coalesces_pauses_and_cancels_pending_drag(
        self,
    ):
        from datetime import timedelta

        start = self.timeline_range()
        timeline = self.window.timeline
        changes = []
        self.controller.time_control.connect("time_change", changes.append)
        self.addCleanup(
            lambda: self.controller.time_control.disconnect(
                "time_change", changes.append
            )
        )
        timeline.play_button.click()
        for value in (600, 900, 1800):
            timeline.slider.setValue(value)
        self.assertFalse(timeline.playing)
        self.assertFalse(changes)
        timeline.slider.sliderReleased.emit()
        self.assertEqual(changes, [start + timedelta(minutes=30)])
        timeline.slider.setValue(3000)
        self.controller.time_control.increase(minutes=10)
        timeline.slider.sliderReleased.emit()
        self.assertEqual(
            self.controller.time_control.time, start + timedelta(minutes=40)
        )
        self.assertFalse(timeline.commit_timer.isActive())
        timeline.slider.setValue(2700)
        self.wait_for_cursor(
            lambda: self.controller.time_control.time == start + timedelta(minutes=45)
        )
        self.assertFalse(timeline.commit_timer.isActive())

    def test_forecast_timeline_manual_date_cancel_and_unbounded_accept(self):
        from datetime import timedelta
        from PySide6.QtWidgets import QDialog, QDateTimeEdit
        from PySide6.QtCore import QDateTime

        start = self.timeline_range()
        timeline = self.window.timeline
        target = start + timedelta(days=3)

        def cancel(dialog):
            dialog.findChild(QDateTimeEdit).setDateTime(QDateTime(target))
            return QDialog.DialogCode.Rejected

        timeline.play()
        with patch.object(QDialog, "exec", cancel):
            timeline.select_button.click()
        self.assertFalse(timeline.playing)
        self.assertEqual(self.controller.time_control.time, start)

        def accept(dialog):
            dialog.findChild(QDateTimeEdit).setDateTime(QDateTime(target))
            return QDialog.DialogCode.Accepted

        with patch.object(QDialog, "exec", accept):
            timeline.select_button.click()
        self.assertEqual(self.controller.time_control.time, target)
        self.assertIn("outside", timeline.caption.text())
        timeline.backward_button.click()
        self.assertEqual(self.controller.time_control.time, start + timedelta(hours=1))

    def test_forecast_timeline_is_independent_of_nmea_and_restores_only_step(self):
        from datetime import timedelta

        self.load()
        self.controller.session.set_playing(True)
        self.controller.session.advance(5)
        self.window.views.setCurrentWidget(self.window.map)
        start = self.timeline_range()
        timeline = self.window.timeline
        timeline.play_button.click()
        timeline.tick()
        self.assertEqual(self.controller.session.position, 5)
        self.assertTrue(self.controller.session.playing)
        self.window.views.setCurrentWidget(self.window.logs_view)
        self.drain()
        self.assertFalse(timeline.playing)
        self.assertTrue(self.controller.session.playing)
        self.assertTrue(self.window.forecast_toolbar.isHidden())
        self.assertEqual(
            self.controller.time_control.time, start + timedelta(minutes=15)
        )
        self.window.views.setCurrentWidget(self.window.map)
        self.assertFalse(self.window.forecast_toolbar.isHidden())
        timeline.step_combo.setCurrentText("3h")
        timeline.play()
        self.window.workspace.save()
        payload = self.window.workspace.store.load()
        self.assertEqual(payload["forecast"], {"step": "3h"})
        self.restart_workspace()
        self.assertEqual(self.window.timeline.step_combo.currentText(), "3h")
        self.assertFalse(self.window.timeline.playing)
        self.assertFalse(self.controller.session.playing)
        self.window.navigation_toolbar.hide()
        self.assertTrue(self.window.forecast_toolbar.isHidden())
        self.window.navigation_toolbar.show()
        self.assertFalse(self.window.forecast_toolbar.isHidden())

    def test_forecast_timeline_close_cancels_drag_poll_and_clock_callbacks(self):
        start = self.timeline_range()
        timeline = self.window.timeline
        clock, marine = timeline.clock, timeline.marine
        timeline.slider.setValue(1200)
        self.assertTrue(timeline.commit_timer.isActive())
        self.window.close()
        self.assertTrue(timeline.closed)
        self.assertFalse(timeline.commit_timer.isActive())
        self.assertFalse(timeline.play_timer.isActive())
        self.assertFalse(timeline.poll_timer.isActive())
        self.assertFalse(clock.handlers["time_change"])
        self.assertNotIn(timeline._marine_changed, marine.handlers["changed"])
        self.assertNotIn(timeline._marine_changed, marine.fields.handlers["changed"])
        timeline.commit()
        self.assertFalse(timeline.tick())
        self.assertEqual(clock.time, start)
        # QWidgetAction releases widgets during toolbar destruction, after their
        # child controls may already be gone. A late hide must avoid those controls.
        from PySide6.QtGui import QHideEvent
        from shiboken6 import delete

        delete(timeline.play_button)
        timeline.hideEvent(QHideEvent())

    def marine_fixture(self):
        from dataclasses import replace
        from test.test_marine_fields import field
        from test.test_marine import dataset, START
        from gweatherrouting.core.marine.models import Sample, Series

        current = field()
        tide = dataset()
        station = replace(
            tide.station, latitude=49.5, longitude=-125.5, series_codes=("wlp",)
        )
        tide = replace(
            tide,
            station=station,
            series=(
                Series(
                    "water_level",
                    "m",
                    (
                        Sample(START, 2.5, "2"),
                        Sample(tide.requested_end, 3.5, "2"),
                    ),
                ),
            ),
        )
        self.controller.time_control.set_time(START.astimezone().replace(tzinfo=None))
        (
            self.window.map._center_lat,
            self.window.map._center_lon,
            self.window.map._zoom,
        ) = (49.5, -125.5, 9)
        self.window.map.queue_draw()
        return current, tide

    def wait_for_marine(self, predicate):
        deadline = time.monotonic() + 8
        while not predicate() and time.monotonic() < deadline:
            self.drain()
            time.sleep(0.005)
        self.assertTrue(predicate())
        self.drain()

    def test_marine_current_sources_group_selection_removal_and_clock_coverage(self):
        from dataclasses import replace

        current, _ = self.marine_fixture()
        manager = self.controller.core.marine_manager
        other = replace(current, east=current.east * 2)
        manager.fields.add(current)
        manager.fields.add(other)
        self.drain()
        panel = self.window.marine_panels["currents"]
        self.assertIn("Current data available", panel.coverage.text())
        root = panel.sources.tree.topLevelItem(0)
        self.assertEqual(root.childCount(), 2)
        from shiboken6 import isValid

        child = root.child(0)
        child.setCheckState(0, Qt.CheckState.Unchecked)
        self.assertTrue(
            isValid(child)
        )  # The checkbox's native signal is still returning.
        self.drain()
        root = panel.sources.tree.topLevelItem(0)
        self.assertEqual(root.checkState(0), Qt.CheckState.PartiallyChecked)
        self.assertEqual(len(manager.fields.snapshot().fields), 1)
        root.setCheckState(0, Qt.CheckState.Unchecked)
        self.drain()
        self.assertFalse(manager.fields.snapshot().fields)
        self.assertIsNone(self.window.timeline.bounds)
        panel.sources.tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
        self.drain()
        self.controller.time_control.increase(hours=2)
        self.assertIn("Outside the loaded forecast times", panel.coverage.text())
        root = panel.sources.tree.topLevelItem(0)
        identifier = root.child(0).data(0, Qt.ItemDataRole.UserRole)
        panel.sources.tree.setCurrentItem(root.child(0))
        panel.sources.remove.click()
        self.drain()
        self.assertNotIn(identifier, manager.fields.fields)
        self.assertEqual(len(manager.fields.fields), 1)

    def test_marine_tides_heights_display_selection_and_provenance(self):
        current, tide = self.marine_fixture()
        manager = self.controller.core.marine_manager
        manager.add(tide)
        manager.enable(tide.id)
        manager.fields.add(current)
        self.drain()
        panel = self.window.marine_panels["tides"]
        self.assertEqual(panel.sources.item(0, 2).text(), "2.50 m")
        self.assertIn("1 of 1", panel.coverage.text())
        panel.toggle.setChecked(False)
        self.assertFalse(self.window.map.marine.tides_visible)
        self.assertTrue(self.window.map.marine.currents_visible)
        self.assertTrue(manager.is_enabled(tide.id))
        panel.toggle.setChecked(True)
        panel.sources.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        self.drain()
        self.assertFalse(manager.is_enabled(tide.id))
        self.window.show_marine_settings("tides")
        dialog = self.window.marine_settings
        dialog.sources.selectRow(0)
        self.assertEqual(dialog.samples.rowCount(), 2)
        self.assertEqual(dialog.samples.item(0, 4).text(), "2")
        self.assertIn(tide.id, dialog.provenance.toPlainText())
        self.assertIn(tide.attribution, dialog.provenance.toPlainText())
        self.assertIn("datum", dialog.provenance.toPlainText())
        dialog.fallback.setChecked(True)
        self.assertTrue(manager.allow_fallback)
        dialog.sources.item(0, 0).setCheckState(Qt.CheckState.Checked)
        self.drain()
        self.assertTrue(manager.is_enabled(tide.id))
        from dataclasses import replace

        second = replace(tide, attribution="Second saved source")
        manager.add(second)
        self.drain()
        dialog.sources.selectRow(0)
        dialog.down.click()
        self.drain()
        self.assertEqual(
            [d.id for d in manager.ordered_datasets()], [second.id, tide.id]
        )
        self.assertEqual(dialog.selected_id(), tide.id)
        dialog.up.click()
        self.drain()
        self.assertEqual(
            [d.id for d in manager.ordered_datasets()], [tide.id, second.id]
        )
        dialog.remove.click()
        self.drain()
        self.assertNotIn(tide.id, manager.datasets)
        dialog.sources.selectRow(0)
        dialog.remove.click()
        self.drain()
        self.assertEqual(panel.sources.rowCount(), 0)

    def test_marine_current_download_freezes_request_and_commits_on_ui_thread(self):
        current, _ = self.marine_fixture()
        manager = self.controller.core.marine_manager
        panel = self.window.marine_panels["currents"]
        panel.hours.setValue(3)
        request = panel.target.area()
        instant = panel.start.instant()
        departure = self.window.routing_panel.departure.dateTime()
        started, release = Event(), Event()
        self.addCleanup(release.set)

        def download(area, start, end, cancel, report):
            self.assertEqual((area, start), (request, instant))
            self.assertEqual((end - start).total_seconds(), 10800)
            report("Fixture provider downloading…")
            started.set()
            release.wait(5)
            return [current], ["Second provider unavailable"]

        commits = []
        original_add = manager.fields.add

        def add(value):
            commits.append(get_ident())
            return original_add(value)

        with (
            patch.object(
                manager.coverage_planner, "download", side_effect=download
            ) as operation,
            patch.object(manager.fields, "add", side_effect=add),
        ):
            panel.download.click()
            self.wait_for_marine(started.is_set)
            self.assertFalse(panel.download.isEnabled())
            self.assertTrue(panel.jobs.busy)
            (
                self.window.map._center_lat,
                self.window.map._center_lon,
                self.window.map._zoom,
            ) = (0, 0, 5)
            self.window.map.queue_draw()
            self.controller.time_control.increase(hours=4)
            release.set()
            self.wait_for_marine(lambda: not panel.jobs.busy)
            operation.assert_called_once()
        self.assertEqual(commits, [get_ident()])
        self.assertIn(current.id, manager.fields.fields)
        self.assertIn("Second provider unavailable", panel.status.text())
        self.assertFalse(self.controller.session.recording)
        self.assertEqual(departure, self.window.routing_panel.departure.dateTime())

    def test_marine_cancel_and_close_discard_late_downloads_and_progress(self):
        current, _ = self.marine_fixture()
        panel = self.window.marine_panels["currents"]
        manager = self.controller.core.marine_manager
        for close in (False, True):
            started, release, returned = Event(), Event(), Event()
            self.addCleanup(release.set)

            def download(area, start, end, cancel, report):
                started.set()
                release.wait(5)
                report("Late progress")
                returned.set()
                return [current], []

            with patch.object(
                manager.coverage_planner, "download", side_effect=download
            ):
                panel.download.click()
                self.wait_for_marine(started.is_set)
                if close:
                    self.window.close()
                else:
                    panel.cancel.click()
                release.set()
                self.wait_for_marine(returned.is_set)
                if not close:
                    self.wait_for_marine(lambda: not panel.jobs.busy)
                    self.assertIn("Cancelled", panel.status.text())
                self.assertNotIn(current.id, manager.fields.fields)
        self.assertTrue(panel.closed)
        self.assertTrue(panel.jobs.closed)
        self.assertFalse(panel.timer.isActive())
        self.assertFalse(self.window.map.marine._renderer._states)
        self.assertNotIn(panel.sources_changed, manager.handlers["changed"])

    def test_marine_tide_route_download_across_date_line_and_saved_reuse(self):
        from dataclasses import replace
        from unittest.mock import Mock

        _, tide = self.marine_fixture()
        panel = self.window.marine_panels["tides"]
        station = replace(tide.station, latitude=10, longitude=179.9)
        tide = replace(tide, station=station)
        far = replace(station, id="example:far", longitude=0)
        points = [(10, 179.8), (10, -179.8)]
        route = SimpleNamespace(name="Date line route", to_list=lambda: list(points))
        panel.target.core = SimpleNamespace(trackManager=[route], routingManager=[])
        panel.target.refresh()
        panel.target.setCurrentIndex(1)
        panel.hours.setValue(1)
        panel.provider = Mock()
        started, release = Event(), Event()
        self.addCleanup(release.set)

        def discover(cancel):
            started.set()
            release.wait(5)
            return station, far

        panel.provider.discover.side_effect = discover
        panel.provider.download.return_value = tide
        panel.download.click()
        self.wait_for_marine(started.is_set)
        points[:] = [(0, 0)]
        release.set()
        self.wait_for_marine(lambda: not panel.jobs.busy)
        panel.provider.download.assert_called_once()
        self.assertEqual(panel.provider.download.call_args.args[0], station)
        self.assertTrue(panel.manager.is_enabled(tide.id))
        self.assertEqual(panel.target.currentIndex(), 1)
        # Reuse a saved interval whose actual samples cover the request.
        panel.hours.setValue(1)
        panel.start.set_time(tide.requested_start)
        points[:] = [(10, 179.8), (10, -179.8)]
        # The fixture covers 15 minutes, so use an explicit coherent hour window.
        from gweatherrouting.core.marine.models import Series, Sample
        from datetime import timedelta

        end = tide.requested_start + timedelta(hours=1)
        tide = replace(
            tide,
            requested_end=end,
            series=(
                Series(
                    "water_level",
                    "m",
                    tuple(
                        Sample(
                            tide.requested_start + timedelta(minutes=15 * i), 2.5, "2"
                        )
                        for i in range(5)
                    ),
                ),
            ),
        )
        panel.manager.add(tide)
        panel.manager.enable(tide.id, False)
        panel.provider.download.reset_mock()
        panel.download.click()
        self.wait_for_marine(lambda: not panel.jobs.busy)
        panel.provider.download.assert_not_called()
        self.assertTrue(panel.manager.is_enabled(tide.id))
        self.assertIn("reused 1", panel.status.text())

    def test_marine_panel_preferences_and_download_start_survive_view_switch(self):
        from datetime import timedelta

        current, _ = self.marine_fixture()
        self.controller.core.marine_manager.fields.add(current)
        panel = self.window.marine_panels["currents"]
        instant = panel.start.instant()
        self.controller.time_control.increase(hours=1)
        self.assertEqual(panel.start.instant(), instant + timedelta(hours=1))
        panel.start.edited()
        self.controller.time_control.increase(hours=1)
        self.assertEqual(panel.start.instant(), instant + timedelta(hours=1))
        self.window.marine_action.trigger()
        self.drain()
        self.assertFalse(
            self.window.marine_panels["currents"].visibleRegion().isEmpty()
        )
        self.assertTrue(self.window.marine_open["currents"])
        self.assertTrue(self.window.marine_open["tides"])
        panel.toggle.setChecked(False)
        self.window.views.setCurrentWidget(self.window.logs_view)
        self.assertTrue(self.window.marine_docks["currents"].isHidden())
        self.assertFalse(self.window.marine_action.isVisible())
        self.window.workspace.save()
        payload = self.window.workspace.store.load()
        self.assertEqual(
            payload["marine"],
            {
                "open": {"currents": True, "tides": True},
                "currents": False,
                "tides": True,
            },
        )
        self.restart_workspace()
        self.assertFalse(self.window.marine_panels["currents"].toggle.isChecked())
        self.assertFalse(self.window.marine_panels["currents"].jobs.busy)
        self.window.views.setCurrentWidget(self.window.map)
        self.assertFalse(self.window.marine_docks["currents"].isHidden())
        self.window.views.setCurrentWidget(self.window.logs_view)
        self.window.reset_panels()
        self.assertFalse(any(self.window.marine_open.values()))

    def test_marine_overlay_uses_shared_worker_and_cursor_values(self):
        import cairo

        current, tide = self.marine_fixture()
        manager = self.controller.core.marine_manager
        manager.fields.add(current)
        manager.add(tide)
        manager.enable(tide.id)
        canvas = self.window.map
        renderer = canvas.marine._renderer
        with patch.object(
            renderer._land_mask,
            "surface",
            side_effect=lambda view: cairo.ImageSurface(
                cairo.FORMAT_A8, view.width, view.height
            ),
        ):
            canvas.queue_draw()
            canvas.repaint()
            self.wait_for_marine(
                lambda: renderer._states.get(canvas) is not None
                and renderer._states[canvas].frame is not None
            )
            job, surface = renderer._states[canvas].frame
            self.assertTrue(any(bytes(surface.get_data())))
            self.assertEqual(job.palette, tide.requested_start)
            self.assertEqual(renderer.schedule, self.controller.scheduler.submit)
            self.assertIn(
                "Current 4.3 kn toward 27°T", canvas.marine.cursor_text(49.5, -125.5)
            )
            canvas._hover(QPointF(canvas.width() / 2, canvas.height() / 2))
            self.assertIn("Test currents", self.window.cursor_status.text())
            canvas.hide()
            self.assertFalse(renderer._states)
        self.assertFalse(self.window.log_map.marine.visible)

    def test_marine_account_controls_use_system_store_without_saved_secrets(self):
        from test.test_marine_credentials import MemoryKeyring
        from gweatherrouting.core.marine.credentials import SystemCredentialStore
        from gweatherrouting.core.marine.spatialproviders import CopernicusProvider

        provider = CopernicusProvider(SystemCredentialStore(MemoryKeyring()))
        self.controller.core.marine_manager.coverage_planner.worldwide = provider
        self.window.show_settings()
        account = self.window.settings_dialog.currents
        account.username.setText("sailor")
        account.password.setText("test-secret")
        account.save.click()
        self.assertEqual(account.password.text(), "")
        self.wait_for_marine(lambda: not account.task.busy)
        self.assertEqual(provider.credentials, ("sailor", "test-secret"))
        self.assertTrue(provider.credentials_saved)
        self.window.workspace.save()
        self.assertNotIn("test-secret", self.window.workspace.store.path.read_text())
        account.forget.click()
        self.wait_for_marine(lambda: not account.task.busy)
        self.assertIsNone(provider.credentials)
        self.window.settings_dialog.tabs.setCurrentWidget(account)
        self.drain()
        account.password.setText("draft-secret")
        self.window.settings_dialog.hide()
        self.assertEqual(account.password.text(), "")

    def test_routing_parameter_controls_snapshot_metadata_without_global_mutation(self):
        self.plan_test_waypoints()
        panel = self.window.routing_panel
        algorithm = panel.algorithm.currentData()
        original = {key: param.value for key, param in algorithm.PARAMS.items()}
        changes = []
        panel.options_changed.connect(lambda: changes.append(True))
        for key, param in algorithm.PARAMS.items():
            control = panel.parameter_widgets[key]
            self.assertEqual(control.toolTip(), param.tooltip)
            if param.ttype != "bool":
                self.assertEqual(
                    (control.minimum(), control.maximum()), (param.lower, param.upper)
                )
                self.assertEqual(control.singleStep(), param.step)
        panel.parameter_widgets["subdiv"].setValue(3)
        panel.parameter_widgets["concurrent"].setChecked(True)
        request, _ = panel._request()
        panel.parameter_widgets["subdiv"].setValue(4)
        self.assertEqual(dict(request.parameters)["subdiv"], 3)
        self.assertTrue(dict(request.parameters)["concurrent"])
        self.assertEqual(
            {key: param.value for key, param in algorithm.PARAMS.items()}, original
        )
        self.assertTrue(changes)
        panel.use_currents.setChecked(True)
        self.assertEqual(panel.parameter_widgets, {})
        self.assertTrue(panel.coastlines.isChecked())
        panel.use_currents.setChecked(False)
        self.assertEqual(panel.parameter_widgets["subdiv"].value(), 4)

    def test_routing_comparison_scenarios_save_sequentially_on_ui_thread(self):
        from test.test_routingjob import ConstantWind

        self.controller.core.grib_manager.gribs = [ConstantWind()]
        panel = self.window.routing_panel
        panel.coastlines.setChecked(False)
        panel.set_location("start", (0, 0))
        panel.set_location("destination", (0, 0.15))
        for key, text in (
            ("time_offset_hours", "0, 1"),
            ("polar_efficiency_pct", "85, 100"),
        ):
            enabled, entry = panel.comparison_axes[key]
            enabled.setChecked(True)
            entry.setText(text)
        commits = []
        original = self.controller.core.routingManager.append

        def append(route):
            commits.append(get_ident())
            return original(route)

        with patch.object(
            self.controller.core.routingManager, "append", side_effect=append
        ):
            panel.calculate.click()
            self.assertTrue(self.controller.routing.busy)
            self.assertFalse(panel.comparison_axes["time_offset_hours"][1].isEnabled())
            self.wait_for_route()
        self.assertFalse(self.controller.routing.error)
        self.assertEqual(len(self.controller.core.routingManager), 4)
        self.assertEqual(commits, [get_ident()] * 4)
        self.assertEqual(panel.status.text(), "4/4 scenarios complete")
        names = [route.name for route in self.controller.core.routingManager]
        self.assertTrue(names[0].endswith("eff85%"))
        self.assertTrue(names[2].endswith("t+1h_eff85%"))
        self.assertEqual(
            [
                route.provenance["options"]["polar_efficiency"]
                for route in self.controller.core.routingManager
            ],
            [0.85, 1, 0.85, 1],
        )
        panel.comparison_axes["time_offset_hours"][1].setText("nan")
        panel.calculate.click()
        self.assertFalse(self.controller.routing.busy)
        self.assertIn("finite", panel.status.text())
        self.assertEqual(len(self.controller.core.routingManager), 4)

    def wait_for_routing_stage(self, predicate):
        deadline = time.monotonic() + 8
        while not predicate() and time.monotonic() < deadline:
            self.drain()
            time.sleep(0.005)
        self.assertTrue(predicate())

    def test_routing_track_first_point_and_start_poi_freeze_requested_waypoints(self):
        from test.test_routingjob import ConstantWind
        from gweatherrouting.core.geo.track import Track
        from gweatherrouting.core.geo.poi import POI
        from gweatherrouting.core.routingjob import CalculatedRoute

        track = Track("Course", [(0, 0, None), (0, 0.15, None), (0, 0.3, None)])
        self.controller.core.trackManager.append(track)
        poi = POI("Start buoy", (1, 2))
        self.controller.core.poiManager.append(poi)
        self.controller.core.grib_manager.gribs = [ConstantWind()]
        panel = self.window.routing_panel
        panel.refresh_destinations()
        panel.destination.setCurrentText("Track: Course")
        panel.start_mode.setCurrentText("POI: Start buoy")
        self.assertEqual(panel._request()[0].start, (1, 2))
        panel.start_mode.setCurrentText("First waypoint")
        requested, _ = panel._request()
        self.assertIsNone(requested.start)
        self.assertEqual(requested.waypoints, ((0, 0), (0, 0.15), (0, 0.3)))
        self.assertEqual(self.window.map.route_plan, requested.waypoints)
        started, release = Event(), Event()
        self.addCleanup(release.set)
        captured = []

        def calculate(core, request, *args):
            captured.append(request)
            started.set()
            release.wait(5)
            return CalculatedRoute(((0, 0, "2026-10-01", 0, 1, 1, 0),), (), {})

        with patch(
            "gweatherrouting.qt.routingcontroller.calculate_route",
            side_effect=calculate,
        ):
            panel.calculate.click()
            self.wait_for_routing_stage(started.is_set)
            track.points[:] = [(50, 50, None)]
            release.set()
            self.wait_for_route()
        self.assertEqual(captured[0].waypoints, requested.waypoints)
        panel.destination.setCurrentIndex(0)
        panel.calculate.click()
        self.assertFalse(panel.calculate.isEnabled())
        self.assertEqual(self.window.map.route_plan, ())
        self.assertFalse(self.controller.routing.busy)

    def test_routing_tab_restore_migrates_removed_results_page(self):
        pages = self.window.routing_panel.pages
        for old_page, expected in (
            (0, "Plan"),
            (1, "Plan"),
            (2, "Compare"),
            (3, "Currents"),
        ):
            self.window.workspace._restore_routing({"page": old_page})
            self.assertEqual(pages.tabText(pages.currentIndex()), expected)
        self.window.workspace._restore_routing({"page": 1, "page_name": "Compare"})
        self.assertEqual(pages.tabText(pages.currentIndex()), "Compare")

    def test_routing_has_no_coordinate_entry_and_map_selection_survives_refresh(self):
        from PySide6.QtWidgets import QDoubleSpinBox, QPushButton

        panel = self.window.routing_panel
        self.assertNotIn(
            "Manage polars…", [w.text() for w in panel.findChildren(QPushButton)]
        )
        self.assertFalse(
            any(
                w.accessibleName() in ("Latitude", "Longitude")
                for w in panel.findChildren(QDoubleSpinBox)
            )
        )
        self.assertFalse(panel.calculate.isEnabled())
        with self.assertRaisesRegex(ValueError, "Choose a waypoint track"):
            panel._request()
        panel.set_location("start", (1, 2))
        panel.set_location("destination", (3, 4))
        panel.refresh_destinations()
        request, _ = panel._request()
        self.assertEqual(request.start, (1, 2))
        self.assertEqual(request.destination, (3, 4))
        self.assertEqual(self.window.map.route_plan, ((1, 2), (3, 4)))
        self.assertTrue(panel.calculate.isEnabled())
        self.assertFalse(
            any(
                "Custom coordinates" in combo.itemText(i)
                for combo in (panel.start_mode, panel.destination)
                for i in range(combo.count())
            )
        )

    def test_routing_current_engine_preflight_and_provenance_use_shared_models(self):
        from test.test_routingoptions import CurrentWind
        from test.test_current_routing import START, current_field
        from PySide6.QtCore import QDateTime

        current = current_field(east=0.4)
        self.controller.core.marine_manager.fields.add(current)
        self.controller.core.grib_manager.gribs = [CurrentWind()]
        panel = self.window.routing_panel
        panel.use_currents.setChecked(True)
        panel.auto_currents.setChecked(False)
        panel.coastlines.setChecked(False)
        panel.departure.setDateTime(QDateTime(START.astimezone().replace(tzinfo=None)))
        panel.set_location("start", (0, 0))
        panel.set_location("destination", (0, 0.005))
        approvals = []

        def confirm(report):
            approvals.append((get_ident(), report))
            return True

        self.controller.routing.confirm = confirm
        panel.calculate.click()
        self.wait_for_route()
        self.assertFalse(self.controller.routing.error)
        self.assertTrue(approvals)
        self.assertEqual(approvals[0][0], get_ident())
        route = self.controller.core.routingManager[-1]
        self.assertEqual(route.provenance["routing"], "isochrone-with-currents")
        self.assertEqual(route.provenance["currents"]["actual_sources"], [current.id])
        self.assertTrue(route.provenance["samples"])
        routes = self.window.navigation_panel.routes
        self.assertIn("coverage checked", routes.item(routes.currentRow(), 1).text())
        self.assertTrue(route.points[-1][2].endswith("+00:00"))
        self.assertEqual(route.points[-1][3], 270)
        self.assertIn("currents included", panel.status.text())

    def test_routing_blocked_preflight_has_cancel_only_and_close_dismisses_dialog(self):
        from test.test_routingoptions import CurrentWind
        from PySide6.QtCore import QTimer

        self.controller.core.grib_manager.gribs = [CurrentWind()]
        panel = self.window.routing_panel
        panel.use_currents.setChecked(True)
        panel.auto_currents.setChecked(False)
        panel.coastlines.setChecked(False)
        panel.set_location("start", (0, 0))
        panel.set_location("destination", (0, 0.005))
        observed = []
        deadline = time.monotonic() + 5

        def close_dialog():
            dialog = panel.preflight_dialog
            if dialog is None and time.monotonic() < deadline:
                QTimer.singleShot(5, close_dialog)
                return
            if dialog is not None:
                observed.append([button.text() for button in dialog.buttons()])
            self.controller.routing.cancel()

        QTimer.singleShot(5, close_dialog)
        panel.calculate.click()
        self.wait_for_route()
        self.assertEqual(observed, [["Cancel routing"]])
        self.assertFalse(self.controller.core.routingManager.elements)
        self.assertIsNone(panel.preflight_dialog)
        self.assertEqual(panel.status.text(), "Routing cancelled")
        # A genuine parent close also breaks the worker's pending UI handoff.
        observed.clear()
        deadline = time.monotonic() + 5

        def close_window():
            if panel.preflight_dialog is None and time.monotonic() < deadline:
                QTimer.singleShot(5, close_window)
                return
            observed.append(panel.preflight_dialog is not None)
            self.window.close()

        QTimer.singleShot(5, close_window)
        panel.calculate.click()
        worker = self.controller.routing.worker_thread
        self.wait_for_routing_stage(lambda: bool(observed))
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(observed, [True])
        self.assertFalse(self.controller.core.routingManager.elements)

    def test_routing_automatic_download_callbacks_commit_on_ui_thread(self):
        self.plan_test_waypoints()
        from test.test_current_routing import current_field
        from gweatherrouting.core.routingjob import CalculatedRoute

        current = current_field()
        panel = self.window.routing_panel
        panel.use_currents.setChecked(True)
        self.assertTrue(panel.auto_currents.isChecked())
        self.assertFalse(self.controller.core.grib_manager.gribs)
        commits = []
        old_add = self.controller.core.marine_manager.fields.add

        def add(field):
            commits.append(("current", get_ident()))
            return old_add(field)

        def save_wind(gribs):
            commits.append(("wind", get_ident()))

        def calculate(core, request, sources, providers, cancel, progress, **callbacks):
            self.assertTrue(request.auto_download)
            self.assertEqual(sources, ())
            self.assertFalse(callbacks["current_selection"].fields)
            callbacks["report"]("Downloading missing coverage…")
            callbacks["save_fields"]([current])
            callbacks["save_gribs"]([])
            return CalculatedRoute(((0, 0, "2026-10-01", 0, 1, 1, 0),), (), {})

        with (
            patch(
                "gweatherrouting.qt.routingcontroller.calculate_route",
                side_effect=calculate,
            ),
            patch.object(self.controller.routing, "_save_gribs", side_effect=save_wind),
            patch.object(
                self.controller.core.marine_manager.fields, "add", side_effect=add
            ),
        ):
            panel.calculate.click()
            self.wait_for_route()
        self.assertFalse(self.controller.routing.error)
        self.assertEqual(commits, [("current", get_ident()), ("wind", get_ident())])
        self.assertIn(current.id, self.controller.core.marine_manager.fields.fields)
        self.assertEqual(len(self.controller.core.routingManager), 1)

    def test_routing_comparison_cancel_retains_only_completed_scenarios(self):
        self.plan_test_waypoints()
        from test.test_routingjob import ConstantWind
        from gweatherrouting.core.routingjob import CalculatedRoute

        self.controller.core.grib_manager.gribs = [ConstantWind()]
        panel = self.window.routing_panel
        enabled, values = panel.comparison_axes["time_offset_hours"]
        enabled.setChecked(True)
        values.setText("0, 1, 2")
        started, release = Event(), Event()
        self.addCleanup(release.set)
        calls = []

        def calculate(core, request, *args):
            calls.append(request)
            if len(calls) == 2:
                started.set()
                release.wait(5)
            return CalculatedRoute(((0, 0, "2026-10-01", 0, 1, 1, 0),), (), {})

        with patch(
            "gweatherrouting.qt.routingcontroller.calculate_route",
            side_effect=calculate,
        ):
            panel.calculate.click()
            self.wait_for_routing_stage(started.is_set)
            panel.cancel_button.click()
            release.set()
            self.wait_for_route()
        self.assertEqual(len(self.controller.core.routingManager), 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("1/3 scenarios saved", panel.status.text())
        self.assertFalse(self.window.map.route_preview)

    def test_routing_advanced_options_restore_without_calculations_or_downloads(self):
        panel = self.window.routing_panel
        panel.parameter_widgets["subdiv"].setValue(3)
        panel.parameter_widgets["min_increase"].setValue(4.5)
        enabled, values = panel.comparison_axes["polar_efficiency_pct"]
        enabled.setChecked(True)
        values.setText("90, 100")
        panel.use_currents.setChecked(True)
        panel.auto_currents.setChecked(False)
        self.window.workspace.save()
        payload = self.window.workspace.store.load()["routing"]["options"]
        self.assertTrue(payload["use_currents"])
        self.assertFalse(payload["auto_currents"])
        self.restart_workspace()
        panel = self.window.routing_panel
        self.assertTrue(panel.use_currents.isChecked())
        self.assertFalse(panel.auto_currents.isChecked())
        self.assertFalse(self.controller.routing.busy)
        self.assertFalse(self.controller.core.routingManager.elements)
        self.assertEqual(
            panel.comparison_axes["polar_efficiency_pct"][1].text(), "90, 100"
        )
        self.assertTrue(panel.comparison_axes["polar_efficiency_pct"][0].isChecked())
        panel.use_currents.setChecked(False)
        self.assertEqual(panel.parameter_widgets["subdiv"].value(), 3)
        self.assertEqual(panel.parameter_widgets["min_increase"].value(), 4.5)

    def test_frontend_does_not_import_gtk(self):
        self.assertNotIn("gi.repository.Gtk", sys.modules)
        self.assertFalse(
            any(name.startswith("gweatherrouting.gtk") for name in sys.modules)
        )
        self.assertFalse(hasattr(self.window, "save_button"))
        self.assertFalse(hasattr(self.window, "clear_button"))
        self.assertTrue(self.window.grab().width())

    def test_settings_uses_native_preferences_menu_without_header_duplicate(self):
        from PySide6.QtGui import QAction, QKeySequence

        action = self.window.settings_action
        self.assertEqual(action.menuRole(), QAction.MenuRole.PreferencesRole)
        self.assertEqual(
            action.shortcut(), QKeySequence(QKeySequence.StandardKey.Preferences)
        )
        self.assertFalse(
            any(a.text() == "Settings" for a in self.window.workspace_toolbar.actions())
        )
        self.assertIsNone(self.window.settings_dialog)
        action.trigger()
        self.assertTrue(self.window.settings_dialog.isVisible())
        self.assertIs(self.window.settings_dialog.controller, self.controller)

    def test_map_data_settings_missing_selection_and_no_automatic_download(self):
        self.assertIsNone(self.window.settings_dialog)
        self.window.show_settings()
        page = self.window.settings_dialog.map_data
        self.window.settings_dialog.tabs.setCurrentWidget(page)
        self.assertTrue(page.downloads["coastlines"].isEnabled())
        self.assertTrue(page.downloads["seamarks"].isEnabled())
        self.assertFalse(page.status.isVisible())
        self.assertFalse(self.controller.map_data.task.busy)
        with patch.object(self.controller.map_data, "start") as start:
            page.downloads["coastlines"].click()
            start.assert_called_once_with(("coastlines",))
            start.reset_mock()
            page.downloads["seamarks"].click()
            start.assert_called_once_with(("seamarks",))
        self.window.settings_dialog.hide()
        self.controller.offline = False
        self.controller.load_charts = True
        self.window._offer_map_data()
        self.controller.offline = True
        self.assertTrue(self.window.settings_dialog.isVisible())
        self.assertIs(self.window.settings_dialog.tabs.currentWidget(), page)

    def test_map_data_real_coastline_install_reloads_on_ui_thread_without_duplicates(
        self,
    ):
        from test.test_mapdata import coastline_archive, Response
        from gweatherrouting.rendering.chartlayer import ChartLayer
        from gweatherrouting.common import resource_path

        body = coastline_archive(self.path)
        root = self.path / "base-map"
        root.mkdir()
        (root / "gshhs").mkdir()
        (root / "gshhs" / "partial").write_text("interrupted old installation")
        setup = self.controller.map_data
        setup.directory = root
        self.window.show_settings()
        page = self.window.settings_dialog.map_data
        other = ChartLayer("user-chart", "raster", self.controller.settings)
        other.enabled = False
        with patch("gweatherrouting.rendering.chartcollection.DATA_DIR", str(root)):
            self.controller.charts.load_base_chart()
            self.controller.charts.charts.append(other)
            fallback = resource_path("gweatherrouting", "data/countries.geojson")
            self.assertTrue(
                any(c.path == fallback for c in self.controller.charts.charts)
            )
            callback_threads = []
            real = self.controller.charts.reload_base_chart

            def reload():
                callback_threads.append(get_ident())
                real()

            with (
                patch(
                    "gweatherrouting.core.mapdata.requests.get",
                    return_value=Response(body),
                ),
                patch.object(
                    self.controller.charts, "reload_base_chart", side_effect=reload
                ),
            ):
                self.assertTrue(setup.start(("coastlines",)))
                self.wait_for_routing_stage(lambda: not setup.task.busy)
            self.assertIn("installed and ready", page.status.text())
            self.assertEqual(callback_threads, [get_ident()])
            self.assertFalse(setup.missing()["coastlines"])
            self.assertTrue(setup.missing()["seamarks"])
            self.assertFalse(page.downloads["coastlines"].isEnabled())
            self.assertIn("Installed", page.downloads["coastlines"].text())
            self.assertFalse((root / "gshhs" / "partial").exists())
            self.assertIsNotNone(self.controller.charts.gshhsLayer.lpvFile)
            self.assertIn(
                self.controller.charts.gshhsLayer,
                self.controller.charts.get_line_point_validity_providers(),
            )
            self.assertFalse(
                any(c.path == fallback for c in self.controller.charts.charts)
            )
            self.assertIn(other, self.controller.charts.charts)
            self.assertFalse(other.enabled)
            count = len(self.controller.charts.charts)
            self.controller.charts.reload_base_chart()
            self.assertEqual(len(self.controller.charts.charts), count)
            self.assertFalse(setup.start(("coastlines",)))
            self.assertFalse(setup.task.busy)
            self.assertEqual(
                self.window.map.marine._renderer._land_mask.directory,
                root / "gshhs/GSHHS_shp",
            )
            coast = self.controller.charts.gshhsLayer
            coast.enabled = False
            body = (
                b'<osm version="0.6"><node id="1" lat="0" lon="0">'
                b'<tag k="seamark:type" v="buoy_lateral"/></node></osm>'
            )
            with patch(
                "gweatherrouting.core.mapdata.requests.get", return_value=Response(body)
            ):
                self.assertTrue(setup.start(("seamarks",)))
                self.wait_for_routing_stage(lambda: not setup.task.busy)
            self.assertFalse(any(setup.missing().values()))
            self.assertIs(self.controller.charts.gshhsLayer, coast)
            self.assertFalse(coast.enabled)
            self.assertIsNotNone(self.controller.charts.osmLayer)
            self.assertTrue(all(not b.isEnabled() for b in page.downloads.values()))
            self.window.settings_dialog.hide()
            self.controller.offline = False
            self.controller.load_charts = True
            self.window._offer_map_data()
            self.controller.offline = True
            self.assertFalse(self.window.settings_dialog.isVisible())

    def test_map_data_cancel_and_close_reject_late_chart_attachment(self):
        from gweatherrouting.core.mapdata import MapDownloadCancelledError

        setup = self.controller.map_data
        started, release = Event(), Event()
        self.addCleanup(release.set)

        def install(kinds, directory, cancel, report):
            started.set()
            release.wait(5)
            return (), MapDownloadCancelledError("Download cancelled")

        with (
            patch(
                "gweatherrouting.qt.mapdatasetup.install_map_data", side_effect=install
            ),
            patch.object(self.controller.charts, "reload_base_chart") as reload,
        ):
            self.assertTrue(setup.start(("coastlines",)))
            self.wait_for_routing_stage(started.is_set)
            self.assertFalse(setup.start(("seamarks",)))
            setup.cancel()
            self.assertTrue(setup.cancel_event.is_set())
            release.set()
            self.wait_for_routing_stage(lambda: not setup.task.busy)
            self.assertEqual(setup.message, "Download cancelled")
            reload.assert_not_called()
            started.clear()
            release.clear()
            self.assertTrue(setup.start(("coastlines",)))
            self.wait_for_routing_stage(started.is_set)
            worker = setup.task.worker_thread
            self.window.close()
            release.set()
            worker.join(2)
            self.drain()
            self.assertFalse(worker.is_alive())
            self.assertTrue(setup.task.closed)
            self.assertTrue(setup.cancel_event.is_set())
            reload.assert_not_called()

    def test_map_data_serializes_with_routing_and_library_operations(self):
        self.plan_test_waypoints()
        setup = self.controller.map_data
        self.controller.routing.busy = True
        self.assertFalse(setup.start(("coastlines",)))
        self.assertIn("Wait", setup.message)
        self.controller.routing.busy = False
        setup.task.busy = True
        self.addCleanup(setattr, setup.task, "busy", False)
        self.assertFalse(self.controller.data.run("scan", "test", lambda p: None))
        self.assertTrue(self.controller.polars.blocked)
        self.assertFalse(
            self.controller.routing.start(self.window.routing_panel._request()[0])
        )
        self.assertIn("Wait", self.controller.routing.error)

    def test_map_data_partial_completion_is_reloaded_after_later_error(self):
        setup = self.controller.map_data
        with (
            patch(
                "gweatherrouting.qt.mapdatasetup.install_map_data",
                return_value=(("coastlines",), ValueError("Sea marks unavailable")),
            ),
            patch.object(self.controller.charts, "reload_base_chart") as reload,
        ):
            self.assertTrue(setup.start(("coastlines", "seamarks")))
            self.wait_for_routing_stage(lambda: not setup.task.busy)
        reload.assert_called_once()
        self.assertIn("Sea marks unavailable", setup.message)
        self.assertIn("completed datasets are available", setup.message)

    def test_rebuilt_cached_layers_are_drawn_before_returning(self):
        from PySide6.QtGui import QColor, QImage, QPainter

        canvas = self.window.map
        for factor in (1, 2):
            with self.subTest(dpr=factor):
                size = (canvas.width(), canvas.height())
                image = QImage(
                    size[0] * factor,
                    size[1] * factor,
                    QImage.Format.Format_ARGB32_Premultiplied,
                )
                image.setDevicePixelRatio(factor)
                image.fill(QColor("#39954c"))
                target = QImage(image.size(), image.format())
                target.setDevicePixelRatio(factor)
                target.fill(QColor("black"))
                painter = QPainter(target)
                try:
                    cache = canvas._draw_reusable_layer(
                        painter, None, "new", lambda: image
                    )
                finally:
                    painter.end()
                self.assertIs(cache[4], image)
                self.assertEqual(
                    target.pixelColor(100 * factor, 100 * factor), QColor("#39954c")
                )

    def test_map_presents_rebuild_frames_with_direct_overlays_at_two_pixel_scales(self):
        from contextlib import ExitStack
        from PySide6.QtGui import QColor, QImage, QPainter

        canvas = self.window.map
        for factor in (1, 2):
            for trigger in ("cold", "data", "pan", "zoom"):
                with self.subTest(dpr=factor, trigger=trigger), ExitStack() as stack:
                    stack.enter_context(
                        patch.object(canvas, "get_scale_factor", return_value=factor)
                    )
                    stack.enter_context(
                        patch.object(canvas, "devicePixelRatioF", return_value=factor)
                    )
                    epoch = [0]
                    stack.enter_context(
                        patch.object(
                            canvas, "_base_key", side_effect=lambda: ("base", epoch[0])
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            canvas, "_nav_key", side_effect=lambda: ("nav", epoch[0])
                        )
                    )

                    def layer(transparent=False):
                        image = QImage(
                            canvas.width() * factor,
                            canvas.height() * factor,
                            QImage.Format.Format_ARGB32_Premultiplied,
                        )
                        image.setDevicePixelRatio(factor)
                        image.fill(
                            Qt.GlobalColor.transparent
                            if transparent
                            else QColor("#39954c")
                        )
                        if transparent:
                            painter = QPainter(image)
                            painter.fillRect(
                                canvas.width() // 2,
                                canvas.height() // 2,
                                30,
                                30,
                                QColor("#d43342"),
                            )
                            painter.end()
                        return image

                    base = stack.enter_context(
                        patch.object(canvas, "_base_image", side_effect=layer)
                    )
                    nav = stack.enter_context(
                        patch.object(
                            canvas, "_navigation_image", side_effect=lambda: layer(True)
                        )
                    )
                    for method in (
                        "_paint_route_overlay",
                        "_paint_track_overlay",
                        "_paint_dashboard",
                    ):
                        stack.enter_context(patch.object(canvas, method))
                    stack.enter_context(
                        patch.object(
                            canvas,
                            "_paint_caption",
                            side_effect=lambda p: p.fillRect(
                                12, 12, 20, 20, QColor("white")
                            ),
                        )
                    )
                    canvas._base_cache = canvas._nav_cache = None
                    canvas._zoom_animating = False
                    if trigger != "cold":
                        canvas._base_cache = (
                            ("base", 0),
                            canvas._center_lat,
                            canvas._center_lon,
                            canvas._zoom,
                            layer(),
                        )
                        canvas._nav_cache = (
                            ("nav", 0),
                            canvas._center_lat,
                            canvas._center_lon,
                            canvas._zoom,
                            layer(True),
                        )
                    if trigger == "data":
                        epoch[0] = 1
                    elif trigger == "pan":
                        canvas._center_lon += 10
                    elif trigger == "zoom":
                        canvas._zoom += 1
                    output = QImage(
                        canvas.width() * factor,
                        canvas.height() * factor,
                        QImage.Format.Format_ARGB32_Premultiplied,
                    )
                    output.setDevicePixelRatio(factor)
                    output.fill(QColor("black"))
                    painter = QPainter(output)
                    try:
                        canvas.render(painter, QPointF(0, 0).toPoint())
                    finally:
                        painter.end()
                    # A direct marker survives the original bug while both cached layers vanish.
                    self.assertEqual(
                        output.pixelColor(20 * factor, 20 * factor), QColor("white")
                    )
                    self.assertEqual(
                        output.pixelColor(100 * factor, 100 * factor), QColor("#39954c")
                    )
                    self.assertEqual(
                        output.pixelColor(
                            (canvas.width() // 2 + 10) * factor,
                            (canvas.height() // 2 + 10) * factor,
                        ),
                        QColor("#d43342"),
                    )
                    self.assertEqual(base.call_count, 1)
                    self.assertEqual(nav.call_count, 1)

    def test_padded_scene_pixels_cover_pan_and_zoom_and_rebuild_before_edges(self):
        from PySide6.QtGui import QColor, QImage, QPainter

        canvas = self.window.map
        canvas.resize(640, 480)
        width, height = canvas.width(), canvas.height()
        for factor in (1, 2):
            with (
                self.subTest(dpr=factor),
                patch.object(canvas, "get_scale_factor", return_value=factor),
                patch.object(canvas, "devicePixelRatioF", return_value=factor),
            ):
                canvas._zoom_animating = False
                landmark = canvas.convert_screen_to_geographic(width + 40, height / 2)
                calls = []

                def charts(view, cr):
                    calls.append(
                        (view.get_allocated_width(), view.get_allocated_height())
                    )
                    cr.set_source_rgb(0.2, 0.6, 0.3)
                    cr.paint()
                    x, y = view.convert_geographic_to_screen(landmark)
                    cr.set_source_rgb(0.8, 0.1, 0.6)
                    cr.rectangle(x - 5, y - 5, 10, 10)
                    cr.fill()

                def paint(cache):
                    output = QImage(
                        width * factor,
                        height * factor,
                        QImage.Format.Format_ARGB32_Premultiplied,
                    )
                    output.setDevicePixelRatio(factor)
                    output.fill(QColor("black"))
                    painter = QPainter(output)
                    try:
                        cache = canvas._draw_reusable_layer(
                            painter, cache, "scene", canvas._base_image
                        )
                    finally:
                        painter.end()
                    for x, y in (
                        (1, 1),
                        (width - 2, 1),
                        (1, height - 2),
                        (width - 2, height - 2),
                    ):
                        self.assertEqual(
                            output.pixelColor(x * factor, y * factor),
                            QColor(51, 153, 76),
                        )
                    self.assertEqual(canvas.get_allocated_width(), width)
                    return cache, output

                with (
                    patch.object(canvas.charts, "do_draw", side_effect=charts),
                    patch.object(canvas.wind, "draw"),
                    patch.object(canvas.marine, "do_draw"),
                ):
                    cache, _ = paint(None)
                    self.assertGreater(calls[0][0], width)
                    canvas._pan_pixels(-70, 0)
                    moved, output = paint(cache)
                    self.assertIs(moved, cache)
                    self.assertEqual(len(calls), 1)
                    x, y = canvas.convert_geographic_to_screen(landmark)
                    self.assertEqual(
                        output.pixelColor(round(x * factor), round(y * factor)),
                        QColor(204, 25, 153),
                    )
                    # New movement beyond the safe margin rebuilds in this paint.
                    canvas._pan_pixels(-150, 0)
                    cache, _ = paint(cache)
                    self.assertEqual(len(calls), 2)
                    anchor = canvas.convert_screen_to_geographic(0, height)
                    canvas._zoom_anchor = (*anchor.get_degrees(), 0, height)
                    canvas._zoom_animating = True
                    canvas._zoom -= 0.25
                    canvas._recenter_on_anchor()
                    moved, _ = paint(cache)
                    self.assertIs(moved, cache)
                    self.assertEqual(len(calls), 2)
                    # A large zoom-out exhausts the padding; never stretch an
                    # undersized image and expose empty chart edges.
                    canvas._zoom -= 2
                    canvas._recenter_on_anchor()
                    cache, _ = paint(cache)
                    self.assertEqual(len(calls), 3)
                    self.assertTrue(canvas._zoom_animating)
                canvas._finish_zoom_silent()

    def test_padded_map_paint_cannot_escape_into_ancestor_surface(self):
        from PySide6.QtGui import QColor, QImage, QPainter

        canvas = self.window.map
        for factor in (1, 2):
            with self.subTest(dpr=factor):
                output = QImage(
                    (canvas.width() + 400) * factor,
                    (canvas.height() + 400) * factor,
                    QImage.Format.Format_ARGB32_Premultiplied,
                )
                output.setDevicePixelRatio(factor)
                output.fill(QColor("#151515"))
                painter = QPainter(output)
                painter.translate(200, 200)

                def layer():
                    image = QImage(
                        (canvas.width() + 384) * factor,
                        (canvas.height() + 384) * factor,
                        QImage.Format.Format_ARGB32_Premultiplied,
                    )
                    image.setDevicePixelRatio(factor)
                    image.fill(QColor("#39954c"))
                    return image

                canvas._base_cache = canvas._nav_cache = None
                with (
                    patch(
                        "gweatherrouting.qt.mapcanvas.QPainter",
                        return_value=painter,
                        RenderHint=QPainter.RenderHint,
                    ),
                    patch.object(canvas, "_base_image", side_effect=layer),
                    patch.object(canvas, "_navigation_image", side_effect=layer),
                ):
                    canvas.paintEvent(None)
                self.assertEqual(
                    output.pixelColor(100 * factor, 100 * factor), QColor("#151515")
                )
                self.assertEqual(
                    output.pixelColor(300 * factor, 300 * factor), QColor("#39954c")
                )
                self.assertEqual(
                    output.pixelColor((canvas.width() + 300) * factor, 300 * factor),
                    QColor("#151515"),
                )

    def test_padding_budget_and_failed_bake_restore_interactive_projection(self):
        canvas = self.window.map
        for factor in (1, 2, 4):
            with (
                self.subTest(dpr=factor),
                patch.object(canvas, "get_scale_factor", return_value=factor),
            ):
                padding = canvas._layer_padding()
                extra = (
                    (
                        (canvas.width() + 2 * padding) * (canvas.height() + 2 * padding)
                        - canvas.width() * canvas.height()
                    )
                    * factor**2
                    * 4
                )
                self.assertLessEqual(extra, canvas._PADDING_BYTES)
                self.assertLessEqual(padding, 192)
        canvas._zoom_animating = True
        with self.assertRaisesRegex(ValueError, "reader failed"):
            canvas._build_padded_layer(
                lambda: (_ for _ in ()).throw(ValueError("reader failed"))
            )
        self.assertEqual(canvas._render_padding, 0)
        self.assertTrue(canvas._zoom_animating)
        canvas._finish_zoom_silent()

    @staticmethod
    def _native_pinch(kind, value=0.0, pointer=QPointF(83, 71)):
        from PySide6.QtGui import QNativeGestureEvent, QPointingDevice

        return QNativeGestureEvent(
            kind,
            QPointingDevice.primaryPointingDevice(),
            2,
            pointer,
            pointer,
            pointer,
            value,
            QPointF(),
        )

    @staticmethod
    def _precise_scroll(
        dx=0,
        dy=0,
        phase=Qt.ScrollPhase.ScrollUpdate,
        modifiers=Qt.KeyboardModifier.NoModifier,
        inverted=False,
    ):
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QWheelEvent

        return QWheelEvent(
            QPointF(83, 71),
            QPointF(83, 71),
            QPoint(dx, dy),
            QPoint(0, dy * 8),
            Qt.MouseButton.NoButton,
            modifiers,
            phase,
            inverted,
        )

    def test_native_pinch_is_direct_anchored_and_independent_of_wheel_smoothing(self):
        for view, canvas in (
            (self.window.map, self.window.map),
            (self.window.logs_view, self.window.log_map),
        ):
            self.window.views.setCurrentWidget(view)
            self.drain()
            for smooth in (False, True):
                with self.subTest(view=canvas is self.window.map, smooth=smooth):
                    canvas._smooth_zoom = smooth
                    before = canvas._zoom
                    anchor = canvas.convert_screen_to_geographic(83, 71)
                    self.app.sendEvent(
                        canvas,
                        self._native_pinch(Qt.NativeGestureType.BeginNativeGesture),
                    )
                    self.assertTrue(canvas.is_moving())
                    for value in (0.25, -0.1):
                        self.app.sendEvent(
                            canvas,
                            self._native_pinch(
                                Qt.NativeGestureType.ZoomNativeGesture, value
                            ),
                        )
                    self.assertAlmostEqual(canvas._zoom, before + math.log2(1.25 * 0.9))
                    self.assertIsNone(canvas._zoom_tick_token)
                    x, y = canvas.convert_geographic_to_screen(anchor)
                    self.assertAlmostEqual(x, 83, places=7)
                    self.assertAlmostEqual(y, 71, places=7)
                    # Compatibility scroll/rotation must not apply a second zoom.
                    zoom = canvas._zoom
                    canvas.wheelEvent(self._motion_wheel())
                    self.app.sendEvent(
                        canvas,
                        self._native_pinch(
                            Qt.NativeGestureType.RotateNativeGesture, 12
                        ),
                    )
                    self.assertEqual(canvas._zoom, zoom)
                    self.app.sendEvent(
                        canvas,
                        self._native_pinch(Qt.NativeGestureType.EndNativeGesture),
                    )
                    self.assertFalse(canvas.is_moving())
                    self.assertIsNone(canvas._zoom_anchor)

    def test_native_pinch_rejects_invalid_scales_and_clamps_zoom(self):
        canvas = self.window.map
        for value in (-1.0, -2.0, float("nan"), float("inf")):
            before = canvas._zoom
            self.app.sendEvent(
                canvas,
                self._native_pinch(Qt.NativeGestureType.ZoomNativeGesture, value),
            )
            self.assertEqual(canvas._zoom, before)
            self.assertFalse(canvas.is_moving())
        for start, value, expected in ((19.9, 4, 20), (1.1, -0.9, 1)):
            canvas._zoom = start
            self.app.sendEvent(
                canvas,
                self._native_pinch(Qt.NativeGestureType.ZoomNativeGesture, value),
            )
            self.assertEqual(canvas._zoom, expected)
            self.app.sendEvent(
                canvas, self._native_pinch(Qt.NativeGestureType.EndNativeGesture)
            )

    def test_trackpad_pan_uses_both_pixel_axes_and_native_momentum_once(self):
        canvas = self.window.map
        canvas._trackpad_scroll_pans = True
        before = canvas.convert_geographic_to_screen(MapPoint(48.4, -123.4))
        zoom = canvas._zoom
        for event in (
            self._precise_scroll(phase=Qt.ScrollPhase.ScrollBegin),
            self._precise_scroll(23, -17, inverted=True),
            self._precise_scroll(11, 7, phase=Qt.ScrollPhase.ScrollMomentum),
        ):
            self.app.sendEvent(canvas, event)
            self.assertTrue(event.isAccepted())
        after = canvas.convert_geographic_to_screen(MapPoint(48.4, -123.4))
        self.assertAlmostEqual(after[0] - before[0], 34, places=7)
        self.assertAlmostEqual(after[1] - before[1], -10, places=7)
        self.assertEqual(canvas._zoom, zoom)
        self.assertTrue(canvas._trackpad_panning)
        self.assertIsNone(canvas._inertia)
        self.assertIsNone(canvas._zoom_tick_token)
        self.app.sendEvent(canvas, self._precise_scroll(phase=Qt.ScrollPhase.ScrollEnd))
        self.assertFalse(canvas.is_moving())
        self.assertFalse(canvas._scroll_settle.isActive())

    def test_trackpad_modifiers_mouse_wheel_and_non_macos_keep_zoom(self):
        canvas = self.window.map
        canvas._smooth_zoom = False
        canvas._trackpad_scroll_pans = True
        for modifier in (
            Qt.KeyboardModifier.ControlModifier,
            Qt.KeyboardModifier.MetaModifier,
        ):
            before = canvas._zoom
            anchor = canvas.convert_screen_to_geographic(83, 71)
            self.app.sendEvent(canvas, self._precise_scroll(0, 15, modifiers=modifier))
            self.assertGreater(canvas._zoom, before)
            self.assertFalse(canvas._trackpad_panning)
            self.assertAlmostEqual(
                canvas.convert_geographic_to_screen(anchor)[0], 83, places=7
            )
        before = canvas._zoom
        canvas.wheelEvent(self._motion_wheel())
        self.assertAlmostEqual(canvas._zoom, before + 0.25)
        canvas._trackpad_scroll_pans = False
        before = canvas._zoom
        self.app.sendEvent(canvas, self._precise_scroll(0, 15))
        self.assertGreater(canvas._zoom, before)
        self.assertFalse(canvas._trackpad_panning)

    def test_trackpad_motion_defers_overlays_and_workspace_then_settles(self):
        canvas = self.window.map
        canvas._trackpad_scroll_pans = True
        for start, finish in (
            (
                lambda: self.app.sendEvent(
                    canvas, self._native_pinch(Qt.NativeGestureType.BeginNativeGesture)
                ),
                lambda: self.app.sendEvent(
                    canvas, self._native_pinch(Qt.NativeGestureType.EndNativeGesture)
                ),
            ),
            (
                lambda: self.app.sendEvent(canvas, self._precise_scroll(20, 10)),
                lambda: self.app.sendEvent(
                    canvas, self._precise_scroll(phase=Qt.ScrollPhase.ScrollEnd)
                ),
            ),
        ):
            start()
            canvas._overlay_pending = False
            canvas.queue_draw()
            self.assertTrue(canvas._overlay_pending)
            with patch.object(self.window.workspace, "save") as save:
                self.window.workspace._autosave()
                save.assert_not_called()
                finish()
                self.assertFalse(canvas._overlay_pending)
                self.window.workspace._autosave()
                save.assert_called_once()
        self.app.sendEvent(
            canvas, self._precise_scroll(10, 0, phase=Qt.ScrollPhase.NoScrollPhase)
        )
        self.assertTrue(canvas._scroll_settle.isActive())
        canvas._scroll_settle.timeout.emit()
        self.assertFalse(canvas.is_moving())

    def test_native_and_scroll_motion_cancel_on_fit_placement_hide_resize_and_close(
        self,
    ):
        from PySide6.QtGui import QResizeEvent
        from PySide6.QtCore import QEvent

        canvas = self.window.map
        canvas._trackpad_scroll_pans = True
        for native in (False, True):
            for action in (
                lambda: canvas.fit_coordinates(((48.4, -123.4), (48.5, -123.5))),
                lambda: canvas.begin_move(lambda point: None),
                canvas.hide,
                lambda: self.app.sendEvent(
                    canvas, QEvent(QEvent.Type.WindowDeactivate)
                ),
                lambda: canvas.resizeEvent(QResizeEvent(canvas.size(), canvas.size())),
                lambda: canvas.mousePressEvent(
                    SimpleNamespace(
                        button=lambda: Qt.MouseButton.LeftButton,
                        position=lambda: QPointF(100, 100),
                    )
                ),
            ):
                if native:
                    self.app.sendEvent(
                        canvas,
                        self._native_pinch(Qt.NativeGestureType.BeginNativeGesture),
                    )
                    self.app.sendEvent(
                        canvas,
                        self._native_pinch(Qt.NativeGestureType.ZoomNativeGesture, 0.1),
                    )
                else:
                    self.app.sendEvent(
                        canvas,
                        self._precise_scroll(10, 0, phase=Qt.ScrollPhase.NoScrollPhase),
                    )
                action()
                self.assertFalse(canvas._native_gesture)
                self.assertFalse(canvas._trackpad_panning)
                self.assertFalse(canvas._scroll_settle.isActive())
                self.assertFalse(canvas._zoom_animating)
                canvas.drag = None
                canvas.cancel_move()
                canvas.show()
        self.app.sendEvent(
            canvas, self._precise_scroll(10, 0, phase=Qt.ScrollPhase.NoScrollPhase)
        )
        canvas.dispose()
        self.assertFalse(canvas._scroll_settle.isActive())
        self.assertFalse(canvas.is_moving())

    def _enable_motion_settings(self):
        # The common fixture skips disk loading; initialize notifications using
        # a private settings path before exercising real preference callbacks.
        with patch("gweatherrouting.core.storage.DATA_DIR", str(self.path)):
            ORIGINAL_STORAGE_LOAD(self.controller.settings)

    @staticmethod
    def _motion_wheel(pointer=QPointF(83, 71)):
        from PySide6.QtCore import QPoint

        return SimpleNamespace(
            position=lambda: pointer,
            angleDelta=lambda: QPoint(0, 120),
            pixelDelta=lambda: QPoint(0, 0),
            accept=lambda: None,
        )

    @staticmethod
    def _fling_canvas(canvas, clock):
        def event(x, y):
            return SimpleNamespace(
                button=lambda: Qt.MouseButton.LeftButton, position=lambda: QPointF(x, y)
            )

        canvas.mousePressEvent(event(100, 100))
        clock[0] += 0.04
        canvas.mouseMoveEvent(event(125, 110))
        clock[0] += 0.02
        canvas.mouseMoveEvent(event(145, 120))
        clock[0] += 0.005
        canvas.mouseReleaseEvent(event(145, 120))

    def test_motion_smooth_zoom_off_is_immediate_and_cursor_anchored_in_both_views(
        self,
    ):
        self._enable_motion_settings()
        self.controller.settings.smoothZoom = False
        for view, canvas in (
            (self.window.map, self.window.map),
            (self.window.logs_view, self.window.log_map),
        ):
            self.window.views.setCurrentWidget(view)
            self.drain()
            before = canvas._zoom
            anchor = canvas.convert_screen_to_geographic(83, 71)
            canvas.wheelEvent(self._motion_wheel())
            self.assertAlmostEqual(canvas._zoom, before + 0.25)
            self.assertIsNone(canvas._zoom_target)
            self.assertIsNone(canvas._zoom_tick_token)
            self.assertFalse(canvas._zoom_animating)
            self.assertFalse(canvas._zoom_settle.isActive())
            x, y = canvas.convert_geographic_to_screen(anchor)
            self.assertAlmostEqual(x, 83, places=7)
            self.assertAlmostEqual(y, 71, places=7)

    def test_motion_disabling_smooth_zoom_finishes_current_target_without_drift(self):
        self._enable_motion_settings()
        canvas = self.window.map
        self.controller.settings.smoothZoom = True
        clock = [100.0]
        with patch(
            "gweatherrouting.qt.mapcanvas.time.monotonic", side_effect=lambda: clock[0]
        ):
            before = canvas._zoom
            anchor = canvas.convert_screen_to_geographic(83, 71)
            canvas.wheelEvent(self._motion_wheel())
            token = canvas._zoom_tick_token
            self.assertIsNotNone(token)
            self.assertEqual(canvas._zoom, before)
            clock[0] += 0.016
            canvas._ticks[token].timeout.emit()
            self.assertGreater(canvas._zoom, before)
            self.assertLess(canvas._zoom, before + 0.25)
            self.controller.settings.smoothZoom = False
            self.assertAlmostEqual(canvas._zoom, before + 0.25)
            self.assertIsNone(canvas._zoom_tick_token)
            self.assertNotIn(token, canvas._ticks)
            self.assertFalse(canvas._zoom_animating)
            x, y = canvas.convert_geographic_to_screen(anchor)
            self.assertAlmostEqual(x, 83, places=7)
            self.assertAlmostEqual(y, 71, places=7)

    def test_motion_drag_inertia_follows_release_and_live_setting_stops_it(self):
        self._enable_motion_settings()
        canvas = self.window.map
        clock = [100.0]
        with (
            patch(
                "gweatherrouting.qt.mapcanvas.time.monotonic",
                side_effect=lambda: clock[0],
            ),
            patch.object(canvas, "_hover"),
        ):
            self.controller.settings.panInertia = False
            self._fling_canvas(canvas, clock)
            self.assertIsNone(canvas._inertia)
            self.assertIsNone(canvas._inertia_tick_token)
            self.controller.settings.panInertia = True
            self._fling_canvas(canvas, clock)
            motion = canvas._inertia
            token = canvas._inertia_tick_token
            self.assertIsNotNone(motion)
            point = canvas.convert_screen_to_geographic(100, 100)
            velocity = math.hypot(motion.vx, motion.vy)
            clock[0] += 0.02
            canvas._ticks[token].timeout.emit()
            x, y = canvas.convert_geographic_to_screen(point)
            self.assertGreater(x, 100)
            self.assertGreater(y, 100)
            self.assertLess(math.hypot(motion.vx, motion.vy), velocity)
            canvas.queue_draw()
            self.assertTrue(canvas._overlay_pending)
            with patch.object(self.window.workspace, "save") as save:
                self.window.workspace._autosave()
                save.assert_not_called()
                self.controller.settings.panInertia = False
                self.window.workspace._autosave()
                save.assert_called_once()
            self.assertFalse(canvas._overlay_pending)
            self.assertIsNone(canvas._inertia)
            self.assertIsNone(canvas._inertia_tick_token)
            self.assertNotIn(token, canvas._ticks)
            before = canvas._center_lat, canvas._center_lon
            clock[0] += 1
            self.assertFalse(canvas._inertia_tick(canvas, None))
            self.assertEqual((canvas._center_lat, canvas._center_lon), before)

    def test_motion_pause_before_release_suppresses_glide_and_ticks_finish(self):
        self._enable_motion_settings()
        canvas = self.window.map
        clock = [100.0]
        self.controller.settings.panInertia = True
        with (
            patch(
                "gweatherrouting.qt.mapcanvas.time.monotonic",
                side_effect=lambda: clock[0],
            ),
            patch.object(canvas, "_hover"),
        ):
            canvas.mousePressEvent(
                SimpleNamespace(
                    button=lambda: Qt.MouseButton.LeftButton,
                    position=lambda: QPointF(100, 100),
                )
            )
            clock[0] += 0.04
            canvas.mouseMoveEvent(SimpleNamespace(position=lambda: QPointF(145, 120)))
            clock[0] += 0.2
            canvas.mouseReleaseEvent(None)
            self.assertIsNone(canvas._inertia)
            self._fling_canvas(canvas, clock)
            token = canvas._inertia_tick_token
            for _ in range(180):
                if token not in canvas._ticks:
                    break
                clock[0] += 1 / 60
                canvas._ticks[token].timeout.emit()
            self.assertIsNone(canvas._inertia)
            self.assertIsNone(canvas._inertia_tick_token)
            self.assertNotIn(token, canvas._ticks)

    def test_motion_new_gestures_fit_move_hide_and_resize_cancel_inertia(self):
        self._enable_motion_settings()
        from PySide6.QtGui import QResizeEvent

        canvas = self.window.map
        clock = [100.0]
        self.controller.settings.panInertia = True
        with (
            patch(
                "gweatherrouting.qt.mapcanvas.time.monotonic",
                side_effect=lambda: clock[0],
            ),
            patch.object(canvas, "_hover"),
        ):
            actions = (
                lambda: canvas.mousePressEvent(
                    SimpleNamespace(
                        button=lambda: Qt.MouseButton.LeftButton,
                        position=lambda: QPointF(100, 100),
                    )
                ),
                lambda: canvas.wheelEvent(self._motion_wheel()),
                lambda: canvas.fit_coordinates(((48.4, -123.4), (48.5, -123.5))),
                lambda: canvas.begin_move(lambda point: None),
                canvas.hide,
                lambda: canvas.resizeEvent(QResizeEvent(canvas.size(), canvas.size())),
            )
            for action in actions:
                self._fling_canvas(canvas, clock)
                token = canvas._inertia_tick_token
                self.assertIsNotNone(token)
                action()
                self.assertIsNone(canvas._inertia)
                self.assertIsNone(canvas._inertia_tick_token)
                self.assertNotIn(token, canvas._ticks)
                canvas._finish_zoom_silent()
                canvas.cancel_move()
                canvas.drag = None
                canvas.show()

    def test_motion_display_preferences_persist_and_close_cancels_callbacks(self):
        from PySide6.QtWidgets import QCheckBox

        with (
            patch("gweatherrouting.core.storage.DATA_DIR", str(self.path)),
            patch.object(Storage, "save", ORIGINAL_STORAGE_SAVE),
        ):
            ORIGINAL_STORAGE_LOAD(self.controller.settings)
            self.window.show_settings()
            for checkbox in self.window.settings_dialog.findChildren(QCheckBox):
                if checkbox.text() in ("Smooth zoom", "Pan inertia"):
                    checkbox.setChecked(False)
            for canvas in (self.window.map, self.window.log_map):
                self.assertFalse(canvas._smooth_zoom)
                self.assertFalse(canvas._pan_inertia_enabled)
            with patch.object(Storage, "load_or_save_default", ORIGINAL_STORAGE_LOAD):
                self.restart_workspace()
            for canvas in (self.window.map, self.window.log_map):
                self.assertFalse(canvas._smooth_zoom)
                self.assertFalse(canvas._pan_inertia_enabled)
            self.controller.settings.panInertia = True
            canvas = self.window.map
            clock = [100.0]
            with (
                patch(
                    "gweatherrouting.qt.mapcanvas.time.monotonic",
                    side_effect=lambda: clock[0],
                ),
                patch.object(canvas, "_hover"),
            ):
                self._fling_canvas(canvas, clock)
                self.assertIsNotNone(canvas._inertia_tick_token)
            settings = self.controller.settings
            self.window.close()
            self.assertIsNone(canvas._inertia_tick_token)
            self.assertIsNone(canvas._inertia)
            self.assertFalse(canvas._ticks)
            settings.panInertia = False
            settings.smoothZoom = True
            self.assertFalse(canvas._smooth_zoom)


if __name__ == "__main__":
    unittest.main()
