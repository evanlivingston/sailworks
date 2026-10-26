"""Run Qt integration checks in a separate process."""

import json
import math
import os
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
from gweatherrouting.core.mapviewport import MapPoint, MapViewport, MercatorProjection
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
            "gweatherrouting.core.polarmanager.PolarManager.refresh_local_polars",
        ):
            mock = patch(target)
            mock.start()
            self.addCleanup(mock.stop)
        self.controller = SessionController(
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

    @unittest.skipUnless(
        os.environ.get("GWR_MARINE_QUICK_SNAPSHOT") == "1",
        "opt-in full-application native marine gesture readback",
    )
    def test_presenter_marine_arrows_render_during_pan_and_wheel(self):
        import ctypes
        from dataclasses import replace
        from datetime import timedelta
        from threading import Lock

        import numpy as np
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QMouseEvent, QWheelEvent
        from PySide6.QtTest import QTest

        from gweatherrouting.qt.marinequick import MarineCurrentItem
        from gweatherrouting.rendering.marinepresentation import chart_instant
        from gweatherrouting.rendering.marinesnapshot import (
            CurrentArrow,
            MarineSnapshot,
            current_triangles,
        )
        from test.test_marine import dataset

        canvas = self.window.map
        presenter = canvas._weather_presenter
        lock = Lock()
        publications, nodes, frames = [], [], []
        node_geometry = []
        cleared = []
        original_present = MarineCurrentItem.present
        original_node = MarineCurrentItem.updatePaintNode

        def present(item, snapshot, camera):
            original_present(item, snapshot, camera)
            with lock:
                publications.append((item, camera, item.vertices.tobytes()))

        def paint_node(item, old, data):
            node = original_node(item, old, data)
            with lock:
                if node is None:
                    cleared.append(item)
                if node is not None:
                    geometry = node.geometry()
                    node_geometry.append(
                        (
                            item,
                            ctypes.string_at(
                                int(geometry.vertexData()),
                                geometry.vertexCount() * geometry.sizeOfVertex(),
                            ),
                            node.childCount(),
                            node.parent().childCount() if node.parent() else 0,
                        )
                    )
                    records = [r for r in publications if r[0] is item]
                    if records:
                        nodes.append(records[-1])
            return node

        def wait_for(predicate, stage, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                self.app.processEvents()
                if predicate():
                    return
                time.sleep(0.002)
            self.fail(
                f"{stage}; presenter={presenter.reason!r}, "
                f"active={presenter.active}, widget={presenter.widget!r}; "
                f"publications/nodes/frames="
                f"{len(publications)}/{len(nodes)}/{len(frames)}"
            )

        self.assertNotIn(
            self.app.platformName(),
            ("offscreen", "minimal"),
            "Run this check with a native hardware Qt platform",
        )
        wait_for(
            lambda: presenter.widget is not None and presenter._rendered,
            "Application presenter did not create/render its visible Quick host",
        )
        widget = presenter.widget
        layer = widget._marine_layer
        self.assertTrue(widget.isVisible())
        self.assertTrue(presenter.foreground.isVisible())
        self.assertIs(
            self.app.widgetAt(widget.mapToGlobal(widget.rect().center())), widget
        )
        camera = widget._camera
        # Put deterministic geographic grid and station anchors in this actual
        # application's restored camera, independent of saved user coordinates.
        # Keep the station centered so pan followed by centered wheel zoom
        # leaves its ring inside the captured viewport throughout the gesture.
        anchors = [
            camera.view().convert_screen_to_geographic(x, y).get_degrees()
            for x, y in (
                (camera.width * 0.45, camera.height * 0.45),
                (camera.width * 0.5, camera.height * 0.5),
            )
        ]
        marine = canvas.marine
        marine.sources = replace(
            marine.sources,
            currents=replace(marine.sources.currents, datasets=(dataset(),)),
        )
        marine.visible = marine.currents_visible = True
        instant = chart_instant(marine.time_control.time)
        snapshot = MarineSnapshot(
            marine.sources,
            instant,
            (CurrentArrow(*anchors[0], 1, 0), CurrentArrow(*anchors[1], 0, 1, True)),
            "available",
        )
        replacements = {}
        sampling_release = Event()
        sampling_release.set()
        self.addCleanup(sampling_release.set)

        def sampled(sources, requested, *_):
            if requested in replacements:
                if not sampling_release.wait(5):
                    raise RuntimeError("Timed out waiting for loading-frame readback")
            return replace(
                replacements.get(requested, snapshot),
                sources=sources,
                instant=requested,
            )

        def rendered():
            # afterRendering may execute on the scene graph thread: read only
            # the locked observations, never QWidget/camera state here.
            with lock:
                records = [r for r in nodes if r[0] is layer.item]
                if records:
                    frames.append(records[-1])

        def has_frame(current):
            expected = layer.item.vertices.tobytes()
            with lock:
                return any(c == current and v == expected for _, c, v in frames)

        def assert_ownership():
            self.assertIs(
                presenter.widget, widget, "Presenter replaced the gesture host"
            )
            self.assertTrue(presenter.active, presenter.reason)
            self.assertTrue(widget.isVisible())
            self.assertFalse(layer.error, layer.state)
            self.assertEqual(layer._content, (marine.sources, instant, True))
            self.assertFalse(hasattr(marine, "do_draw"))
            self.assertEqual(
                (canvas._center_lat, canvas._center_lon, canvas._zoom),
                (
                    widget._camera.center_lat,
                    widget._camera.center_lon,
                    widget._camera.zoom,
                ),
                "Presenter did not mirror the gesture camera into the application",
            )

        def readback():
            image = widget.grabFramebuffer()
            self.assertFalse(image.isNull(), "Visible Quick host readback unavailable")
            # Verify actual station-ring pixels, not merely changing basemap pixels.
            x, y = widget._camera.view().convert_geographic_to_screen(
                MapPoint(*anchors[1])
            )
            # Cocoa's Quick framebuffer can contain Retina-sized pixels while
            # its QImage DPR metadata remains 1. Map logical widget coordinates
            # using the captured dimensions, independently on each axis.
            scale_x = image.width() / widget.width()
            scale_y = image.height() / widget.height()
            colors = [
                image.pixelColor(px, py)
                for px in range(
                    max(0, int((x - 10) * scale_x)),
                    min(image.width(), int((x + 10) * scale_x)),
                )
                for py in range(
                    max(0, int((y - 10) * scale_y)),
                    min(image.height(), int((y + 10) * scale_y)),
                )
            ]
            self.assertTrue(
                any(
                    c.red() > 245 and 165 < c.green() < 190 and c.blue() < 60
                    for c in colors
                ),
                "Station triangles reached a node but station ring is absent from native pixels; "
                f"anchor={(x, y)}, image={image.width()}x{image.height()}, "
                f"scale={(scale_x, scale_y)}, dpr={image.devicePixelRatio()}, "
                f"camera={widget._camera}, "
                f"vertex_bounds={np.min(layer.item.vertices['position'], axis=0)}.."
                f"{np.max(layer.item.vertices['position'], axis=0)}, "
                f"colors={sorted(set(c.name() for c in colors))[:30]}",
            )
            return image

        with (
            patch(
                "gweatherrouting.qt.marinequick.sample_currents", side_effect=sampled
            ),
            patch.object(MarineCurrentItem, "present", present),
            patch.object(MarineCurrentItem, "updatePaintNode", paint_node),
        ):
            # PySide binds scene-graph virtuals when the item is constructed.
            # Recreate through the production presenter with observers installed.
            presenter.suspend()
            presenter.request()
            wait_for(
                lambda: presenter.widget is not None and presenter._rendered,
                "Observed application Quick host did not render",
            )
            widget = presenter.widget
            layer = widget._marine_layer
            quick_window = widget.quickWindow()
            quick_window.afterRendering.connect(
                rendered, Qt.ConnectionType.DirectConnection
            )
            try:
                presenter.request()  # Exercise real presenter scheduling/ownership.
                wait_for(
                    lambda: layer.snapshot is not None and has_frame(widget._camera),
                    "Initial presenter marine frame missing",
                )
                assert_ownership()
                baseline = readback()
                initial = layer.item.vertices.tobytes()
                start = widget.rect().center()
                QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
                try:
                    for delta in (QPoint(24, 10), QPoint(48, 20)):
                        position = QPointF(start + delta)
                        event = QMouseEvent(
                            QMouseEvent.Type.MouseMove,
                            position,
                            QPointF(widget.mapToGlobal(position.toPoint())),
                            Qt.MouseButton.NoButton,
                            Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier,
                        )
                        self.app.sendEvent(widget, event)
                        self.assertTrue(event.isAccepted(), "Visible host rejected pan")
                        current = widget._camera
                        self.assertNotEqual(current, camera, "Pan never changed camera")
                        self.assertNotEqual(
                            layer.item.vertices.tobytes(),
                            initial,
                            "Pan camera moved but marine vertices froze",
                        )
                        wait_for(
                            lambda: has_frame(current),
                            "Pan vertices never reached a rendered frame while button held",
                        )
                        self.assertIsNotNone(
                            widget._drag, "Pan ended before marine rendered"
                        )
                        assert_ownership()
                        np.testing.assert_allclose(
                            layer.item.vertices["position"],
                            np.asarray(current_triangles(snapshot, current))[:, :2],
                            rtol=0,
                            atol=1e-4,
                        )
                        self.assertNotEqual(
                            readback(), baseline, "Pan native image froze"
                        )
                finally:
                    QTest.mouseRelease(
                        widget, Qt.MouseButton.LeftButton, pos=start + QPoint(48, 20)
                    )
                wait_for(
                    lambda: not widget._pan_inertia_tick.isActive(),
                    "Pan inertia never settled",
                )
                before = widget._camera
                initial = layer.item.vertices.tobytes()
                baseline = readback()
                position = QPointF(widget.rect().center())
                wheel = QWheelEvent(
                    position,
                    QPointF(widget.mapToGlobal(position.toPoint())),
                    QPoint(),
                    QPoint(0, 480),
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                    Qt.ScrollPhase.NoScrollPhase,
                    False,
                )
                self.app.sendEvent(widget, wheel)
                self.assertTrue(wheel.isAccepted(), "Visible host rejected wheel")
                target = widget._wheel_zoom_target
                self.assertIsNotNone(
                    target, "Presenter-hosted wheel animation never started"
                )

                def intermediate_frame():
                    with lock:
                        return any(
                            before.zoom < c.zoom < target and v != initial
                            for _, c, v in frames
                        )

                wait_for(
                    intermediate_frame,
                    "Wheel camera/vertices/node/frame pipeline stalled before gesture end",
                )
                self.assertTrue(
                    widget._wheel_zoom_tick.isActive(),
                    "Marine rendered only after wheel settled",
                )
                assert_ownership()
                self.assertNotEqual(
                    readback(), baseline, "Wheel native marine image froze"
                )
                self.assertTrue(
                    widget._wheel_zoom_tick.isActive(),
                    "Readback occurred only after wheel settled",
                )
                wait_for(
                    lambda: widget._wheel_zoom_target is None, "Wheel never settled"
                )

                # Reproduce time scrubbing interleaved with pans in the real
                # presenter, including a rendered empty/loading transition.
                # Unique saturated colors make *all* old layers identifiable
                # anywhere in the Quick framebuffer, not just at new anchors.
                palette = ((247, 13, 231), (19, 241, 37), (11, 229, 247), (239, 31, 67))
                base_time = marine.time_control.time
                camera = widget._camera
                for index in range(len(palette)):
                    selected = base_time + timedelta(hours=index + 1)
                    requested = chart_instant(selected)
                    lat, lon = (
                        camera.view()
                        .convert_screen_to_geographic(
                            camera.width * (0.3 + index * 0.12), camera.height * 0.5
                        )
                        .get_degrees()
                    )
                    replacements[requested] = MarineSnapshot(
                        marine.sources,
                        requested,
                        (CurrentArrow(lat, lon, index + 1, 0),),
                        "available",
                    )

                def synthetic_style(speed):
                    # flow() reports speed in knots; directions and geometry
                    # still go through production current_triangles().
                    from gweatherrouting.rendering.marinepresentation import flow

                    speeds = [flow(i + 1, 0)[0] for i in range(len(palette))]
                    index = min(
                        range(len(speeds)), key=lambda i: abs(speeds[i] - speed)
                    )
                    return 36, tuple(c / 255 for c in palette[index])

                def assert_latest_pixels(expected):
                    from PySide6.QtGui import QImage

                    image = widget.grabFramebuffer().convertToFormat(
                        QImage.Format.Format_RGBA8888
                    )
                    self.assertFalse(image.isNull())
                    rgba = (
                        np.frombuffer(image.constBits(), dtype=np.uint8)
                        .reshape(image.height(), image.bytesPerLine())[
                            :, : image.width() * 4
                        ]
                        .reshape(image.height(), image.width(), 4)
                    )
                    for color in palette:
                        mask = (
                            np.max(
                                np.abs(rgba[:, :, :3].astype(np.int16) - color), axis=2
                            )
                            <= 2
                        )
                        ys, xs = np.nonzero(mask)
                        if expected is None or color != palette[expected]:
                            self.assertEqual(
                                len(xs), 0, f"Stale {color} anywhere in framebuffer"
                            )
                            continue
                        self.assertGreater(len(xs), 30, "Latest marine arrow missing")
                        vertices = layer.item.vertices["position"]
                        scale = np.array(
                            [
                                image.width() / widget.width(),
                                image.height() / widget.height(),
                            ]
                        )
                        low, high = (
                            vertices.min(axis=0) * scale,
                            vertices.max(axis=0) * scale,
                        )
                        self.assertTrue(
                            np.all((xs >= low[0] - 2) & (xs <= high[0] + 2))
                            and np.all((ys >= low[1] - 2) & (ys <= high[1] + 2)),
                            "Current-color pixels include frozen geometry at an old camera",
                        )

                with patch(
                    "gweatherrouting.rendering.marinesnapshot.current_style",
                    side_effect=synthetic_style,
                ):
                    for index, (requested, latest) in enumerate(replacements.items()):
                        with self.subTest(marine_time=requested):
                            with lock:
                                cleared.clear()
                            sampling_release.clear()
                            try:
                                marine.time_control.set_time(
                                    base_time + timedelta(hours=index + 1)
                                )
                                presenter.request()
                                wait_for(
                                    lambda: layer._content
                                    == (marine.sources, requested, True)
                                    and layer.snapshot is None
                                    and layer.item in cleared,
                                    "New marine time never rendered its loading transition",
                                )
                                assert_latest_pixels(None)
                            finally:
                                sampling_release.set()
                            instant = (
                                requested  # Keep existing ownership assertions current.
                            )
                            wait_for(
                                lambda: layer.snapshot is not None
                                and layer.snapshot.instant == requested
                                and has_frame(widget._camera),
                                "Replacement marine time never reached a frame",
                            )

                            def assert_latest():
                                assert_ownership()
                                self.assertIs(layer.snapshot.sources, marine.sources)
                                self.assertEqual(layer.snapshot, latest)
                                rows = np.asarray(
                                    current_triangles(latest, widget._camera)
                                )
                                np.testing.assert_allclose(
                                    layer.item.vertices["position"],
                                    rows[:, :2],
                                    rtol=0,
                                    atol=1e-4,
                                )
                                np.testing.assert_array_equal(
                                    layer.item.vertices["color"], rows[:, 2:]
                                )
                                root = layer.item.parentItem()
                                items = []

                                def collect(parent):
                                    for child in parent.childItems():
                                        if isinstance(child, MarineCurrentItem):
                                            items.append(child)
                                        collect(child)

                                collect(root)
                                self.assertEqual(
                                    items, [layer.item], "Marine items accumulated"
                                )
                                with lock:
                                    actual = [
                                        r for r in node_geometry if r[0] is layer.item
                                    ][-1]
                                self.assertEqual(
                                    actual[1], layer.item.vertices.tobytes()
                                )
                                self.assertEqual(
                                    actual[2], 0, "Marine child nodes accumulated"
                                )
                                self.assertLessEqual(
                                    actual[3], 1, "Marine sibling nodes accumulated"
                                )
                                assert_latest_pixels(index)

                            assert_latest()
                            start = widget.rect().center()
                            QTest.mousePress(
                                widget, Qt.MouseButton.LeftButton, pos=start
                            )
                            end = start + QPoint(29, -13)
                            try:
                                event = QMouseEvent(
                                    QMouseEvent.Type.MouseMove,
                                    QPointF(end),
                                    QPointF(widget.mapToGlobal(end)),
                                    Qt.MouseButton.NoButton,
                                    Qt.MouseButton.LeftButton,
                                    Qt.KeyboardModifier.NoModifier,
                                )
                                before = widget._camera
                                self.app.sendEvent(widget, event)
                                self.assertTrue(event.isAccepted())
                                self.assertNotEqual(widget._camera, before)
                                wait_for(
                                    lambda: has_frame(widget._camera),
                                    "Latest-time pan frame missing",
                                )
                                assert_latest()
                            finally:
                                QTest.mouseRelease(
                                    widget, Qt.MouseButton.LeftButton, pos=end
                                )
                            wait_for(
                                lambda: not widget._pan_inertia_tick.isActive()
                                and has_frame(widget._camera),
                                "Latest-time pan did not settle",
                            )
                            assert_latest()
            finally:
                quick_window.afterRendering.disconnect(rendered)

    def test_viewport_camera_state_compatibility_aliases(self):
        from gweatherrouting.qt.viewportmap import ViewportMapState

        canvas = self.window.map
        self.assertIsInstance(canvas._view_state, ViewportMapState)
        self.assertIs(
            canvas._base_chart_renderer, canvas._weather_services.base_chart_renderer
        )
        self.assertIs(canvas.wind, canvas._weather_services.wind)
        self.assertIs(canvas._wind_cursor, canvas._weather_services.wind_cursor)
        self.assertIs(
            canvas.weather_coverage, canvas._weather_services.weather_coverage
        )
        self.assertIs(
            canvas._weather_presenter, canvas._weather_services.weather_presenter
        )
        canvas._center_lat = 47.1
        canvas._center_lon = -122.2
        canvas._zoom = 8.5
        self.assertEqual(
            (
                canvas._view_state.camera.center_lat,
                canvas._view_state.camera.center_lon,
                canvas._view_state.camera.zoom,
            ),
            (47.1, -122.2, 8.5),
        )
        canvas.points = ("track-point",)
        canvas.offsets = (3,)
        canvas.position = 2.5
        canvas.route_plan = ((1, 2),)
        canvas.route_preview = ((3, 4),)
        canvas.route = ((5, 6),)
        canvas.route_speeds = (7,)
        canvas.selected_route_point = 0
        canvas.ais_targets = ("target",)
        canvas.ais_visible = False
        self.assertEqual(canvas._view_state.points, ("track-point",))
        self.assertEqual(canvas._view_state.offsets, (3,))
        self.assertEqual(canvas._view_state.position, 2.5)
        self.assertEqual(canvas._view_state.route_plan, ((1, 2),))
        self.assertEqual(canvas._view_state.route_preview, ((3, 4),))
        self.assertEqual(canvas._view_state.route, ((5, 6),))
        self.assertEqual(canvas._view_state.route_speeds, (7,))
        self.assertEqual(canvas._view_state.selected_route_point, 0)
        self.assertEqual(canvas._view_state.ais_targets, ("target",))
        self.assertFalse(canvas._view_state.ais_visible)
        self.assertTrue(self.window.map.marine.visible)
        self.assertFalse(self.window.log_map.marine.visible)

        canvas._view_state.camera.center_lat = 46.2
        canvas._view_state.camera.center_lon = -121.3
        canvas._view_state.camera.zoom = 9.5
        self.assertEqual(
            (canvas._center_lat, canvas._center_lon, canvas._zoom),
            (46.2, -121.3, 9.5),
        )

    def test_live_wind_barbs_keep_size_over_zoomed_base_and_retina(self):
        from PySide6.QtGui import QImage, QPainter
        from gweatherrouting.rendering.overlayrenderer import RenderJob, RenderState
        from gweatherrouting.rendering.windpainter import flatten_wind_columns

        canvas = self.window.map
        style = (
            canvas.time_control.time,
            0.8,
            True,
            False,
            True,
        )
        key = (style, canvas.get_scale_factor(), ())
        job = RenderJob(key, MapViewport(canvas), (), 1, style)
        job.barb_flat = flatten_wind_columns(
            [[(0, 10, (canvas._center_lat, canvas._center_lon))]]
        )
        canvas.wind._states[canvas] = RenderState(key=key, frame=(job, None))
        original_zoom = canvas._zoom
        for factor in (1, 2):
            bounds = []
            for zoom in (original_zoom, original_zoom + 0.4, original_zoom + 2):
                canvas._zoom = zoom
                canvas._zoom_animating = True
                image = QImage(
                    canvas.width() * factor,
                    canvas.height() * factor,
                    QImage.Format.Format_ARGB32_Premultiplied,
                )
                image.setDevicePixelRatio(factor)
                image.fill(Qt.GlobalColor.transparent)
                painter = QPainter(image)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                canvas._paint_wind_barbs(painter)
                painter.end()
                import numpy as np

                pixels = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(
                    image.height(), image.bytesPerLine()
                )
                ys, xs = np.nonzero(pixels[:, 3 : image.width() * 4 : 4])
                self.assertGreater(len(xs), 0)
                bounds.append((xs.min(), ys.min(), xs.max(), ys.max()))
            self.assertEqual(bounds, [bounds[0]] * 3)
            self.assertAlmostEqual(
                (bounds[0][3] - bounds[0][1] + 1) / factor, 20, delta=1
            )
        canvas._zoom = original_zoom
        canvas._zoom_animating = False

        # The live overlay must still paint when the base/navigation caches
        # are reused, rather than becoming part of either scaled image.
        with patch.object(
            canvas, "_paint_wind_barbs", wraps=canvas._paint_wind_barbs
        ) as live:
            canvas.grab()
            canvas.grab()
            self.assertEqual(live.call_count, 2)

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
            self.assertEqual(dock.titleBarWidget().height(), 0)
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
        self.assertTrue(dock.isFloating())
        QTest.mousePress(dock, Qt.MouseButton.LeftButton, pos=QPoint(1, 100))
        self.assertIsNotNone(
            frame._resize,
            f"floating={dock.isFloating()} size={dock.size()} grabber={dock.mouseGrabber()}",
        )
        QTest.mouseRelease(dock, Qt.MouseButton.LeftButton, pos=QPoint(1, 100))
        self.assertIsNone(frame._resize)

    def test_full_plot_core_replay_and_source_visibility_without_transport(self):
        for name in ("play", "stop", "restart", "speed", "speed_group"):
            self.assertFalse(hasattr(self.window, name))
        log = self.load()
        self.assertFalse(self.controller.session.playing)
        self.assertEqual(
            len(self.window.plots.axes[0].lines[0].get_xdata()), len(log.points)
        )
        self.controller.session.set_playing(True)
        self.controller.session.advance(5)
        self.drain()
        self.assertTrue(self.controller.session.playing)
        self.assertEqual(self.window.plots.position, 5)
        self.assertEqual(tuple(self.controller.points), log.points)
        for cursor in self.window.plots.cursors:
            self.assertEqual(list(cursor.get_xdata()), [5, 5])
        self.controller.session.stop()
        self.assertFalse(self.controller.session.playing)
        self.assertEqual(self.window.plots.position, 5)
        self.controller.session.restart()
        self.assertEqual(self.window.plots.position, 0)
        self.controller.session.set_playing(True)
        self.controller.session.advance(100)
        self.assertFalse(self.controller.session.playing)
        self.assertEqual(self.window.plots.position, log.duration)
        self.window.source.setCurrentIndex(0)
        self.assertEqual(self.controller.points, [])
        self.window.source.setCurrentIndex(1)
        self.assertEqual(tuple(self.controller.points), log.points)
        self.window.nmea_dock.setFloating(True)
        self.drain()
        self.assertTrue(self.window.nmea_dock.isFloating())
        self.assertEqual(tuple(self.controller.points), log.points)

    def test_recording_persists_for_record_toggle_and_shutdown(self):
        paths = []
        for _ in range(2):
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
            self.window.record.click()
            self.assertFalse(self.controller.session.recording)
            self.assertEqual(tuple(self.controller.points), points)
            self.assertFalse(self.window.record.isChecked())
            self.assertEqual(self.controller.session.recording_path, path)
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
        self.assertTrue(self.controller.session.error)
        self.assertFalse(hasattr(self.window, "load_button"))
        self.assertFalse(hasattr(self.window, "file_info_button"))
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
        import cairo
        from PySide6.QtGui import QImage

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
        self.controller.charts.load_vector_layer(str(path))

        class Projection(MercatorProjection):
            _center_lat = 48.4
            _center_lon = -123.4
            _zoom = 6.0

            def get_allocated_width(self):
                return 600

            def get_allocated_height(self):
                return 400

            def get_scale_factor(self):
                return 1

            def queue_draw(self):
                pass

        canvas = Projection()
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 600, 400)
        cr = cairo.Context(surface)
        cr.set_source_rgb(0.0, 0.0, 1.0)
        cr.paint()
        self.controller.charts._draw_base(
            canvas, cr, tuple(self.controller.charts.charts)
        )
        surface.flush()
        image = QImage(
            surface.get_data(),
            surface.get_width(),
            surface.get_height(),
            surface.get_stride(),
            QImage.Format.Format_ARGB32_Premultiplied,
        ).copy()
        center = image.pixelColor(image.width() // 2, image.height() // 2)
        self.assertGreater(center.blue(), center.red())  # Hole remains sea.
        viewport = MapViewport(canvas, 0)
        x = (
            viewport._lon_to_x(-121.4)
            - viewport._lon_to_x(canvas._center_lon)
            + canvas.get_allocated_width() / 2
        )
        y = (
            viewport._lat_to_y(48.4)
            - viewport._lat_to_y(canvas._center_lat)
            + canvas.get_allocated_height() / 2
        )
        land = image.pixelColor(round(x), round(y))
        self.assertGreater(land.red(), land.blue())

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
                worker = Thread(target=feed, daemon=True)
                worker.start()
                received = []
                callback = lambda boat: received.append((get_ident(), boat.latitude))
                self.controller.core.connect("boat_position", callback)
                try:
                    self.load()
                    self.window.source.setCurrentIndex(0)
                    self.wait_for_routing_stage(
                        lambda: not self.controller.connection_task.busy
                    )
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
                    before = len(self.controller.points)
                    self.wait_for_routing_stage(
                        lambda: not self.controller.connection_task.busy
                    )
                    self.window.source.setCurrentIndex(1)
                    self.wait_for_routing_stage(
                        lambda: not self.controller.connection_task.busy
                    )
                    self.assertFalse(self.controller.session.streaming)
                    self.assertFalse(self.controller.session.recording)
                    self.assertFalse(self.controller.connections.polling_active)
                    self.assertEqual(self.controller.connections.sources, {})
                    self.assertIsNone(self.controller.connections.thread)
                    self.assertEqual(
                        self.controller.connections.connections[0]["port"],
                        server.getsockname()[1],
                    )
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
        self.window._boat_changed(SimpleNamespace(is_valid=lambda: False))
        self.assertEqual(self.window.instruments.text(), "")
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

    def test_chart_layer_panel_switches_sources_on_both_maps_during_gesture(self):
        from gweatherrouting.rendering.gdalrasterchart import GDALRasterChart

        object.__setattr__(self.controller.settings, "_Storage__init", True)
        self.window.show_library()
        panel = self.window.library_dialog.chart_layers_panel
        self.assertEqual(len(panel.checkboxes), 8)
        chart = GDALRasterChart("/disabled", self.controller.settings, enabled=False)
        self.controller.charts.charts.append(chart)
        for canvas in (self.window.map, self.window.log_map):
            canvas._base_cache = ("old pixels",)
            canvas.drag = (0, 0)
        panel.checkboxes["chartRasterVisible"].setChecked(False)
        self.assertFalse(self.controller.settings.chartRasterVisible)
        for canvas in (self.window.map, self.window.log_map):
            self.assertIsNone(canvas._base_cache)
            canvas.drag = None
        panel.checkboxes["chartRasterVisible"].setChecked(True)
        self.assertFalse(chart.enabled)
        panel.checkboxes["canadianCharts"].setChecked(False)
        self.assertFalse(self.controller.charts._chs_renderer.enabled)
        self.assertTrue(panel.checkboxes["chartENCVisible"].isChecked())

    def test_chart_layer_panel_tracks_external_settings_and_persists_choices(self):
        from gweatherrouting.core.settings import SettingsManager

        object.__setattr__(self.controller.settings, "_Storage__init", True)
        self.window.show_library()
        panel = self.window.library_dialog.chart_layers_panel
        self.controller.settings.canadianCharts = False
        self.assertFalse(panel.checkboxes["canadianCharts"].isChecked())
        self.window.chart_debug_panel.refresh()
        self.assertIn("CHS tiles: off", self.window.chart_debug_panel.summary.text())
        with (
            patch("gweatherrouting.core.storage.DATA_DIR", str(self.path)),
            patch.object(Storage, "save", ORIGINAL_STORAGE_SAVE),
        ):
            panel.checkboxes["chartENCVisible"].setChecked(False)
            with patch.object(Storage, "load_or_save_default", ORIGINAL_STORAGE_LOAD):
                reloaded = SettingsManager()
            self.assertFalse(reloaded.chartENCVisible)
            self.assertFalse(reloaded.canadianCharts)
            self.assertTrue(reloaded.chartWorldVisible)

    def test_mbtiles_import_toggle_persistence_and_remove_keeps_file(self):
        from gweatherrouting.core.settings import SettingsManager
        from gweatherrouting.rendering.mbtilesrenderer import MBTilesChart
        from test.test_mbtiles import make_mbtiles

        path = make_mbtiles(self.path / "offline.mbtiles")
        original_mtime = Path(path).stat().st_mtime_ns
        self.controller.data.import_chart(path, "mbtiles")
        self.wait_for_data()
        self.assertFalse(self.controller.data.error)
        chart = self.controller.charts.charts[0]
        self.assertIsInstance(chart, MBTilesChart)
        self.assertEqual(len(self.controller.settings.mbtilesCharts), 1)
        self.window.show_library()
        library = self.window.library_dialog
        self.assertIn("chartMBTilesVisible", library.chart_layers_panel.checkboxes)
        self.assertTrue(library.imported_chart(chart))
        self.assertEqual(library.charts.rowCount(), 1)
        with (
            patch("gweatherrouting.core.storage.DATA_DIR", str(self.path)),
            patch.object(Storage, "save", ORIGINAL_STORAGE_SAVE),
        ):
            object.__setattr__(self.controller.settings, "_Storage__init", True)
            self.controller.data.set_chart_enabled(chart, False)
            with patch.object(Storage, "load_or_save_default", ORIGINAL_STORAGE_LOAD):
                reloaded = SettingsManager()
            self.assertFalse(reloaded.mbtilesCharts[0]["enabled"])
            restored = self.controller.charts.load_mbtiles_layer(
                reloaded.mbtilesCharts[0]["path"],
                reloaded.mbtilesCharts[0]["metadata"],
                False,
            )
            self.assertEqual(restored.info, chart.info)
            self.controller.charts.charts.remove(restored)
            self.controller.data.remove_chart(chart)
        self.assertFalse(self.controller.settings.mbtilesCharts)
        self.assertTrue(Path(path).exists())
        self.assertEqual(Path(path).stat().st_mtime_ns, original_mtime)

    def test_chart_controls_are_consolidated_in_data_library(self):
        from PySide6.QtWidgets import QCheckBox, QDockWidget

        self.window.show_library()
        library = self.window.library_dialog
        library.show_charts()
        self.assertIs(library.tabs.currentWidget(), library.charts_page)
        self.assertEqual(
            [
                library.chart_sections.tabText(i)
                for i in range(library.chart_sections.count())
            ],
            ["Sources & appearance", "Local files", "Downloads & base maps"],
        )
        self.assertEqual(len(library.chart_layers_panel.checkboxes), 8)
        self.assertIsNone(self.window.findChild(QDockWidget, "chart-layers"))
        self.assertFalse(self.window.chart_debug_panel.findChildren(QCheckBox))
        self.window.show_settings()
        settings = self.window.settings_dialog
        self.assertFalse(hasattr(settings, "palette_combo"))
        self.assertFalse(hasattr(settings, "map_data"))
        self.assertNotIn(
            "Map Data", [settings.tabs.tabText(i) for i in range(settings.tabs.count())]
        )
        self.assertFalse(
            any("Canadian" in box.text() for box in settings.findChildren(QCheckBox))
        )

    def test_qt_overlay_completes_without_glib_and_shutdown_discards_callbacks(self):
        import cairo

        canvas = self.window.map
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
        self.window.show_library()
        settings = self.window.library_dialog
        settings.palette_combo.setCurrentText("dark")
        self.assertEqual(self.controller.settings.chartPalette, "dark")
        settings.hide()
        self.drain()
        self.assertIsNotNone(self.window.map.background)

    def wait_for_route(self):
        # The current-coverage preflight samples a corridor and checks it
        # against the coastline, which is slow on a loaded machine. This is a
        # hang guard, not a runtime target, so it is set well above the time
        # the check actually needs.
        deadline = time.monotonic() + 60
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

    def test_data_source_selection_controls_live_connections(self):
        controller = self.controller
        controller.offline = False
        self.load()
        release = Event()
        self.addCleanup(release.set)

        def start():
            controller.connections._polling_active = True

        def stop():
            release.wait(3)
            controller.connections._polling_active = False

        with (
            patch.object(
                controller.connections, "start_polling", side_effect=start
            ) as opened,
            patch.object(
                controller.connections, "stop_polling", side_effect=stop
            ) as closed,
        ):
            self.window.source.setCurrentIndex(0)
            self.wait_for_routing_stage(lambda: not controller.connection_task.busy)
            self.assertTrue(controller.session.streaming)
            self.assertFalse(self.window.record.isHidden())
            controller.session.start_recording()
            self.window.source.setCurrentIndex(1)
            self.assertFalse(controller.session.streaming)
            self.assertFalse(controller.session.recording)
            self.assertTrue(controller.connection_task.busy)
            self.assertTrue(self.window.record.isHidden())
            # A slow disconnect stays off the UI thread and cannot race a restart.
            controller.set_live_reading(True)
            self.assertFalse(controller.session.streaming)
            release.set()
            self.wait_for_routing_stage(lambda: not controller.connection_task.busy)
            self.assertFalse(controller.connections.polling_active)
            self.assertEqual(self.window._connection_message(), "")
            self.window.source.setCurrentIndex(0)
            self.wait_for_routing_stage(lambda: not controller.connection_task.busy)
            self.assertTrue(controller.session.streaming)
            self.assertTrue(controller.connections.polling_active)
            self.assertEqual(opened.call_count, 2)
            closed.assert_called_once()
            controller.connections._polling_active = False

    def test_live_reading_toggle_is_not_persisted_as_a_separate_setting(self):
        self.assertFalse(hasattr(self.window, "streaming"))
        self.assertNotIn("streaming", self.window.workspace.save_state()["nmea"])

    def test_reading_off_ignores_already_queued_live_packets(self):
        packet = NMEADataPacket.parse(rmc("120000"))
        self.controller.set_live_reading(False)
        with patch.object(self.controller.wind_pipe, "transform") as transform:
            self.controller._consume((packet,), self.controller.generation)
            transform.assert_not_called()
        self.assertEqual(self.controller.points, [])

    def test_connection_start_and_reconnect_worker_keep_playback_and_close_safely(self):
        started, release = Event(), Event()
        controller = self.controller

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
            controller.session.set_playing(True)
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
            set(self.window.tabifiedDockWidgets(self.window.nmea_dock)),
            {self.window.routing_dock},
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
        self.assertEqual(workspace.status.text(), "")
        self.assertTrue(workspace.status.isHidden())
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

    def test_wind_visibility_does_not_forget_prepared_weather(self):
        canvas = self.window.map
        with patch.object(canvas.wind, "forget") as forget:
            canvas.set_wind_visible(False)
            self.assertFalse(canvas.wind_visible)
            canvas.set_wind_visible(True)
            self.assertTrue(canvas.wind_visible)
            forget.assert_not_called()

    def test_map_display_controls_are_independent_and_survive_restart(self):
        self.window.weather_panel.wind.toggle()
        self.window.dashboard_action.trigger()
        self.assertFalse(self.window.map.wind_visible)
        self.assertFalse(self.window.map.dashboard_visible)
        self.window.views.setCurrentWidget(self.window.logs_view)
        self.assertTrue(self.window.weather_panel.wind.isChecked())
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
        self.assertFalse(self.window.weather_action.isVisible())
        self.assertFalse(self.window.dashboard_action.isVisible())
        self.window.views.setCurrentWidget(self.window.map)
        self.assertFalse(self.window.dashboard_action.isChecked())
        self.window.weather_panel.wind.toggle()
        self.window.dashboard_action.trigger()
        self.assertTrue(self.window.map.dashboard_visible)

    def test_weather_barb_density_updates_maps_and_persists(self):
        from gweatherrouting.core.settings import SettingsManager
        from gweatherrouting.rendering.windpainter import BARB_DENSITIES

        panel = self.window.weather_panel
        canvas = self.window.map
        with (
            patch("gweatherrouting.core.storage.DATA_DIR", str(self.path)),
            patch.object(Storage, "save", ORIGINAL_STORAGE_SAVE),
            patch.object(self.controller.data, "redraw") as redraw,
            patch.object(
                canvas.wind, "live_geometry_for_view", return_value=({}, 0.8)
            ) as geometry,
        ):
            ORIGINAL_STORAGE_LOAD(self.controller.settings)
            for key, _label, _spacing in BARB_DENSITIES:
                panel.barb_density.setCurrentIndex(panel.barb_density.findData(key))
                self.assertEqual(self.controller.settings.gribBarbDensity, key)
                panel.refresh()
                self.assertEqual(panel.barb_density.currentData(), key)
                canvas._paint_wind_barbs(None)
                self.assertEqual(geometry.call_args.kwargs["density"], key)
            redraw.assert_called()
            with patch.object(Storage, "load_or_save_default", ORIGINAL_STORAGE_LOAD):
                self.assertEqual(SettingsManager().gribBarbDensity, "all")
        self.window.views.setCurrentWidget(self.window.logs_view)
        self.assertEqual(panel.barb_density.currentData(), "all")

    def test_weather_streamline_style_updates_render_key_and_persists(self):
        from gweatherrouting.core.settings import SettingsManager

        panel, canvas = self.window.weather_panel, self.window.map
        before = canvas._wind_style_key()
        with (
            patch("gweatherrouting.core.storage.DATA_DIR", str(self.path)),
            patch.object(Storage, "save", ORIGINAL_STORAGE_SAVE),
            patch.object(self.controller.data, "redraw") as redraw,
        ):
            ORIGINAL_STORAGE_LOAD(self.controller.settings)
            panel.wind_style.setCurrentIndex(panel.wind_style.findData("streamlines"))
            self.assertEqual(self.controller.settings.gribWindStyle, "streamlines")
            self.assertNotEqual(canvas._wind_style_key(), before)
            self.assertTrue(panel.barb_density.isEnabled())
            self.assertEqual(
                panel.barb_density.itemText(panel.barb_density.findData("all")),
                "Maximum",
            )
            sparse_key = canvas._wind_style_key()
            panel.barb_density.setCurrentIndex(panel.barb_density.findData("dense"))
            self.assertNotEqual(canvas._wind_style_key(), sparse_key)
            self.assertEqual(self.controller.settings.gribBarbDensity, "dense")
            redraw.assert_called()
            with patch.object(Storage, "load_or_save_default", ORIGINAL_STORAGE_LOAD):
                self.assertEqual(SettingsManager().gribWindStyle, "streamlines")
            panel.wind_style.setCurrentIndex(panel.wind_style.findData("barbs"))
            self.assertTrue(panel.barb_density.isEnabled())
            self.assertEqual(
                panel.barb_density.itemText(panel.barb_density.findData("all")),
                "All forecast points",
            )
            barbs_key = canvas._wind_style_key()
            panel.barb_density.setCurrentIndex(panel.barb_density.findData("sparse"))
            self.assertEqual(canvas._wind_style_key(), barbs_key)

    def test_weather_panel_controls_and_layout_survive_restart(self):
        self.window.weather_action.trigger()
        self.drain()
        panel = self.window.weather_panel
        self.assertTrue(self.window.weather_open)
        self.assertFalse(self.window.weather_dock.isHidden())
        panel.wind.setChecked(False)
        self.assertTrue(self.controller.settings.gribIsobarsVisible)
        panel.interval.setValue(8)
        panel.isobar_opacity.setValue(60)
        panel.labels.setChecked(False)
        self.assertEqual(self.controller.settings.gribIsobarInterval, 8)
        self.assertEqual(self.controller.settings.gribIsobarOpacity, 0.6)
        self.assertFalse(self.controller.settings.gribIsobarLabels)
        panel.isobars.setChecked(False)
        self.assertFalse(panel.isobar_options.isEnabled())
        panel.isobars.setChecked(True)
        self.window.workspace.save()
        self.restart_workspace()
        self.assertTrue(self.window.weather_open)
        self.assertFalse(self.window.weather_dock.isHidden())
        self.assertFalse(self.window.weather_panel.wind.isChecked())
        self.window.views.setCurrentWidget(self.window.polar_workspace)
        self.assertTrue(self.window.weather_dock.isHidden())
        self.window.views.setCurrentWidget(self.window.logs_view)
        self.assertFalse(self.window.weather_dock.isHidden())
        self.assertTrue(self.window.weather_panel.wind.isChecked())
        self.window.weather_dock.setFloating(True)
        self.window.reset_panels()
        self.assertFalse(self.window.weather_dock.isFloating())
        self.assertFalse(self.window.weather_open)

    def test_bottom_status_bar_has_consistent_padding(self):
        margins = self.window.statusBar().contentsMargins()
        self.assertEqual(
            (margins.left(), margins.top(), margins.right(), margins.bottom()),
            (12, 4, 12, 4),
        )

    def test_cursor_columns_stay_put_when_observations_disappear(self):
        canvas = self.window.map
        status = self.window.cursor_status
        with (
            patch.object(canvas.charts, "depth_at", return_value={"area": (9.1, 18.2)}),
            patch.object(canvas._wind_cursor, "request") as wind,
        ):
            canvas._hover(QPointF(100, 100))
            wind.call_args.args[-1]((180, 10))
            QApplication.processEvents()
            positions = {key: label.x() for key, label in status.fields.items()}
            canvas._cursor_parts["depth"] = ""
            canvas._cursor_parts["wind"] = ""
            canvas._emit_cursor_readout()
            QApplication.processEvents()
            self.assertEqual(
                {key: label.x() for key, label in status.fields.items()}, positions
            )
            self.assertEqual(status.fields["depth"].text(), "")
            self.assertEqual(status.fields["wind"].text(), "")
            self.assertIn("Lat ", status.fields["coords"].text())
            self.window.statusBar().showMessage("Forecast models updated", 5000)
            QApplication.processEvents()
            self.assertTrue(status.fields["coords"].isVisible())

    def test_connection_completion_does_not_show_generic_done(self):
        self.controller.offline = False
        task = self.controller.connection_task
        task.message = "Done"
        self.assertEqual(self.window._connection_message(), "")
        task.message = "Connection failed"
        self.assertEqual(self.window._connection_message(), "Connection failed")

    def test_cursor_chart_depth_updates_immediately_and_keeps_wind_and_coordinates(
        self,
    ):
        canvas = self.window.map
        with (
            patch.object(
                canvas.charts,
                "depth_at",
                return_value={"sounding": 12.4, "distance_m": 35},
            ) as lookup,
            patch.object(canvas._wind_cursor, "request") as wind,
        ):
            canvas._hover(QPointF(100, 100))
            lookup.assert_called_once()
            self.assertIn("12.4 m (35 m away)", self.window.cursor_status.text())
            wind.call_args.args[-1]((180, 10))
            text = self.window.cursor_status.text()
            self.assertIn("12.4 m (35 m away)", text)
            self.assertIn("Sounding", text)
            self.assertNotIn("Nearby charted", text)
            self.assertIn("  ·  ", text)
            self.assertIn("Lat ", text)
            self.assertIn("Wind", text)

    def test_cursor_depth_range_preserves_decimals_and_cancels_on_source_change(self):
        canvas = self.window.map
        canvas.set_wind_visible(False)
        with patch.object(
            canvas.charts, "depth_at", return_value={"area": (9.1, 18.2)}
        ) as lookup:
            canvas._hover(QPointF(100, 100))
            self.assertIn("Depth 9.1–18.2 m", self.window.cursor_status.text())
            canvas.invalidate_chart_sources()
            self.assertEqual(self.window.cursor_status.text(), "")
            lookup.reset_mock()
            canvas._sample_depth()
            lookup.assert_not_called()

    def test_cursor_depth_lookup_skips_stale_view_and_leave(self):
        from PySide6.QtCore import QEvent

        canvas = self.window.map
        canvas.set_wind_visible(False)
        with patch.object(canvas.charts, "depth_at", return_value=None) as lookup:
            canvas._hover(QPointF(100, 100))
            lookup.reset_mock()
            canvas._zoom += 1
            canvas._sample_depth()
            lookup.assert_not_called()
            canvas._hover(QPointF(100, 100))
            lookup.reset_mock()
            canvas.leaveEvent(QEvent(QEvent.Type.Leave))
            canvas._sample_depth()
            lookup.assert_not_called()

    def test_cursor_depth_clears_when_individual_chart_selection_changes(self):
        canvas = self.window.map
        canvas.set_wind_visible(False)
        canvas._hover(QPointF(100, 100))
        self.controller.data.changed.emit()
        self.assertEqual(self.window.cursor_status.text(), "")

    def test_cursor_depth_updates_on_every_move_without_processing_timers(self):
        canvas = self.window.map
        canvas.set_wind_visible(False)
        with patch.object(canvas.charts, "depth_at") as lookup:
            for x, result, label in (
                (100, {"sounding": 12.4, "distance_m": 35}, "12.4 m"),
                (110, {"area": (9.1, 18.2)}, "9.1–18.2 m"),
                (120, None, None),
            ):
                lookup.return_value = result
                canvas.mouseMoveEvent(SimpleNamespace(position=lambda: QPointF(x, 100)))
                text = self.window.cursor_status.text()
                self.assertIn("Lat ", text)
                if label:
                    self.assertIn(label, text)
                else:
                    self.assertNotIn("Sounding", text)
                    self.assertNotIn("Depth", text)
                lat, lon = canvas.convert_screen_to_geographic(x, 100).get_degrees()
                lookup.assert_called_with(lat, lon, canvas.get_scale())
            self.assertEqual(lookup.call_count, 3)

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

    def test_forecast_scrubbing_updates_time_while_handle_is_held(self):
        from datetime import timedelta

        start = self.timeline_range()
        timeline = self.window.timeline
        timeline.slider.setSliderDown(True)
        for value in (600, 1200, 1800):
            timeline.slider.setValue(value)
            self.wait_for_cursor(
                lambda: self.controller.time_control.time
                == start + timedelta(seconds=value)
            )
            self.assertTrue(timeline.slider.isSliderDown())
        timeline.slider.setSliderDown(False)
        self.assertEqual(
            self.controller.time_control.time, start + timedelta(minutes=30)
        )

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
        self.window._ensure_library()
        self.drain()
        sources = self.window.library_dialog.marine.currents
        self.assertIn("Current data available", panel.coverage.text())
        root = sources.tree.topLevelItem(0)
        self.assertEqual(root.childCount(), 2)
        from shiboken6 import isValid

        child = root.child(0)
        child.setCheckState(0, Qt.CheckState.Unchecked)
        self.assertTrue(
            isValid(child)
        )  # The checkbox's native signal is still returning.
        self.drain()
        root = sources.tree.topLevelItem(0)
        self.assertEqual(root.checkState(0), Qt.CheckState.PartiallyChecked)
        self.assertEqual(len(manager.fields.snapshot().fields), 1)
        root.setCheckState(0, Qt.CheckState.Unchecked)
        self.drain()
        self.assertFalse(manager.fields.snapshot().fields)
        self.assertIsNone(self.window.timeline.bounds)
        sources.tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
        self.drain()
        self.controller.time_control.increase(hours=2)
        self.assertIn("Outside the loaded forecast times", panel.coverage.text())
        root = sources.tree.topLevelItem(0)
        identifier = root.child(0).data(0, Qt.ItemDataRole.UserRole)
        sources.tree.setCurrentItem(root.child(0))
        sources.remove.click()
        self.drain()
        self.assertNotIn(identifier, manager.fields.fields)
        self.assertEqual(len(manager.fields.fields), 1)

    def test_current_panel_reuses_field_order_but_samples_each_view_and_time(self):
        current, _ = self.marine_fixture()
        manager = self.controller.core.marine_manager
        manager.fields.add(current)
        panel = self.window.marine_panels["currents"]
        panel.invalidate_fields()
        with patch.object(
            manager.fields, "snapshot", wraps=manager.fields.snapshot
        ) as snapshot:
            for _ in range(3):
                self.window.map._pan_pixels(-16, 0)
                panel.refresh()
            snapshot.assert_not_called()
            from datetime import timedelta, timezone

            with patch(
                "gweatherrouting.qt.marinepanel.chart_instant",
                return_value=self.controller.time_control.time.astimezone(timezone.utc)
                + timedelta(hours=2),
            ):
                panel.refresh()
                snapshot.assert_not_called()
                self.assertIn(
                    "Outside the loaded forecast times", panel.coverage.text()
                )
            manager.fields.enable(current.id, False)
            self.assertGreater(snapshot.call_count, 0)
            self.assertFalse(panel._ordered_current_fields)
            self.assertIn("No enabled current models", panel.coverage.text())

    def test_marine_tides_heights_display_selection_and_provenance(self):
        current, tide = self.marine_fixture()
        manager = self.controller.core.marine_manager
        manager.add(tide)
        manager.enable(tide.id)
        manager.fields.add(current)
        self.drain()
        panel = self.window.marine_panels["tides"]
        self.window._ensure_library()
        self.drain()
        tides = self.window.library_dialog.marine.tides
        self.assertEqual(tides.item(0, 2).text(), "2.50 m")
        self.assertIn("1 of 1", panel.coverage.text())
        panel.toggle.setChecked(False)
        self.assertFalse(self.window.map.marine.tides_visible)
        self.assertTrue(self.window.map.marine.currents_visible)
        self.assertTrue(manager.is_enabled(tide.id))
        panel.toggle.setChecked(True)
        tides.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
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
        self.assertEqual(tides.rowCount(), 0)

    def test_marine_current_download_freezes_request_and_commits_on_ui_thread(self):
        current, _ = self.marine_fixture()
        manager = self.controller.core.marine_manager
        self.window._ensure_library()
        self.drain()
        marine = self.window.library_dialog.marine
        marine.hours["currents"].setValue(3)
        request = marine.targets["currents"].area()
        instant = marine.starts["currents"].instant()
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
            marine.downloads["currents"].click()
            self.wait_for_marine(started.is_set)
            self.assertFalse(marine.downloads["currents"].isEnabled())
            self.assertTrue(marine.jobs.busy)
            (
                self.window.map._center_lat,
                self.window.map._center_lon,
                self.window.map._zoom,
            ) = (0, 0, 5)
            self.window.map.queue_draw()
            self.controller.time_control.increase(hours=4)
            release.set()
            self.wait_for_marine(lambda: not marine.jobs.busy)
            operation.assert_called_once()
        self.assertEqual(commits, [get_ident()])
        self.assertIn(current.id, manager.fields.fields)
        self.assertIn("Second provider unavailable", marine.status.text())
        self.assertFalse(self.controller.session.recording)
        self.assertEqual(departure, self.window.routing_panel.departure.dateTime())

    def test_marine_cancel_and_close_discard_late_downloads_and_progress(self):
        current, _ = self.marine_fixture()
        panel = self.window.marine_panels["currents"]
        manager = self.controller.core.marine_manager
        self.window._ensure_library()
        self.drain()
        marine = self.window.library_dialog.marine
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
                marine.downloads["currents"].click()
                self.wait_for_marine(started.is_set)
                if close:
                    self.window.close()
                else:
                    marine.cancel.click()
                release.set()
                self.wait_for_marine(returned.is_set)
                if not close:
                    self.wait_for_marine(lambda: not marine.jobs.busy)
                    self.assertIn("Cancelled", marine.status.text())
                self.assertNotIn(current.id, manager.fields.fields)
        self.assertTrue(marine.closed)
        self.assertTrue(marine.jobs.closed)
        self.assertFalse(marine.timer.isActive())
        self.assertFalse(self.window.map.marine._hosts)
        self.assertNotIn(marine.sources_changed, manager.handlers["changed"])
        self.assertNotIn(marine.sources_changed, manager.fields.handlers["changed"])

    def test_marine_tide_route_download_across_date_line_and_saved_reuse(self):
        from dataclasses import replace
        from unittest.mock import Mock

        _, tide = self.marine_fixture()
        self.window._ensure_library()
        self.drain()
        marine = self.window.library_dialog.marine
        target, hours, start = (
            marine.targets["tides"],
            marine.hours["tides"],
            marine.starts["tides"],
        )
        station = replace(tide.station, latitude=10, longitude=179.9)
        tide = replace(tide, station=station)
        far = replace(station, id="example:far", longitude=0)
        points = [(10, 179.8), (10, -179.8)]
        route = SimpleNamespace(name="Date line route", to_list=lambda: list(points))
        target.core = SimpleNamespace(trackManager=[route], routingManager=[])
        target.refresh()
        target.setCurrentIndex(1)
        hours.setValue(1)
        marine.provider = Mock()
        started, release = Event(), Event()
        self.addCleanup(release.set)

        def discover(cancel):
            started.set()
            release.wait(5)
            return station, far

        marine.provider.discover.side_effect = discover
        marine.provider.download.return_value = tide
        marine.downloads["tides"].click()
        self.wait_for_marine(started.is_set)
        points[:] = [(0, 0)]
        release.set()
        self.wait_for_marine(lambda: not marine.jobs.busy)
        marine.provider.download.assert_called_once()
        self.assertEqual(marine.provider.download.call_args.args[0], station)
        self.assertTrue(marine.manager.is_enabled(tide.id))
        self.assertEqual(target.currentIndex(), 1)
        # Reuse a saved interval whose actual samples cover the request.
        hours.setValue(1)
        start.set_time(tide.requested_start)
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
        marine.manager.add(tide)
        marine.manager.enable(tide.id, False)
        marine.provider.download.reset_mock()
        marine.downloads["tides"].click()
        self.wait_for_marine(lambda: not marine.jobs.busy)
        marine.provider.download.assert_not_called()
        self.assertTrue(marine.manager.is_enabled(tide.id))
        self.assertIn("reused 1", marine.status.text())

    def test_marine_panel_preferences_and_download_start_survive_view_switch(self):
        from datetime import timedelta

        current, _ = self.marine_fixture()
        self.controller.core.marine_manager.fields.add(current)
        panel = self.window.marine_panels["currents"]
        self.window.marine_action.trigger()
        self.drain()
        self.assertFalse(self.window.marine_docks["currents"].isHidden())
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
        self.window.views.setCurrentWidget(self.window.map)
        self.assertFalse(self.window.marine_docks["currents"].isHidden())
        self.window.views.setCurrentWidget(self.window.logs_view)
        self.window.reset_panels()
        self.assertFalse(any(self.window.marine_open.values()))
        self.window.views.setCurrentWidget(self.window.map)
        self.window._ensure_library()
        self.drain()
        marine = self.window.library_dialog.marine
        self.assertFalse(marine.jobs.busy)
        instant = marine.starts["currents"].instant()
        self.controller.time_control.increase(hours=1)
        self.assertEqual(
            marine.starts["currents"].instant(), instant + timedelta(hours=1)
        )
        marine.starts["currents"].edited()
        self.controller.time_control.increase(hours=1)
        self.assertEqual(
            marine.starts["currents"].instant(), instant + timedelta(hours=1)
        )

    def test_marine_overlay_uses_shared_worker_and_cursor_values(self):
        current, tide = self.marine_fixture()
        manager = self.controller.core.marine_manager
        manager.fields.add(current)
        manager.add(tide)
        manager.enable(tide.id)
        canvas = self.window.map
        canvas.queue_draw()
        canvas.repaint()
        presenter = canvas._weather_presenter
        self.wait_for_marine(
            lambda: presenter.widget is not None
            and presenter.widget._marine_layer.snapshot is not None
        )
        snapshot = presenter.widget._marine_layer.snapshot
        self.assertTrue(snapshot.arrows)
        self.assertEqual(snapshot.instant, tide.requested_start)
        self.assertIn(
            "Current 4.3 kn toward 27°T", canvas.marine.cursor_text(49.5, -125.5)
        )
        canvas._hover(QPointF(canvas.width() / 2, canvas.height() / 2))
        self.assertIn("Test currents", self.window.cursor_status.text())
        canvas.hide()
        self.assertNotIn(canvas, canvas.marine._hosts)
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

    def test_routing_safety_settings_snapshot_and_survive_restart(self):
        self.plan_test_waypoints()
        panel = self.window.routing_panel
        self.assertIsNone(panel._request()[0].tws_max_kn)
        self.assertIsNone(panel._request()[0].min_sailing_speed_kn)
        self.assertFalse(panel.maximum_wind.isEnabled())
        panel.limit_wind.setChecked(True)
        panel.maximum_wind.setValue(28)
        panel.motor_fallback.setChecked(True)
        panel.minimum_sailing_speed.setValue(2.5)
        panel.motor_speed.setValue(6)
        panel.wave_proxy.setChecked(True)
        request, _ = panel._request()
        self.assertEqual(
            (request.tws_max_kn, request.min_sailing_speed_kn, request.motor_speed_kn),
            (28, 2.5, 6),
        )
        self.assertTrue(request.wave_correct)
        panel.maximum_wind.setValue(30)
        self.assertEqual(request.tws_max_kn, 28)
        self.controller.routing.busy = True
        panel.refresh()
        self.assertFalse(panel.maximum_wind.isEnabled())
        self.assertFalse(panel.motor_fallback.isEnabled())
        self.controller.routing.busy = False
        panel.refresh()
        self.window.workspace.save()
        self.restart_workspace()
        panel = self.window.routing_panel
        self.assertTrue(panel.limit_wind.isChecked())
        self.assertTrue(panel.motor_fallback.isChecked())
        self.assertTrue(panel.wave_proxy.isChecked())
        self.assertEqual(panel.maximum_wind.value(), 30)
        self.assertEqual(panel.motor_speed.value(), 6)

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

    def test_routing_current_priority_defaults_finest_persists_and_reaches_request(
        self,
    ):
        from gweatherrouting.core.routingjob import CalculatedRoute

        self.plan_test_waypoints()
        panel = self.window.routing_panel
        self.assertEqual(panel.current_priority.currentData(), "finest")
        self.window.workspace.save()
        payload = self.window.workspace.store.load()["routing"]["options"]
        self.assertEqual(payload["current_priority"], "finest")
        panel.use_currents.setChecked(True)
        panel.current_priority.setCurrentIndex(
            panel.current_priority.findData("newest")
        )
        seen = []

        def calculate(core, request, sources, providers, cancel, progress, **callbacks):
            seen.append(request.current_priority)
            return CalculatedRoute(((0, 0, "2026-10-01", 0, 1, 1, 0),), (), {})

        with patch(
            "gweatherrouting.qt.routingcontroller.calculate_route",
            side_effect=calculate,
        ):
            panel.calculate.click()
            self.wait_for_route()
        self.assertEqual(seen, ["newest"])
        self.window.workspace.save()
        payload = self.window.workspace.store.load()["routing"]["options"]
        self.assertEqual(payload["current_priority"], "newest")
        self.restart_workspace()
        self.assertEqual(
            self.window.routing_panel.current_priority.currentData(), "newest"
        )

    def test_routing_advanced_options_restore_without_calculations_or_downloads(self):
        panel = self.window.routing_panel
        panel.parameter_widgets["subdiv"].setValue(3)
        panel.parameter_widgets["min_increase"].setValue(4.5)
        enabled, values = panel.comparison_axes["polar_efficiency_pct"]
        enabled.setChecked(True)
        values.setText("90, 100")
        panel.use_currents.setChecked(True)
        self.window.workspace.save()
        payload = self.window.workspace.store.load()["routing"]["options"]
        self.assertTrue(payload["use_currents"])
        self.assertNotIn("auto_currents", payload)
        self.restart_workspace()
        panel = self.window.routing_panel
        self.assertTrue(panel.use_currents.isChecked())
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

    def test_map_data_library_missing_selection_and_no_automatic_download(self):
        self.assertIsNone(self.window.settings_dialog)
        self.window._ensure_library(chart_section=2)
        library = self.window.library_dialog
        page = library.map_data
        library.show_charts(section=2)
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
        library.hide()
        self.controller.load_charts = True
        self.window._offer_map_data()
        # _offer_map_data() now shows library if there's missing map data
        self.assertTrue(library.isVisible())
        self.assertIs(library.tabs.currentWidget(), library.charts_page)
        self.assertEqual(library.chart_sections.currentIndex(), 2)
        self.assertIsNone(self.window.settings_dialog)

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
        self.window._ensure_library(chart_section=2)
        page = self.window.library_dialog.map_data
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
            self.window.library_dialog.hide()
            self.controller.load_charts = True
            self.window._offer_map_data()
            # _offer_map_data() shows library if there's missing map data
            self.assertTrue(self.window.library_dialog.isVisible())

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
        from PySide6.QtGui import QImage, QPainter

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
        from PySide6.QtGui import QImage, QPainter

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
        from PySide6.QtGui import QImage, QPainter

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
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter

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

                canvas._paint_layer(
                    painter,
                    layer(),
                    QRectF(
                        -192,
                        -192,
                        canvas.width() + 384,
                        canvas.height() + 384,
                    ),
                )
                painter.end()
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


if __name__ == "__main__":
    unittest.main()
