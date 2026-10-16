"""Qt-only base portrayal worker checks, run in an isolated process."""

import json
import threading
from unittest.mock import patch

import cairo
from osgeo import ogr
from PySide6.QtCore import QTimer

from gweatherrouting.core.mapviewport import MapViewport
from gweatherrouting.qt.basecharts import BaseChartRenderer, describe_sources
from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart
from gweatherrouting.rendering.gshhs import GSHHSVectorChart
from gweatherrouting.rendering.overlayrenderer import MARGIN, RenderJob
from gweatherrouting.rendering.vectordrawer.simplechartdrawer import SimpleChartDrawer
from test.qt_gpu_checks import Fixture


class BaseChartChecks(Fixture):
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
