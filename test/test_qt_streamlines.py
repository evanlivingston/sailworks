"""Focused viewport streamline ownership, invalidation and display checks."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import unittest
import time
from unittest.mock import Mock, patch
from pathlib import Path

import numpy as np
from PySide6.QtCore import QUrl
from PySide6.QtGui import QColor
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest

from gweatherrouting.qt.gribviewport import ViewportCamera, _viewport_widget_class
from gweatherrouting.qt.viewportstreamlines import (
    StreamlineImageProvider,
    ViewportStreamlineModel,
    encode_segments,
    clip_segments,
    wind_columns,
)
from gweatherrouting.qt.weatherpresenter import WeatherPresenter
from gweatherrouting.rendering.streamlines import geometry_for_view


class TestStreamlineModel(unittest.TestCase):
    def setUp(self):
        self.model = ViewportStreamlineModel(("selected.grb",), "selection-generation")
        # Deterministically drive completion without opening a real GRIB.
        self.model._thread = object()
        self.camera = ViewportCamera(width=120, height=100)

    def tearDown(self):
        self.model.close()

    def test_loading_ready_and_time_density_camera_invalidation(self):
        for camera in (
            self.camera,
            replace(self.camera, time=self.camera.time + timedelta(seconds=37)),
            replace(self.camera, density="dense"),
            replace(self.camera, center_lon=1),
            replace(self.camera, width=130),
            replace(self.camera, zoom=4),
            replace(self.camera, over_land=False),
        ):
            self.assertEqual(self.model.update(camera), ("loading", None))
            cancel = self.model._cancel
            flat = object()
            self.model._complete(camera, cancel, flat, "")
            self.assertEqual(self.model.update(camera), ("ready", flat))

    def test_stale_completion_hidden_closed_and_source_replacement(self):
        self.model.update(self.camera)
        old_cancel = self.model._cancel
        new_camera = replace(self.camera, zoom=4)
        self.model.update(new_camera)
        self.model._complete(self.camera, old_cancel, object(), "")
        self.assertEqual(self.model.update(new_camera), ("loading", None))
        self.assertEqual(self.model.update(new_camera, False), ("hidden", None))
        self.model._complete(new_camera, self.model._cancel, object(), "")
        self.assertEqual(self.model.update(new_camera, False), ("hidden", None))
        self.model.close()
        self.model._complete(new_camera, self.model._cancel, object(), "")
        self.assertEqual(self.model.update(new_camera), ("hidden", None))
        replacement = ViewportStreamlineModel(("other.grb",), "new-generation")
        self.assertNotEqual(replacement.paths, self.model.paths)
        self.assertNotEqual(replacement.display_identity, self.model.display_identity)
        replacement.close()

    def test_shared_trace_and_geometry_with_authoritative_time_and_nan_holes(self):
        camera = replace(
            self.camera,
            zoom=3,
            time=self.camera.time + timedelta(seconds=37, microseconds=123456),
        )
        nodes = np.array([(lat, lon) for lat in (-20, 0, 20) for lon in (-20, 0, 20)])
        sampler = Mock()
        sampler.indices = [SimpleNamespace(nodes=lambda _bounds: nodes)]

        def sample(_field, _instant, lats, _lons, _cancel):
            uv = np.zeros(lats.shape + (2,))
            uv[..., 0] = 5
            return uv

        sampler.sample.side_effect = sample
        cancel = lambda: False
        columns = wind_columns(sampler, camera, cancel)
        args = sampler.sample.call_args.args
        self.assertEqual(args[:2], ("wind", camera.time))
        self.assertIs(args[4], cancel)
        from gweatherrouting.rendering.streamlinepaths import (
            paths_to_flat,
            trace_streamline_paths,
        )

        paths = trace_streamline_paths(camera.view(), columns, lambda *_: False)
        self.assertTrue(paths)
        flat = paths_to_flat(paths)
        geometry = geometry_for_view(flat, 0, 0, 3, 120, 100, True)
        self.assertTrue(geometry)

        def sample_with_hole(*args):
            uv = sample(*args)
            uv[1, 1] = np.nan
            return uv

        sampler.sample.side_effect = sample_with_hole
        columns = wind_columns(sampler, camera, cancel)
        self.assertEqual(sum(int(np.isnan(column[3]).sum()) for column in columns), 1)

    def test_curvilinear_nodes_do_not_expand_to_cartesian_product(self):
        camera = replace(self.camera, width=4096, height=2160)
        nodes = np.column_stack(
            (np.linspace(40, 50, 2000), np.linspace(-130, -120, 2000))
        )
        sampler = Mock()
        sampler.indices = [SimpleNamespace(nodes=lambda _bounds: nodes)]

        def sample(_field, _instant, lats, _lons, _cancel):
            return np.zeros(lats.shape + (2,))

        sampler.sample.side_effect = sample
        columns = wind_columns(sampler, camera, lambda: False)
        self.assertLessEqual(len(columns), 512)
        self.assertLessEqual(len(columns) * len(columns[0][1]), 512 * 512)

    def test_presenter_selects_quick_style_without_muting_streamline_opacity(self):
        settings = SimpleNamespace(
            chartPalette="dark", gribArrowOpacity=0.7, gribWindStyle="streamlines"
        )
        canvas, widget, presenter = Mock(), Mock(), Mock()
        canvas.controller.settings = settings
        canvas.wind_visible = True
        canvas._desktop_viewport = True
        camera = object()
        canvas.requested_viewport_camera.return_value = camera
        widget._chart_palette = "dark"
        widget._camera = camera
        widget._environment = None
        presenter.canvas, presenter.widget = canvas, widget
        presenter._source, presenter._rendered = None, True
        for style in ("streamlines", "barbs"):
            settings.gribWindStyle = style
            WeatherPresenter._sync_active(presenter, None)
            widget.set_wind_style.assert_called_with(style)
            widget.set_wind_opacity.assert_called_with(0.7)
        canvas.wind_visible = False
        WeatherPresenter._sync_active(presenter, None)
        widget.set_wind_opacity.assert_called_with(0)


class TestStreamlinePublicationScheduling(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.widget_class = _viewport_widget_class()

    def make_host(self, prior, publication=(1, None)):
        streamlines = Mock()
        streamlines.property.side_effect = lambda name: {
            "ready": True,
            "shaderError": "",
        }[name]
        host = SimpleNamespace(
            _wind_style="streamlines",
            _wind_opacity=1,
            _streamlines=streamlines,
            _streamline_last_camera=prior,
            _streamline_display_camera=prior,
            _streamline_publication=publication,
            _streamline_urls=("old",),
            _streamline_settle=Mock(),
            _streamline_model=Mock(),
            _streamline_provider=Mock(),
            _show_state=Mock(),
            _status=Mock(),
            _streamline_readiness_changed=Mock(),
        )
        host._update_streamline_camera = (
            lambda camera: self.widget_class._update_streamline_camera(host, camera)
        )
        return host

    def camera(self):
        return ViewportCamera(width=120, height=100)

    def test_pan_reuses_valid_publication_and_settles(self):
        prior = self.camera()
        camera = replace(prior, center_lon=1)
        host = self.make_host(prior, (1, prior))
        self.widget_class._publish_streamlines(host, camera)
        host._streamline_model.update.assert_not_called()
        host._streamline_provider.publish.assert_not_called()
        self.assertEqual(
            host._streamlines.setProperty.call_args_list[0].args[0], "cameraCenter"
        )
        host._streamlines.setProperty.assert_any_call("cameraZoom", camera.zoom)
        host._streamline_settle.start.assert_called_once_with()

    def test_reselect_restores_valid_publication_without_retracing(self):
        camera = self.camera()
        host = self.make_host(camera, (1, camera))
        self.widget_class._publish_streamlines(host, camera)
        host._streamlines.setVisible.assert_called_with(True)
        host._streamline_model.update.assert_not_called()
        host._streamline_provider.publish.assert_not_called()

    def test_loading_new_time_retains_prior_groups_dimmed(self):
        prior = self.camera()
        camera = replace(prior, time=prior.time + timedelta(seconds=37))
        host = self.make_host(prior, (1, prior))
        host._streamline_model.update.return_value = ("loading", None)
        self.widget_class._publish_streamlines(host, camera)
        host._streamline_model.update.assert_called_once_with(camera, True)
        host._streamlines.setOpacity.assert_any_call(0.6)
        host._streamlines.setVisible.assert_called_with(True)
        host._status.setText.assert_called_with(
            "Updating streamlines — previous forecast time"
        )
        host._streamline_provider.publish.assert_not_called()

    def test_missing_publication_does_not_take_reuse_fast_path(self):
        camera = self.camera()
        host = self.make_host(camera, None)
        host._streamline_model.update.return_value = ("ready", ())
        self.assertEqual(
            self.widget_class._publish_streamlines(host, camera), "unavailable"
        )
        host._streamline_model.update.assert_called_once_with(camera, True)
        host._streamline_provider.publish.assert_not_called()
        host._show_state.assert_called_with("unavailable")


class TestStreamlineShaderStructure(unittest.TestCase):
    def test_pixel_space_quads_and_connector_discard(self):
        shader_dir = Path(__file__).parents[1] / "gweatherrouting/qt/shaders"
        vertex = (shader_dir / "streamlines.vert").read_text()
        fragment = (shader_dir / "streamlines.frag").read_text()
        qml = (shader_dir / "WeatherStreamlines.qml").read_text()
        self.assertIn("256.0 * exp2(cameraZoom)", vertex)
        self.assertIn("vec2 cameraCenter;", vertex)
        self.assertIn("arrowMode == 0 ? 2 : 4", vertex)
        self.assertIn("marker = float(vertexY / 2);", vertex)
        self.assertIn("b - 5.0*direction", vertex)
        self.assertIn("2.5 : -2.5", vertex)
        self.assertIn("abs(marker - round(marker)) > 0.00001", fragment)
        self.assertIn(
            "Math.max(1, batch.segmentCount * (batch.arrow ? 4 : 2) - 1)", qml
        )
        self.assertIn("required property var modelData", qml)
        self.assertIn("effect.status === ShaderEffect.Compiled", qml)
        self.assertIn("property vector2d cameraCenter", qml)
        self.assertNotIn("previewScale", qml)
        self.assertIn("Math.min(8192, batchSize)", qml)

    def test_payload_batch_limit_and_geometric_clipping(self):
        image, count = encode_segments(np.zeros((8192, 4)))
        self.assertEqual((image.height(), count), (8192, 8192))
        with self.assertRaises(ValueError):
            encode_segments(np.zeros((8193, 4)))
        clipped = clip_segments(
            [[-5, 5, 15, 5], [-5, -5, -1, -1], [2, 2, 8, 8]], 10, 10
        )
        np.testing.assert_allclose(clipped, [[0, 5, 10, 5], [2, 2, 8, 8]])


class TestStreamlineShaderPixels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_qquickwindow_draws_and_withdraws_streamlines(self):
        shader_dir = Path(__file__).parents[1] / "gweatherrouting/qt/shaders"
        engine = QQmlEngine()
        provider = StreamlineImageProvider()
        engine.addImageProvider("streamlines", provider)
        component = QQmlComponent(
            engine, QUrl.fromLocalFile(str(shader_dir / "WeatherStreamlines.qml"))
        )
        if component.isError():
            self.fail(
                "QML component failed: "
                + "; ".join(e.toString() for e in component.errors())
            )
        layer = component.create()
        self.assertIsNotNone(layer, "; ".join(e.toString() for e in component.errors()))
        window = QQuickWindow()
        window.setWidth(120)
        window.setHeight(100)
        window.setColor(QColor("white"))
        scene_errors = []
        window.sceneGraphError.connect(
            lambda error, message: scene_errors.append((error, message))
        )
        try:
            layer.setParentItem(window.contentItem())
            layer.setWidth(window.contentItem().width())
            layer.setHeight(window.contentItem().height())
            window.show()
            layer.setWidth(window.contentItem().width())
            layer.setHeight(window.contentItem().height())
            self.assertGreater(layer.width(), 0)
            self.assertGreater(layer.height(), 0)
            self._pump()
            image_url, count = provider.publish(
                "horizontal",
                np.array([[0.5 + (20 - 60) / 256, 0.5, 0.5 + (100 - 60) / 256, 0.5]]),
            )
            layer.setProperty(
                "groups",
                [
                    {"source": image_url, "count": count, "color": QColor("red")},
                    {
                        "source": image_url,
                        "count": count,
                        "color": QColor("red"),
                        "arrow": True,
                    },
                ],
            )
            layer.setProperty(
                "cameraCenter",
                __import__("PySide6.QtGui", fromlist=["QVector2D"]).QVector2D(0.5, 0.5),
            )
            layer.setProperty("cameraZoom", 0)
            deadline = time.monotonic() + 2.0
            while (
                not layer.property("ready")
                and not layer.property("shaderError")
                and time.monotonic() < deadline
            ):
                QTest.qWait(10)
                self.app.processEvents()
            self.assertTrue(
                layer.property("ready"),
                f"root error={layer.property('shaderError')!r}; "
                f"{layer.property('batchDiagnostics')}",
            )
            self.assertFalse(
                layer.property("shaderError"),
                f"{layer.property('shaderError')!r}; "
                f"{layer.property('batchDiagnostics')}",
            )
            pixels = window.grabWindow()
            if pixels.isNull():
                self.skipTest(
                    "This Qt platform does not support QQuickWindow framebuffer readback"
                )
            self.assertFalse(
                scene_errors, f"Qt Quick scene graph errors: {scene_errors}"
            )
            # grabWindow() returns physical pixels while the QML scene and
            # segment coordinates are in device-independent pixels (notably
            # on Retina/macOS, where the framebuffer is 2x).
            dpr = pixels.width() / window.width()
            sample_y = round(50 * dpr)
            red_pixels = sum(
                1
                for x in range(20, 101)
                if pixels.pixelColor(round(x * dpr), sample_y).red() > 180
                and pixels.pixelColor(round(x * dpr), sample_y).green() < 100
            )
            red_points = [
                (x, y)
                for y in range(pixels.height())
                for x in range(pixels.width())
                if pixels.pixelColor(x, y).red() > 180
                and pixels.pixelColor(x, y).green() < 100
                and pixels.pixelColor(x, y).blue() < 100
            ]
            red_bounds = (
                (
                    min(x for x, _ in red_points) / dpr,
                    min(y for _, y in red_points) / dpr,
                    max(x for x, _ in red_points) / dpr,
                    max(y for _, y in red_points) / dpr,
                )
                if red_points
                else None
            )
            self.assertGreater(
                red_pixels,
                20,
                f"expected red horizontal segment near y=50, got bounds {red_bounds}",
            )
            # The chevron tip is the segment endpoint; branches extend 5 px
            # backward and 2.5 px to either side of its direction.
            for x, y in ((96, 48), (96, 52)):
                self.assertTrue(
                    any(
                        pixels.pixelColor(px, py).red() > 180
                        and pixels.pixelColor(px, py).green() < 100
                        for px in range(round((x - 1) * dpr), round((x + 2) * dpr))
                        for py in range(round((y - 1) * dpr), round((y + 2) * dpr))
                    ),
                    f"expected arrow chevron near ({x}, {y}) at DPR {dpr}",
                )
            layer.setProperty("groups", [])
            self._pump()
            withdrawn = window.grabWindow()
            self.assertFalse(
                withdrawn.isNull(), "readback became empty after withdrawal"
            )
            self.assertEqual(
                withdrawn.pixelColor(round(60 * dpr), round(50 * dpr)),
                QColor("white"),
            )
        finally:
            window.close()
            layer.deleteLater()
            engine.deleteLater()

    def test_qquickwindow_draws_arrow_only_and_withdraws_it(self):
        """Measure arrow mode independently from the combined shader groups."""
        shader_dir = Path(__file__).parents[1] / "gweatherrouting/qt/shaders"
        engine = QQmlEngine()
        provider = StreamlineImageProvider()
        engine.addImageProvider("streamlines", provider)
        component = QQmlComponent(
            engine, QUrl.fromLocalFile(str(shader_dir / "WeatherStreamlines.qml"))
        )
        self.assertFalse(
            component.isError(), "; ".join(e.toString() for e in component.errors())
        )
        layer = component.create()
        self.assertIsNotNone(layer, "; ".join(e.toString() for e in component.errors()))
        window = QQuickWindow()
        window.setWidth(120)
        window.setHeight(100)
        window.setColor(QColor("white"))
        scene_errors = []
        window.sceneGraphError.connect(
            lambda error, message: scene_errors.append((error, message))
        )
        try:
            layer.setParentItem(window.contentItem())
            layer.setWidth(window.contentItem().width())
            layer.setHeight(window.contentItem().height())
            window.show()
            self._pump()
            image_url, count = provider.publish(
                "arrow-only",
                np.array([[0.5 + (20 - 60) / 256, 0.5, 0.5 + (100 - 60) / 256, 0.5]]),
            )
            layer.setProperty(
                "groups",
                [
                    {
                        "source": image_url,
                        "count": count,
                        "color": QColor("red"),
                        "arrow": True,
                    }
                ],
            )
            from PySide6.QtGui import QVector2D

            layer.setProperty("cameraCenter", QVector2D(0.5, 0.5))
            layer.setProperty("cameraZoom", 0)
            deadline = time.monotonic() + 2.0
            while (
                not layer.property("ready")
                and not layer.property("shaderError")
                and time.monotonic() < deadline
            ):
                QTest.qWait(10)
                self.app.processEvents()
            self.assertTrue(
                layer.property("ready"),
                f"root error={layer.property('shaderError')!r}; {layer.property('batchDiagnostics')}",
            )
            self.assertFalse(layer.property("shaderError"))
            pixels = window.grabWindow()
            if pixels.isNull():
                self.skipTest(
                    "This Qt platform does not support QQuickWindow framebuffer readback"
                )
            self.assertFalse(
                scene_errors, f"Qt Quick scene graph errors: {scene_errors}"
            )
            dpr = pixels.width() / window.width()
            for x, y in ((96, 48), (96, 52)):
                self.assertTrue(
                    any(
                        pixels.pixelColor(px, py).red() > 180
                        and pixels.pixelColor(px, py).green() < 100
                        for px in range(round((x - 1) * dpr), round((x + 2) * dpr))
                        for py in range(round((y - 1) * dpr), round((y + 2) * dpr))
                    ),
                    f"expected arrow-only chevron near ({x}, {y}) at DPR {dpr}",
                )
            layer.setProperty("groups", [])
            self._pump()
            withdrawn = window.grabWindow()
            self.assertFalse(
                withdrawn.isNull(), "readback became empty after withdrawal"
            )
            self.assertEqual(
                withdrawn.pixelColor(round(60 * dpr), round(50 * dpr)), QColor("white")
            )
        finally:
            window.close()
            layer.deleteLater()
            engine.deleteLater()

    def _pump(self):
        for _ in range(20):
            self.app.processEvents()


class TestStreamlineImageRetention(unittest.TestCase):
    def test_more_than_32_active_images_survive_until_replacement(self):
        provider = StreamlineImageProvider()
        urls = [provider.publish(str(i), np.zeros((1, 4)))[0] for i in range(40)]
        for url in urls:
            image = provider.requestImage(url.rsplit("/", 1)[1], None, None)
            self.assertFalse(image.isNull(), url)
        provider.retain(urls[-2:])
        for url in urls[:-2]:
            self.assertTrue(
                provider.requestImage(url.rsplit("/", 1)[1], None, None).isNull()
            )
        for url in urls[-2:]:
            self.assertFalse(
                provider.requestImage(url.rsplit("/", 1)[1], None, None).isNull()
            )
        provider.replace_generation()
        for url in urls:
            self.assertTrue(
                provider.requestImage(url.rsplit("/", 1)[1], None, None).isNull()
            )
