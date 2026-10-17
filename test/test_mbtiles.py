"""Read-only MBTiles import, tile orientation, sparse fallback and bounded I/O."""

import io
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import cairo

from gweatherrouting.core.mapviewport import MapViewport
from gweatherrouting.core.mbtiles import MBTilesInfo, open_archive, read_info
from gweatherrouting.rendering.mbtilesrenderer import MBTilesRenderer
from gweatherrouting.rendering.overlayrenderer import RenderJob
from test.map_helpers import MapCanvas, QueuedScheduler


def png(color, size=32):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    cr.set_source_rgb(*color)
    cr.paint()
    buffer = io.BytesIO()
    surface.write_to_png(buffer)
    return buffer.getvalue()


def make_mbtiles(path, tiles=None, scheme="tms", metadata=None):
    if tiles is None:
        tiles = {
            (1, 0, 0): (1, 0, 0),
            (1, 1, 0): (0, 1, 0),
            (1, 0, 1): (0, 0, 1),
            (1, 1, 1): (1, 1, 0),
        }
    with sqlite3.connect(str(path)) as db:
        db.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        db.execute(
            "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB, PRIMARY KEY (zoom_level, tile_column, tile_row))"
        )
        values = {
            "name": "Offline sample",
            "format": "png",
            "scheme": scheme,
            "bounds": "-180,-85.05112878,180,85.05112878",
        }
        values.update(metadata or {})
        db.executemany("INSERT INTO metadata VALUES (?,?)", values.items())
        for (z, x, y), color in tiles.items():
            row = 2**z - 1 - y if scheme == "tms" else y
            db.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, x, row, png(color)))
    return str(path)


class TestMBTilesMetadata(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "coast #1.mbtiles"

    def test_metadata_roundtrip_and_read_only_connection(self):
        make_mbtiles(self.path)
        info = read_info(self.path)
        self.assertEqual(info.zooms, (1,))
        self.assertEqual(info.scheme, "tms")
        self.assertEqual(info, MBTilesInfo.from_metadata(self.path, info.metadata()))
        before = self.path.stat().st_mtime_ns
        with open_archive(self.path) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM tiles")
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_missing_path_does_not_create_database(self):
        with self.assertRaises(ValueError):
            read_info(self.path)
        self.assertFalse(self.path.exists())

    def test_vector_archive_rejected_clearly(self):
        make_mbtiles(self.path, metadata={"format": "pbf"})
        with self.assertRaisesRegex(ValueError, "PNG raster"):
            read_info(self.path)

    def test_unindexed_tiles_rejected_without_modifying_archive(self):
        make_mbtiles(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("ALTER TABLE tiles RENAME TO indexed_tiles")
            db.execute("CREATE TABLE tiles AS SELECT * FROM indexed_tiles")
            db.execute("DROP TABLE indexed_tiles")
        before = self.path.stat().st_mtime_ns
        with self.assertRaisesRegex(ValueError, "tile index"):
            read_info(self.path)
        self.assertEqual(before, self.path.stat().st_mtime_ns)

    def test_empty_database_rejected(self):
        make_mbtiles(self.path, tiles={})
        with self.assertRaisesRegex(ValueError, "no tiles"):
            read_info(self.path)

    def test_bounds_derived_when_metadata_missing(self):
        make_mbtiles(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM metadata WHERE name='bounds'")
        info = read_info(self.path)
        self.assertAlmostEqual(info.bounds[0], -180)
        self.assertAlmostEqual(info.bounds[1], -85.05112878)
        self.assertAlmostEqual(info.bounds[2], 180)
        self.assertAlmostEqual(info.bounds[3], 85.05112878)

    def test_invalid_bounds_and_scheme_rejected(self):
        make_mbtiles(self.path, metadata={"bounds": "nan,0,180,85"})
        with self.assertRaisesRegex(ValueError, "bounds"):
            read_info(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE metadata SET value='unknown' WHERE name='scheme'")
        with self.assertRaisesRegex(ValueError, "scheme"):
            read_info(self.path)


class TestMBTilesPixels(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "map.mbtiles"
        self.canvas = MapCanvas()
        self.canvas._center_lat = self.canvas._center_lon = 0
        self.canvas._zoom = 1
        self.renderer = MBTilesRenderer()

    def render(self, info, factor=1):
        viewport = MapViewport(self.canvas)
        job = RenderJob((), viewport, ((info,),), factor, "cm93")
        return self.renderer._render(job)

    @staticmethod
    def pixel(surface, x, y):
        surface.flush()
        offset = y * surface.get_stride() + x * 4
        return tuple(bytes(surface.get_data())[offset : offset + 4])

    def test_tms_orientation_fractional_zoom_and_retina(self):
        make_mbtiles(self.path)
        info = read_info(self.path)
        for zoom in (1, 1.4, 2):
            self.canvas._zoom = zoom
            for factor in (1, 2):
                with self.subTest(zoom=zoom, factor=factor):
                    image = self.render(info, factor)
                    self.assertEqual(
                        self.pixel(image, 100 * factor, 50 * factor), (0, 0, 255, 255)
                    )
                    self.assertEqual(
                        self.pixel(image, 300 * factor, 50 * factor), (0, 255, 0, 255)
                    )
                    self.assertEqual(
                        self.pixel(image, 100 * factor, 250 * factor), (255, 0, 0, 255)
                    )
                    self.assertEqual(
                        self.pixel(image, 300 * factor, 250 * factor),
                        (0, 255, 255, 255),
                    )

    def test_xyz_scheme(self):
        make_mbtiles(self.path, scheme="xyz")
        image = self.render(read_info(self.path))
        self.assertEqual(self.pixel(image, 100, 50), (0, 0, 255, 255))
        self.assertEqual(self.pixel(image, 100, 250), (255, 0, 0, 255))

    def test_archive_bounds_clip_partial_tiles(self):
        make_mbtiles(self.path, metadata={"bounds": "-20,-20,20,20"})
        image = self.render(read_info(self.path))
        self.assertEqual(self.pixel(image, 100, 50), (0, 0, 0, 0))
        self.assertEqual(self.pixel(image, 205, 145), (0, 255, 0, 255))

    def test_antimeridian_coverage_wraps_tiles(self):
        make_mbtiles(self.path, metadata={"bounds": "170,-80,-170,80"})
        self.canvas._center_lon, self.canvas._zoom = 179, 5
        image = self.render(read_info(self.path))
        self.assertEqual(self.pixel(image, 100, 50), (0, 255, 0, 255))
        self.assertEqual(self.pixel(image, 300, 50), (0, 0, 255, 255))

    def test_database_connection_and_decode_run_off_ui_thread(self):
        make_mbtiles(self.path)
        info = read_info(self.path)
        scheduler = QueuedScheduler()
        renderer = MBTilesRenderer(scheduler)
        self.addCleanup(renderer.forget, self.canvas)
        threads = []

        def opened(path):
            threads.append(threading.get_ident())
            return open_archive(path)

        cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300))
        with patch(
            "gweatherrouting.rendering.mbtilesrenderer.open_archive", side_effect=opened
        ):
            renderer.draw(self.canvas, cr, ((info,),), "cm93")
            deadline = time.monotonic() + 3
            while renderer._running and time.monotonic() < deadline:
                scheduler.drain()
                time.sleep(0.005)
        self.assertFalse(renderer._running)
        self.assertTrue(threads)
        self.assertTrue(all(thread != threading.get_ident() for thread in threads))
        self.assertIsNotNone(renderer._states[self.canvas].frame)

    def test_corrupt_child_uses_valid_parent(self):
        make_mbtiles(self.path, tiles={(0, 0, 0): (0, 0, 1), (1, 0, 0): (1, 0, 0)})
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE tiles SET tile_data=? WHERE zoom_level=1", (b"bad PNG",))
        with self.assertLogs("gweatherrouting", level="WARNING"):
            image = self.render(read_info(self.path))
        self.assertEqual(self.pixel(image, 100, 50), (255, 0, 0, 255))

    def test_sparse_children_use_parent_and_holes_stay_transparent(self):
        make_mbtiles(self.path, tiles={(0, 0, 0): (0, 0, 1), (1, 0, 0): (1, 0, 0)})
        image = self.render(read_info(self.path))
        self.assertEqual(self.pixel(image, 100, 50), (0, 0, 255, 255))
        self.assertEqual(self.pixel(image, 300, 50), (255, 0, 0, 255))
        self.path.unlink()
        make_mbtiles(self.path, tiles={(1, 0, 0): (1, 0, 0)})
        self.renderer = MBTilesRenderer()
        image = self.render(read_info(self.path))
        self.assertEqual(self.pixel(image, 300, 50), (0, 0, 0, 0))

    def test_outside_archive_does_not_open_sqlite_in_render(self):
        make_mbtiles(self.path, metadata={"bounds": "-129,48,-123,51"})
        self.canvas._zoom = 10
        with patch("gweatherrouting.rendering.mbtilesrenderer.open_archive") as opened:
            image = self.render(read_info(self.path))
            opened.assert_not_called()
        self.assertEqual(self.pixel(image, 200, 150), (0, 0, 0, 0))

    def test_tile_enumeration_is_bounded_at_low_display_zoom(self):
        make_mbtiles(self.path, tiles={(16, 0, 0): (1, 0, 0)})
        info = read_info(self.path)
        with patch("gweatherrouting.rendering.mbtilesrenderer.open_archive") as opened:
            self.render(info)
            opened.assert_not_called()

    def test_cache_is_bounded_and_reuses_decoded_tiles(self):
        make_mbtiles(self.path)
        info = read_info(self.path)
        first = self.render(info)
        with patch(
            "gweatherrouting.rendering.mbtilesrenderer.png_size",
            side_effect=AssertionError("Decoded cached tile"),
        ):
            second = self.render(info)
        self.assertEqual(bytes(first.get_data()), bytes(second.get_data()))
        self.renderer.CACHE_ENTRIES = 2
        self.renderer.CACHE_BYTES = 8192
        self.renderer._tiles.clear()
        self.renderer._tile_bytes = 0
        self.render(info)
        self.assertLessEqual(len(self.renderer._tiles), 2)
        self.assertLessEqual(self.renderer._tile_bytes, 8192)

    def test_cancelled_job_does_not_open_database(self):
        make_mbtiles(self.path)
        info = read_info(self.path)
        job = RenderJob((), MapViewport(self.canvas), ((info,),), 1, "cm93")
        job.cancel.set()
        with patch("gweatherrouting.rendering.mbtilesrenderer.open_archive") as opened:
            self.assertIsNone(self.renderer._render(job))
            opened.assert_not_called()
