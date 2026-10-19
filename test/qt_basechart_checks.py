"""Qt-only base portrayal worker checks, run in an isolated process."""

import json
import threading
from unittest.mock import patch

import cairo
from osgeo import ogr
from PySide6.QtCore import Qt, QTimer

from gweatherrouting.core.mapviewport import MapViewport
from gweatherrouting.qt.basecharts import BaseChartRenderer, describe_sources
from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart
from gweatherrouting.rendering.gshhs import GSHHSVectorChart
from gweatherrouting.rendering.overlayrenderer import MARGIN, RenderJob
from gweatherrouting.rendering.vectordrawer.simplechartdrawer import SimpleChartDrawer
from test.qt_gpu_checks import Fixture


class _DummySource:
    """Hashable stand-in with the attributes renderer identity needs."""

    def __init__(self, time):
        self.path = "test"
        self.name = "test"
        self.centre = "test"
        self.start_time = time
        self.last_forecast = time
        self.bounds = ((0, 0), (1, 1))


class BaseChartChecks(Fixture):
    def zoom_event(self, delta):
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QWheelEvent

        return QWheelEvent(
            QPointF(83, 71),
            QPointF(83, 71),
            QPoint(),
            QPoint(0, delta),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )

    def test_zoom_prediction_preserves_off_center_anchor_without_moving_camera(self):
        canvas = self.window.map
        canvas._smooth_zoom = True
        canvas.wheelEvent(self.zoom_event(480))
        target = canvas._zoom_target
        camera = (canvas._center_lat, canvas._center_lon, canvas._zoom)
        canvas._zoom = target
        canvas._recenter_on_anchor()
        expected = (canvas._center_lat, canvas._center_lon)
        canvas._center_lat, canvas._center_lon, canvas._zoom = camera
        canvas._zoom_prefetch.stop()
        with patch.object(canvas.wind, "prefetch_at", return_value=True) as prefetch:
            self.assertTrue(canvas._prefetch_zoom_target())
            args = prefetch.call_args.args
            self.assertAlmostEqual(args[1], expected[0])
            self.assertAlmostEqual(args[2], expected[1])
            self.assertEqual(prefetch.call_args.kwargs, {"zoom": target})
            self.assertEqual(
                camera, (canvas._center_lat, canvas._center_lon, canvas._zoom)
            )

    def test_zoom_prediction_debounces_input_and_stops_with_gesture(self):
        canvas = self.window.map
        canvas._smooth_zoom = True
        with patch.object(canvas.wind, "prefetch_at") as prefetch:
            with patch.object(canvas.wind, "cancel_speculation") as cancel:
                canvas.wheelEvent(self.zoom_event(120))
                canvas.wheelEvent(self.zoom_event(-240))
                self.assertEqual(cancel.call_count, 2)
                self.assertTrue(canvas._zoom_prefetch.isActive())
                prefetch.assert_not_called()
                canvas._finish_zoom_silent()
                self.assertFalse(canvas._zoom_prefetch.isActive())
                self.assertFalse(canvas._prefetch_zoom_target())
                prefetch.assert_not_called()

    def test_zoom_prediction_control_disables_speculation_not_animation(self):
        canvas = self.window.map
        canvas._smooth_zoom = True
        canvas._zoom_prefetch_enabled = False
        with patch.object(canvas.wind, "cancel_speculation") as cancel:
            canvas.wheelEvent(self.zoom_event(120))
            self.assertTrue(canvas._zoom_animating)
            self.assertIsNotNone(canvas._zoom_target)
            self.assertFalse(canvas._zoom_prefetch.isActive())
            cancel.assert_not_called()

    def test_quiet_native_zoom_prepares_current_pinch_scale(self):
        canvas = self.window.map
        canvas._begin_native_gesture()
        canvas._zoom_animating = True
        canvas._zoom += 0.4
        camera = (canvas._center_lat, canvas._center_lon, canvas._zoom)
        with patch.object(canvas.wind, "prefetch_at", return_value=True) as prefetch:
            self.assertTrue(canvas._prefetch_zoom_target())
            self.assertEqual(prefetch.call_args.args, (canvas, camera[0], camera[1]))
            self.assertEqual(prefetch.call_args.kwargs, {"zoom": camera[2]})
        self.assertEqual(camera, (canvas._center_lat, canvas._center_lon, canvas._zoom))

    def simple_chart(self):
        path = self.path / "base.geojson"
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
                                        [-125, 47],
                                        [-122, 47],
                                        [-122, 50],
                                        [-125, 50],
                                        [-125, 47],
                                    ]
                                ],
                            },
                        }
                    ],
                }
            )
        )
        return GDALVectorChart(str(path), self.controller.settings)

    def surface(self, canvas):
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            canvas.get_allocated_width(),
            canvas.get_allocated_height(),
        )
        return surface, cairo.Context(surface)

    def test_private_worker_matches_simple_and_gshhs_portrayal(self):
        canvas = self.window.map
        chart = self.simple_chart()
        coast = GSHHSVectorChart.__new__(GSHHSVectorChart)
        coast.enabled, coast.path, coast.force_downscale = (
            True,
            str(self.path / "gshhs"),
            False,
        )
        coast.countries = (("test", "Label", "48.4", "-123.4"),)
        coast.last_time = {quality: 0 for quality in ("f", "h", "i", "l", "c")}
        coast.drawer = SimpleChartDrawer(self.controller.settings)
        coast.vector_files = {}
        for quality in coast.last_time:
            path = self.path / "gshhs" / "GSHHS_shp" / quality
            path.mkdir(parents=True)
            target = path / f"GSHHS_{quality}_L1.shp"
            dataset = ogr.GetDriverByName("ESRI Shapefile").CopyDataSource(
                chart.vector_file, str(target)
            )
            self.assertIsNotNone(dataset)
            dataset = None
            coast.vector_files[quality + "1"] = ogr.Open(str(target))
        renderer = canvas._base_chart_renderer
        for source in (chart, coast):
            for zoom in (3, 8, 14):
                for palette in ("dark", "cm93"):
                    canvas._zoom = zoom
                    source.drawer.palette = palette
                    viewport = MapViewport(canvas, MARGIN)
                    descriptor = describe_sources([source])[0]
                    job = RenderJob((), viewport, ((descriptor,),), 1, palette)
                    candidate = renderer._render(job)
                    reference = cairo.ImageSurface(
                        cairo.FORMAT_ARGB32, viewport.width, viewport.height
                    )
                    source.do_draw(viewport, cairo.Context(reference))
                    reference.flush()
                    self.assertEqual(
                        bytes(candidate.get_data()),
                        bytes(reference.get_data()),
                        (type(source), zoom, palette),
                    )
                    private = renderer._charts[descriptor]
                    self.assertIsNot(private, source)
                    if source is chart:
                        self.assertIsNot(private.vector_file, source.vector_file)
                    else:
                        for quality in source.last_time:
                            self.assertIsNot(
                                private.vector_files[quality + "1"],
                                source.vector_files[quality + "1"],
                            )

    def test_delayed_portrayal_does_not_block_ui_or_use_gui_chart(self):
        canvas = self.window.map
        chart = self.simple_chart()
        self.controller.charts.charts = [chart]
        renderer = canvas._base_chart_renderer
        entered, release = threading.Event(), threading.Event()
        original = renderer._render
        threads = []

        def delayed(job):
            threads.append(threading.get_ident())
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test worker was not released")
            return original(job)

        self.addCleanup(release.set)
        renderer._render = delayed
        with patch.object(
            chart, "do_draw", side_effect=AssertionError("GUI chart used by worker")
        ):
            canvas._build_padded_layer(canvas._base_image)
            self.assertTrue(entered.wait(2))
            callbacks = []
            QTimer.singleShot(0, lambda: callbacks.append(True))
            self.drain()
            self.assertEqual(callbacks, [True])
            self.assertTrue(renderer._running)
            self.assertNotEqual(threads[0], threading.get_ident())
            release.set()
            self.wait(lambda: renderer._states[canvas].frame is not None)
        self.wait(lambda: renderer._states[canvas].requested is None)

    def test_forgotten_chart_handles_release_only_after_worker_completion(self):
        from gweatherrouting.rendering.overlayrenderer import RenderState

        canvas = self.window.map
        renderer = canvas._base_chart_renderer
        chart = self.simple_chart()
        descriptor = describe_sources([chart])[0]
        job = RenderJob((), MapViewport(canvas), ((descriptor,),), 1, "cm93")
        renderer._worker_chart(descriptor, "cm93")
        renderer._states[canvas] = RenderState(requested=job)
        renderer._running = True
        renderer._active_canvas, renderer._active_job = canvas, job
        renderer.forget(canvas)
        self.assertTrue(job.cancel.is_set())
        self.assertIn(descriptor, renderer._charts)
        renderer._completed(canvas, job, None, None)
        self.assertFalse(renderer._charts)

    def test_idle_chart_handles_release_when_map_is_forgotten(self):
        canvas = self.window.map
        renderer = canvas._base_chart_renderer
        chart = self.simple_chart()
        descriptor = describe_sources([chart])[0]
        renderer._worker_chart(descriptor, "cm93")
        renderer.forget(canvas)
        self.assertFalse(renderer._charts)

    def test_worker_margin_preserves_visible_pixels_and_fractional_pan(self):
        canvas = self.window.map
        chart = self.simple_chart()
        palette = self.controller.settings.chartPalette
        canvas._zoom = 8
        renderer = canvas._base_chart_renderer
        viewport = MapViewport(canvas, MARGIN)
        job = RenderJob((), viewport, ((describe_sources([chart])[0],),), 1, palette)
        frame = renderer._render(job)
        # Prime the unchanged 128px-margin reference at the same camera.
        scratch, context = self.surface(canvas)
        self.controller.charts._draw_base(canvas, context, [chart])
        for delta in (0, 0.5, 64):
            canvas._pan_pixels(delta, 0)
            expected, reference_cr = self.surface(canvas)
            actual, candidate_cr = self.surface(canvas)
            self.controller.charts._draw_base(canvas, reference_cr, [chart])
            renderer._paint(canvas, candidate_cr, job, frame)
            expected.flush()
            actual.flush()
            self.assertEqual(
                bytes(actual.get_data()), bytes(expected.get_data()), delta
            )

    def test_release_velocity_triggers_bounded_directional_prefetch(self):
        from types import SimpleNamespace

        from gweatherrouting.rendering.overlayrenderer import RenderState

        canvas = self.window.map
        renderer = canvas.wind
        source = _DummySource(canvas.time_control.time)
        palette = (canvas.time_control.time, 0.8, True, True, True)
        renderer._states.setdefault(canvas, RenderState())
        renderer._last_content[canvas] = (((source,),), palette, 1)
        # Jitter inside the reuse margin never speculates.
        canvas._release_velocity = (100.0, 0.0)
        canvas._refresh_after_gesture()
        self.assertIsNone(canvas._release_velocity)
        self.assertFalse(renderer._speculative)
        # A real flick queues one bounded lookahead and consumes the vector.
        canvas._release_velocity = (900.0, 0.0)
        canvas._refresh_after_gesture()
        self.assertIsNone(canvas._release_velocity)
        job = renderer._speculative.get(canvas)
        if job is None and renderer._speculative_active is not None:
            self.assertIs(renderer._speculative_active[0], canvas)
            job = renderer._speculative_active[1]
        self.assertIsNotNone(job)
        limit = min(MARGIN, 0.75 * max(canvas.width(), canvas.height()))
        expected = canvas.convert_screen_to_geographic(
            canvas.width() / 2 - 900.0 * min(1.0, limit / 900.0),
            canvas.height() / 2,
        ).get_degrees()
        self.assertAlmostEqual(job.viewport._center_lat, expected[0])
        self.assertAlmostEqual(job.viewport._center_lon, expected[1])
        renderer.cancel_speculation()

    def test_trackpad_stream_queues_mid_gesture_speculation_ahead(self):
        import time
        from types import SimpleNamespace

        from gweatherrouting.rendering.overlayrenderer import RenderState
        from test.qt_preview_checks import QtPreviewChecks

        canvas = self.window.map
        canvas._trackpad_scroll_pans = True
        renderer = canvas.wind
        source = _DummySource(canvas.time_control.time)
        palette = (canvas.time_control.time, 0.8, True, True, True)
        renderer._states.setdefault(canvas, RenderState())
        renderer._last_content[canvas] = (((source,),), palette, 1)
        lon_before = canvas._center_lon
        canvas._trackpad_pan(
            QtPreviewChecks._precise_scroll(phase=Qt.ScrollPhase.ScrollBegin)
        )
        for _ in range(6):
            canvas._trackpad_pan(QtPreviewChecks._precise_scroll(-40, 0))
            time.sleep(0.02)
            self.drain()
        self.assertGreater(canvas._center_lon, lon_before)
        # Arm content after the stream: interim paints with empty charts
        # forget renderer demand, so only the final trigger is asserted.
        renderer._states.setdefault(canvas, RenderState())
        renderer._last_content[canvas] = (((source,),), palette, 1)
        canvas._trackpad_last_speculation = 0.0
        canvas._trackpad_pan(QtPreviewChecks._precise_scroll(-40, 0))
        job = renderer._speculative.get(canvas)
        if job is None and renderer._speculative_active is not None:
            self.assertIs(renderer._speculative_active[0], canvas)
            job = renderer._speculative_active[1]
        self.assertIsNotNone(job)
        # Ahead means further along the direction the view already traveled.
        self.assertGreater(job.viewport._center_lon, canvas._center_lon)
        renderer.cancel_speculation()

    def test_pan_prediction_uses_measured_preparation_time_without_changing_camera(
        self,
    ):
        canvas = self.window.map
        canvas._adaptive_pan_prefetch = True
        renderer = canvas.wind
        charts = ((_DummySource(canvas.time_control.time),),)
        renderer._last_content[canvas] = (charts, (), 1)
        key = renderer._pan_preparation_key(charts, 1, canvas._zoom)
        renderer._pan_preparation.observe(key, 0.4)  # 0.6s including headroom.
        camera = (canvas._center_lat, canvas._center_lon, canvas._zoom)
        expected = canvas.convert_screen_to_geographic(
            canvas.width() / 2 - 240, canvas.height() / 2
        ).get_degrees()
        with patch.object(renderer, "prefetch_at", return_value=True) as prefetch:
            self.assertTrue(canvas._prefetch_along(400, 0))
            prefetch.assert_called_once()
            actual = prefetch.call_args.args
            self.assertIs(actual[0], canvas)
            self.assertAlmostEqual(actual[1], expected[0])
            self.assertAlmostEqual(actual[2], expected[1])
            self.assertEqual(
                camera, (canvas._center_lat, canvas._center_lon, canvas._zoom)
            )
        renderer._pan_preparation.observe("other", 0.1)
        renderer._pan_preparation.observe(key, 0.1)
        with patch.object(renderer, "prefetch_at") as prefetch:
            self.assertFalse(canvas._prefetch_along(400, 0))
            prefetch.assert_not_called()

    def test_stale_palette_and_forgotten_work_cannot_publish(self):
        canvas = self.window.map
        chart = self.simple_chart()
        pending = []
        renderer = BaseChartRenderer(lambda *args: pending.append(args))
        self.addCleanup(lambda: renderer.forget(canvas))
        surface, cr = self.surface(canvas)
        renderer.draw_base(canvas, cr, [chart], "dark")
        self.wait(lambda: bool(pending))
        first = renderer._states[canvas].requested
        # An old completed result must not be published with a new palette.
        renderer.draw_base(canvas, cr, [chart], "cm93")
        self.assertTrue(first.cancel.is_set())
        callback, *args = pending.pop(0)
        callback(*args)
        self.assertIsNone(renderer._states[canvas].frame)
        renderer.forget(canvas)
        self.wait(lambda: bool(pending))
        callback, *args = pending.pop(0)
        callback(*args)
        self.assertNotIn(canvas, renderer._states)
        self.assertFalse(renderer._running)

    def test_source_change_never_paints_previous_source_pixels(self):
        canvas = self.window.map
        chart = self.simple_chart()
        renderer = BaseChartRenderer(self.controller.scheduler.submit)
        self.addCleanup(lambda: renderer.forget(canvas))
        surface, cr = self.surface(canvas)
        renderer.draw_base(canvas, cr, [chart], "dark")
        self.wait(lambda: renderer._states[canvas].frame is not None)
        other = self.simple_chart()
        self.assertEqual(chart.path, other.path)
        self.assertNotEqual(describe_sources([chart]), describe_sources([other]))
        blank, target = self.surface(canvas)
        renderer.draw_base(canvas, target, [other], "dark")
        blank.flush()
        self.assertEqual(
            bytes(blank.get_data()), bytes(blank.get_stride() * blank.get_height())
        )
        renderer.forget(canvas)

    def test_unsupported_and_oversized_work_keep_synchronous_path(self):
        from types import SimpleNamespace

        canvas = self.window.map
        chart = self.simple_chart()
        renderer = canvas._base_chart_renderer
        surface, cr = self.surface(canvas)
        self.assertIsNone(describe_sources([SimpleNamespace(enabled=True)]))
        self.assertFalse(
            renderer.draw_base(canvas, cr, [SimpleNamespace(enabled=True)], "dark")
        )
        renderer.MAX_SURFACE_BYTES = 1
        self.controller.charts.charts = [chart]
        with patch.object(self.controller.charts, "_draw_base") as synchronous:
            self.controller.charts.do_draw(canvas, cr)
        synchronous.assert_called_once()
        self.assertNotIn(canvas, renderer._states)

    def test_mixed_sources_preserve_order_and_only_worker_the_audited_prefix(self):
        from types import SimpleNamespace

        canvas = self.window.map
        chart = self.simple_chart()
        unsupported = SimpleNamespace(enabled=True)
        charts = [chart, unsupported, chart]
        pending = []
        renderer = BaseChartRenderer(lambda *args: pending.append(args))
        self.addCleanup(lambda: renderer.forget(canvas))
        surface, cr = self.surface(canvas)
        calls = []

        def fallback(view, context, suffix):
            self.assertIs(view, canvas)
            self.assertIs(context, cr)
            self.assertEqual(suffix, charts[1:])
            calls.append("synchronous suffix")

        self.assertTrue(
            renderer.draw_base(canvas, cr, charts, "dark", fallback=fallback)
        )
        self.wait(lambda: bool(pending))
        callback, *args = pending.pop(0)
        callback(*args)
        frame = renderer._states[canvas].frame
        self.assertEqual(len(frame[0].charts), 1)
        calls.clear()
        with patch.object(
            renderer, "_paint", side_effect=lambda *args: calls.append("worker prefix")
        ):
            renderer.draw_base(canvas, cr, charts, "dark", fallback=fallback)
        self.assertEqual(calls, ["worker prefix", "synchronous suffix"])
        self.assertEqual(renderer.diagnostics["synchronous_source_count"], 2)

    def test_worker_error_falls_back_without_repeated_jobs(self):
        canvas = self.window.map
        chart = self.simple_chart()
        self.controller.charts.charts = [chart]
        renderer = canvas._base_chart_renderer
        with patch.object(
            renderer, "_render", side_effect=ValueError("test open failure")
        ) as render:
            canvas._base_image()
            self.wait(lambda: renderer._failed_key is not None)
            with patch.object(
                self.controller.charts,
                "_draw_base",
                wraps=self.controller.charts._draw_base,
            ) as synchronous:
                canvas._base_image()
            synchronous.assert_called_once()
            self.assertEqual(render.call_count, 1)
