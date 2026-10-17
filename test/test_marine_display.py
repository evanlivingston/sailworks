"""Marine portrayal directions, UTC, masking, and source invalidation."""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cairo
import numpy as np

from gweatherrouting.core.marine.manager import MarineManager
from gweatherrouting.core.marine.models import Capability, Sample, Series
from gweatherrouting.core.timecontrol import TimeControl
from gweatherrouting.rendering.marinemaplayer import (
    MarineMapLayer,
    MarineRenderer,
    MarineSources,
    arrow,
    chart_instant,
    flow,
    current_style,
)
from gweatherrouting.core.mapviewport import MapPoint, MapViewport
from gweatherrouting.rendering.overlayrenderer import RenderState
from test.map_helpers import MapCanvas
from test.test_marine import START, dataset
from test.test_marine_fields import field


class TestMarineDisplay(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.manager = MarineManager(temporary.name)
        self.clock = TimeControl()
        self.clock.set_time(START.astimezone().replace(tzinfo=None))

    def job(self, fields=()):
        canvas = MapCanvas()
        canvas._center_lat, canvas._center_lon, canvas._zoom = 49.5, -125.5, 9
        sources = MarineSources(
            fields,
            self.manager.snapshot(Capability.WATER_LEVEL),
            self.manager.snapshot(Capability.STATION_CURRENT),
        )
        return SimpleNamespace(
            charts=((sources,),),
            palette=START,
            viewport=MapViewport(canvas, 256),
            factor=1,
            cancel=Event(),
        )

    def test_land_mask_repairs_invalid_geometry_before_clipping(self):
        from pathlib import Path
        from osgeo import ogr
        from gweatherrouting.rendering.marinelandmask import MarineLandMask
        from gweatherrouting.core.geo.shoreline import ShorelineGeometryCache

        geometry = ogr.CreateGeometryFromWkt(
            "POLYGON ((-125.6 49.4, -125.4 49.6, -125.6 49.6, -125.4 49.4, -125.6 49.4))"
        )
        bounds = ogr.CreateGeometryFromWkt(
            "POLYGON ((-126 49, -125 49, -125 50, -126 50, -126 49))"
        )
        cache = ShorelineGeometryCache()
        clipped = cache.clip("bowtie", geometry, bounds)
        self.assertTrue(clipped.IsValid())
        self.assertGreater(clipped.GetArea(), 0)
        self.assertEqual(
            cache.clip("bowtie", geometry, bounds).ExportToWkt(), clipped.ExportToWkt()
        )
        memory = ogr.GetDriverByName("Memory").CreateDataSource("")
        layer = memory.CreateLayer("land", geom_type=ogr.wkbPolygon)
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(geometry)
        layer.CreateFeature(feature)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gshhs/GSHHS_shp/f/GSHHS_f_L1.shp"
            path.parent.mkdir(parents=True)
            path.touch()
            mask = MarineLandMask(directory)
            view = self.job().viewport
            with patch(
                "gweatherrouting.rendering.marinelandmask.ogr.Open",
                return_value=memory,
            ):
                surface = mask.surface(view)
                self.assertTrue(any(bytes(surface.get_data())))
                self.assertIs(mask.surface(view), surface)

    def test_arrow_points_with_flow_in_screen_coordinates(self):
        for east, north, bearing in ((1, 0, 90), (0, 1, 0), (-1, 0, 270), (0, -1, 180)):
            self.assertAlmostEqual(flow(east, north)[1], bearing)
            cr = Mock()
            arrow(cr, 50, 50, east, north)
            self.assertAlmostEqual(
                cr.rotate.call_args.args[0], bearing * 3.141592653589793 / 180
            )
            # In arrow-local coordinates the tip is above (negative Y) the tail.
            self.assertLess(
                cr.move_to.call_args_list[0].args[1],
                max(call.args[1] for call in cr.line_to.call_args_list),
            )
            cr.fill_preserve.assert_called_once()

    def test_intensity_uses_a_fixed_green_yellow_orange_red_scale(self):
        lengths = [current_style(speed)[0] for speed in (0.25, 1, 2, 3, 5)]
        self.assertEqual(lengths, sorted(set(lengths)))
        self.assertEqual(current_style(8), current_style(5))
        green = current_style(0)[1]
        yellow = current_style(1)[1]
        orange = current_style(2)[1]
        red = current_style(3)[1]
        self.assertGreater(green[1], green[0])
        self.assertGreater(yellow[1], orange[1])
        self.assertGreater(orange[1], red[1])
        self.assertGreater(red[0], red[1])

    def test_local_aware_clock_converts_to_utc_without_relabelling(self):
        pacific = timezone(timedelta(hours=-7))
        local = datetime(2026, 10, 10, 17, tzinfo=pacific)
        self.assertEqual(chart_instant(local), START)
        # Naive times follow the same local-time rule as TimeControl.timestamp().
        wall = local.replace(tzinfo=None)
        self.assertEqual(chart_instant(wall).timestamp(), wall.timestamp())

    def test_masked_or_expired_fields_draw_no_arrows_or_false_calm(self):
        for source, instant in (
            (field(masked=True), START),
            (field(), START + timedelta(days=1)),
        ):
            job = self.job((source,))
            job.palette = instant
            surface = MarineRenderer()._render(job)
            self.assertFalse(any(surface.get_data()))

    def test_renderer_draws_model_and_station_currents_and_honours_cancel(self):
        value = dataset()
        self.manager.add(value)
        self.manager.enable(value.id)
        job = self.job((field(),))
        with patch("gweatherrouting.rendering.marinemaplayer.arrow") as draw:
            surface = MarineRenderer()._render(job)
        self.assertIsInstance(surface, cairo.ImageSurface)
        self.assertTrue(any(not call.kwargs for call in draw.call_args_list))
        self.assertTrue(any(call.kwargs.get("station") for call in draw.call_args_list))
        job.cancel.set()
        self.assertIsNone(MarineRenderer()._render(job))

    def test_source_changes_refresh_display_and_retire_old_snapshots(self):
        layer = MarineMapLayer(self.manager, self.clock)
        old = layer.sources
        source = field()
        self.manager.fields.add(source)
        self.assertEqual(layer.sources.fields, (source,))
        self.assertEqual(old.fields, ())
        self.assertIn("Test currents", layer.cursor_text(49.5, -125.5))
        self.manager.fields.enable(source.id, False)
        self.assertEqual(layer.sources.fields, ())
        self.assertEqual(layer.cursor_text(49.5, -125.5), "Current: no valid data")
        value = dataset()
        self.manager.add(value)
        self.manager.enable(value.id)
        self.assertEqual(layer.sources.currents.datasets, (value,))
        self.manager.remove(value.id)
        self.assertEqual(layer.sources.currents.datasets, ())
        layer.set_visible(False)
        self.assertEqual(layer.cursor_text(49.5, -125.5), "")

    def test_tide_height_stays_at_station_and_identifies_source_datum(self):
        original = dataset()
        value = replace(
            original,
            station=replace(original.station, latitude=49.5, longitude=-125.5),
            series=(Series("water_level", "m", (Sample(START, 2.5, "2"),)),),
        )
        self.manager.add(value)
        self.manager.enable(value.id)
        job = self.job()
        with (
            patch("gweatherrouting.rendering.marinemaplayer.label") as draw_label,
            patch("gweatherrouting.rendering.marinemaplayer.arrow") as draw_arrow,
        ):
            MarineRenderer()._render(job)
        draw_label.assert_called_once()
        x, y = job.viewport.convert_geographic_to_screen(MapPoint(49.5, -125.5))
        self.assertEqual(draw_label.call_args.args[1:3], (x + 8, y - 8))
        self.assertIn("2.50 m (source datum)", draw_label.call_args.args[3])
        # A station height alone never creates a spatial-current field.
        draw_arrow.assert_not_called()

    def test_renderer_keeps_frame_during_time_updates_but_not_source_or_expiry(self):
        renderer = MarineRenderer()
        job = self.job((field(),))
        job.key = (START, 1, (job.charts[0][0],))
        self.assertTrue(renderer._can_preview(job, job.key))
        self.assertTrue(
            renderer._can_preview(job, (START + timedelta(minutes=15), *job.key[1:]))
        )
        self.assertFalse(
            renderer._can_preview(job, (START + timedelta(days=1), *job.key[1:]))
        )
        self.assertFalse(
            renderer._can_preview(job, (START, 1, (self.job().charts[0][0],)))
        )

    def test_playback_paints_old_frame_until_complete_replacement_is_ready(self):
        renderer = MarineRenderer()
        job = self.job((field(),))
        job.key = (START, 1, (job.charts[0][0],))
        canvas = MapCanvas()
        canvas._center_lat, canvas._center_lon, canvas._zoom = 49.5, -125.5, 9
        previous = renderer._render(job)
        renderer._states[canvas] = RenderState(key=job.key, frame=(job, previous))
        renderer._start_next = Mock()
        target = START + timedelta(minutes=15)
        cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300))
        with patch.object(renderer, "_paint") as paint:
            renderer.draw(canvas, cr, job.charts, target)
            self.assertIs(paint.call_args.args[2], job)
            replacement = renderer._pending.pop(canvas)
            result = renderer._render(replacement)
            renderer._completed(canvas, replacement, result, None)
            renderer.draw(canvas, cr, job.charts, target)
            self.assertIs(paint.call_args.args[2], replacement)
            paint.reset_mock()
            renderer.draw(canvas, cr, job.charts, START + timedelta(days=1))
            paint.assert_not_called()


class TestMarineShoreline(unittest.TestCase):
    def test_clips_land_and_arrow_tips_preserves_holes_and_alignment(self):
        from osgeo import ogr
        from gweatherrouting.rendering.marinelandmask import MarineLandMask
        from gweatherrouting.rendering.overlayrenderer import paint_viewport

        canvas = MapCanvas()
        canvas._center_lat, canvas._center_lon, canvas._zoom = 0, 0, 12
        view = MapViewport(canvas, 256)
        # A coastline through the centre and a water hole on its landward side.
        geometry = ogr.CreateGeometryFromWkt(
            "POLYGON ((-1 -1, 0 -1, 0 1, -1 1, -1 -1), "
            "(-0.02 -0.02, -0.02 0.02, -0.01 0.02, -0.01 -0.02, -0.02 -0.02))"
        )
        feature = dict(
            geometry=json.loads(geometry.ExportToJson()),
            properties=dict(bbox=[[-1, -1], [1, 0]]),
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("gweatherrouting.rendering.marinelandmask.COUNTRY_SHAPES", [feature]),
        ):
            mask = MarineLandMask(directory)
            land = mask.surface(view)
            self.assertIs(mask.surface(view), land)
            center = view.width // 2, view.height // 2
            self.assertTrue(mask.contains(land, center[0] - 5, center[1]))
            self.assertFalse(mask.contains(land, center[0] + 5, center[1]))
            hole = view.convert_geographic_to_screen_batch(
                np.array([0]), np.array([-0.015])
            )
            self.assertFalse(mask.contains(land, hole[0][0], hole[1][0]))
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, view.width, view.height)
            cr = cairo.Context(surface)
            # Anchor in water, pointing west so the head overlaps land.
            arrow(cr, center[0] + 4, center[1], -2, 0)
            mask.clear(cr, land)
            anchor = view.convert_screen_to_geographic(center[0] + 4, center[1])
            for zoom, longitude in ((12, 0), (12, 0.002), (13, 0.002)):
                canvas._zoom, canvas._center_lon = zoom, longitude
                output = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300)
                paint_viewport(canvas, cairo.Context(output), view, surface)
                output.flush()
                data = output.get_data()
                x, y = canvas.convert_geographic_to_screen(anchor)
                offset = int(y) * output.get_stride() + int(x) * 4
                self.assertNotEqual(bytes(data[offset : offset + 4]), b"\0\0\0\0")
                for x in range(400):
                    _, lon = canvas.convert_screen_to_geographic(x, 150).get_degrees()
                    if lon < -0.0005:
                        self.assertEqual(
                            bytes(
                                data[
                                    150 * output.get_stride()
                                    + x * 4 : 150 * output.get_stride()
                                    + x * 4
                                    + 4
                                ]
                            ),
                            b"\0\0\0\0",
                        )
