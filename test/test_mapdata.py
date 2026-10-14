"""Map setup keeps incomplete downloads out of the live chart directory."""

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from threading import Event
from unittest.mock import patch

from osgeo import ogr

from gweatherrouting.core.mapdata import (
    download_file,
    install_map_data,
    MapDownloadCancelledError,
    unpack_coastlines,
)


def coastline_archive(root):
    root = Path(root)
    shapes = root / "source" / "gshhs" / "GSHHS_shp"
    for quality in "fhilc":
        directory = shapes / quality
        directory.mkdir(parents=True)
        dataset = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(
            str(directory / f"GSHHS_{quality}_L1.shp")
        )
        layer = dataset.CreateLayer("coast", geom_type=ogr.wkbPolygon)
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(
            ogr.CreateGeometryFromWkt("POLYGON ((-1 -1, 1 -1, 1 1, -1 1, -1 -1))")
        )
        layer.CreateFeature(feature)
        feature = layer = dataset = None
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in shapes.rglob("*"):
            if path.is_file():
                archive.write(path, str(path.relative_to(root / "source")))
    return buffer.getvalue()


class Response:
    def __init__(self, body, length=None, cancel=None, error=None):
        self.body, self.cancel, self.error = body, cancel, error
        self.headers = {} if length is None else {"content-length": str(length)}
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def raise_for_status(self):
        if self.error:
            raise self.error

    def iter_content(self, chunk_size):
        for index in range(0, len(self.body), 7):
            yield self.body[index : index + 7]
            if self.cancel:
                self.cancel.set()


class TestMapData(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)

    def test_unknown_length_stream_reports_progress_and_closes_response(self):
        response = Response(b"complete download")
        progress = []
        with patch(
            "gweatherrouting.core.mapdata.requests.get", return_value=response
        ) as get:
            download_file(
                "https://fixture", self.path / "out", Event(), progress.append
            )
        get.assert_called_once_with("https://fixture", stream=True, timeout=(15, 30))
        self.assertEqual((self.path / "out").read_bytes(), response.body)
        self.assertTrue(response.closed)
        self.assertTrue(progress)
        self.assertEqual(set(progress), {None})

    def test_cancel_stream_interrupts_even_without_content_length(self):
        cancel = Event()
        response = Response(b"long enough to cancel", cancel=cancel)
        with patch("gweatherrouting.core.mapdata.requests.get", return_value=response):
            with self.assertRaises(MapDownloadCancelledError):
                download_file("fixture", self.path / "out", cancel, lambda p: None)
        self.assertTrue(response.closed)
        self.assertNotEqual((self.path / "out").read_bytes(), response.body)

    def test_http_empty_and_truncated_responses_never_install(self):
        for response in (
            Response(b"bad", error=ValueError("HTTP failed")),
            Response(b""),
            Response(b"short", length=100),
        ):
            with self.subTest(response=response):
                with patch(
                    "gweatherrouting.core.mapdata.requests.get", return_value=response
                ):
                    installed, error = install_map_data(
                        ("seamarks",), self.path, Event(), lambda *args: None
                    )
                self.assertEqual(installed, ())
                self.assertIsNotNone(error)
                self.assertFalse((self.path / "seamarks.pbf").exists())
                self.assertFalse(list(self.path.glob(".map-download-*")))

    def test_valid_coastlines_install_all_resolutions_and_replace_atomically(self):
        body = coastline_archive(self.path)
        destination = self.path / "installed"
        (destination / "gshhs").mkdir(parents=True)
        (destination / "gshhs" / "previous").write_text("incomplete installation")
        with patch(
            "gweatherrouting.core.mapdata.requests.get",
            return_value=Response(body, len(body)),
        ):
            installed, error = install_map_data(
                ("coastlines",), destination, Event(), lambda *a: None
            )
        self.assertEqual(installed, ("coastlines",))
        self.assertIsNone(error)
        self.assertFalse((destination / "gshhs" / "previous").exists())
        for quality in "fhilc":
            dataset = ogr.Open(
                str(
                    destination
                    / "gshhs"
                    / "GSHHS_shp"
                    / quality
                    / f"GSHHS_{quality}_L1.shp"
                )
            )
            self.assertEqual(dataset.GetLayer().GetFeatureCount(), 1)
        self.assertFalse(list(destination.glob(".map-download-*")))

    def test_invalid_archive_keeps_previous_installation(self):
        (self.path / "gshhs").mkdir()
        old = self.path / "gshhs" / "previous"
        old.write_bytes(b"keep me")
        with patch(
            "gweatherrouting.core.mapdata.requests.get",
            return_value=Response(b"invalid zip"),
        ):
            installed, error = install_map_data(
                ("coastlines",), self.path, Event(), lambda *a: None
            )
        self.assertEqual(installed, ())
        self.assertIsNotNone(error)
        self.assertEqual(old.read_bytes(), b"keep me")

    def test_archive_traversal_and_symlinks_are_rejected(self):
        for name, mode in (("../../outside", 0), ("link", 0o120777 << 16)):
            with self.subTest(name=name):
                archive = self.path / "unsafe.zip"
                with zipfile.ZipFile(archive, "w") as zipped:
                    member = zipfile.ZipInfo(name)
                    member.external_attr = mode
                    zipped.writestr(member, "target")
                with self.assertRaises(ValueError):
                    unpack_coastlines(archive, self.path / "unpack", Event())

    def test_batch_keeps_completed_dataset_when_next_download_fails(self):
        body = coastline_archive(self.path)
        with patch(
            "gweatherrouting.core.mapdata.requests.get",
            side_effect=[Response(body), Response(b"", error=ValueError("offline"))],
        ):
            installed, error = install_map_data(
                ("coastlines", "seamarks"),
                self.path / "installed",
                Event(),
                lambda *a: None,
            )
        self.assertEqual(installed, ("coastlines",))
        self.assertEqual(str(error), "offline")
        self.assertTrue(
            (self.path / "installed/gshhs/GSHHS_shp/h/GSHHS_h_L1.shp").exists()
        )

    def test_cancel_before_install_preserves_existing_file(self):
        old = self.path / "seamarks.pbf"
        old.write_bytes(b"old data")
        cancel = Event()

        def report(message, fraction):
            if message.startswith("Checking"):
                cancel.set()

        with patch(
            "gweatherrouting.core.mapdata.requests.get",
            return_value=Response(b"new data"),
        ):
            installed, error = install_map_data(
                ("seamarks",), self.path, cancel, report
            )
        self.assertEqual(installed, ())
        self.assertIsInstance(error, MapDownloadCancelledError)
        self.assertEqual(old.read_bytes(), b"old data")

    def test_seamarks_are_validated_with_production_osm_driver(self):
        body = (
            b'<osm version="0.6"><node id="1" lat="0" lon="0">'
            b'<tag k="seamark:type" v="buoy_lateral"/></node></osm>'
        )
        with patch(
            "gweatherrouting.core.mapdata.requests.get", return_value=Response(body)
        ):
            installed, error = install_map_data(
                ("seamarks",), self.path, Event(), lambda *a: None
            )
        self.assertEqual(installed, ("seamarks",))
        self.assertIsNone(error)
        self.assertEqual((self.path / "seamarks.pbf").read_bytes(), body)
        with patch(
            "gweatherrouting.core.mapdata.requests.get", return_value=Response(b"{}")
        ):
            installed, error = install_map_data(
                ("seamarks",), self.path, Event(), lambda *a: None
            )
        self.assertEqual(installed, ())
        self.assertIsNotNone(error)
        self.assertEqual((self.path / "seamarks.pbf").read_bytes(), body)

    def test_failed_final_rename_restores_previous_installation(self):
        body = b'<osm version="0.6"><node id="1" lat="0" lon="0"/></osm>'
        previous = self.path / "seamarks.pbf"
        previous.write_bytes(b"previous data")
        real_replace = Path.replace

        def replace(source, destination):
            if source.name == "seamarks.pbf" and source.parent != self.path:
                raise OSError("Rename failed")
            return real_replace(source, destination)

        with (
            patch(
                "gweatherrouting.core.mapdata.requests.get", return_value=Response(body)
            ),
            patch.object(Path, "replace", replace),
        ):
            installed, error = install_map_data(
                ("seamarks",), self.path, Event(), lambda *a: None
            )
        self.assertEqual(installed, ())
        self.assertEqual(str(error), "Rename failed")
        self.assertEqual(previous.read_bytes(), b"previous data")
