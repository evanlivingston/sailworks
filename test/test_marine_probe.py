"""Offline research-probe checks using authored samples, never CHS payloads."""

import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROBE_PATH = Path(__file__).resolve().parents[1] / "tools" / "marine_probe.py"
SPEC = importlib.util.spec_from_file_location("marine_probe", PROBE_PATH)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class TestMarineProbe(unittest.TestCase):
    def test_direction_audit_matches_extrema_without_remapping_bearings(self):
        directions = [
            {"eventDate": "2026-10-11T04:00:00Z", "value": 0},
            {"eventDate": "2026-10-11T10:00:00Z", "value": 180},
        ]
        events = [
            {"eventDate": "2026-10-11T03:58:00Z", "qualifier": "EXTREMA_EBB"},
            {"eventDate": "2026-10-11T10:06:00Z", "qualifier": "EXTREMA_FLOOD"},
            {"eventDate": "2026-10-11T06:58:00Z", "qualifier": "SLACK"},
        ]
        result = probe.audit_directions(directions, events)
        self.assertEqual(result["distinct_bearings"], [0, 180])
        self.assertEqual(
            [row["bearing_degrees"] for row in result["extrema"]], [0, 180]
        )
        self.assertEqual(
            [row["offset_seconds"] for row in result["extrema"]], [120, 360]
        )

    def test_direction_audit_rejects_stale_and_invalid_directions(self):
        directions = [
            {"eventDate": "2026-10-11T00:00:00Z", "value": 355},
            {"eventDate": "2026-10-11T01:00:00Z", "value": None},
            {"eventDate": "2026-10-11T01:00:00Z", "value": -1},
        ]
        events = [{"eventDate": "2026-10-11T01:00:00Z", "qualifier": "EXTREMA_FLOOD"}]
        for rows in (directions, []):
            result = probe.audit_directions(rows, events)
            self.assertEqual(result["extrema"][0]["status"], "no_nearby_direction")
            self.assertIsNone(result["extrema"][0]["bearing_degrees"])
        self.assertEqual(
            probe.audit_directions(directions, events)["invalid_direction_count"], 2
        )

    def test_summaries_preserve_quality_and_do_not_count_missing_as_zero(self):
        rows = [
            {"eventDate": "2026-10-11T00:00:00Z", "value": None, "qcFlagCode": "2"},
            {"eventDate": "2026-10-11T00:15:00Z", "value": 3, "qcFlagCode": "3"},
            {"eventDate": "2026-10-11T00:30:00Z", "value": float("nan")},
        ]
        result = probe.summarize_series(rows)
        self.assertEqual(result["minimum"], 3)
        self.assertEqual(result["missing_or_invalid_values"], 2)
        self.assertEqual(result["interval_seconds"], [900])
        self.assertIn("3", result["qc_flags"])

    def test_empty_events_and_duplicate_timestamps_are_visible(self):
        self.assertIsNone(probe.summarize_series([])["minimum"])
        row = {"eventDate": "2026-10-11T00:00:00Z", "value": 0, "qualifier": "SLACK"}
        result = probe.summarize_series([row, row])
        self.assertFalse(result["strictly_increasing"])
        self.assertEqual(result["qualifiers"], ["SLACK"])

    def test_timezone_normalization_requires_offset(self):
        self.assertEqual(
            probe.stamp(probe.utc("2026-10-10T17:00:00-07:00")),
            "2026-10-11T00:00:00Z",
        )
        with self.assertRaises(ValueError):
            probe.utc("2026-10-11T00:00:00")

    def test_cache_replays_without_network_and_detects_corruption(self):
        payload = b'{"sample": 1}'
        response = io.BytesIO(payload)
        response.headers = {"Content-Type": "application/json"}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "urlopen", return_value=response):
                cache = probe.Cache(directory)
                path = cache.get("https://example.invalid/sample")
            with patch.object(probe, "urlopen", side_effect=AssertionError("network")):
                replay = probe.Cache(directory, offline=True)
                self.assertEqual(
                    replay.get("https://example.invalid/sample").read_bytes(), payload
                )
                with self.assertRaisesRegex(ValueError, "unavailable offline"):
                    replay.get("https://example.invalid/missing")
                path.write_bytes(b"corrupt")
                with self.assertRaisesRegex(ValueError, "integrity"):
                    replay.get("https://example.invalid/sample")

    def test_oversized_response_is_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(probe, "MAX_BYTES", 8),
                patch.object(
                    probe, "urlopen", return_value=io.BytesIO(b"too much data")
                ),
            ):
                with self.assertRaisesRegex(ValueError, "limit"):
                    probe.Cache(directory).get("https://example.invalid/large")
            self.assertEqual(list((Path(directory) / "responses").iterdir()), [])

    def test_listing_rejects_remote_and_parent_links(self):
        html = '<a href="12/">ok</a><a href="https://other/18/">bad</a>'
        html += '<a href="../18/">bad</a><a href="12/">duplicate</a>'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "listing"
            path.write_text(html)
            with patch.object(probe.Cache, "get", return_value=path):
                result = probe.listing(
                    probe.Cache(directory), "unused", r"(?:00|06|12|18)/"
                )
            self.assertEqual(result, ["12/"])


class TestNetCDFInspection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import numpy as np
            from osgeo import gdal
        except ImportError:
            raise unittest.SkipTest(
                "Run with the application environment for GDAL/NumPy"
            )
        cls.np = np
        cls.gdal = gdal
        gdal.UseExceptions()

    def test_coordinate_wrap_bounds_and_descending_latitudes(self):
        self.assertEqual(probe.nearest_cell([50, 49], [234, 235], 49, -125), (1, 1))
        self.assertIsNone(probe.nearest_cell([49, 50], [234, 235], 51, -125))

    def test_real_netcdf_masks_time_and_nearest_cells(self):
        gdal, np = self.gdal, self.np
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.nc"
            dataset = gdal.GetDriverByName("netCDF").CreateMultiDimensional(str(path))
            root = dataset.GetRootGroup()
            t = root.CreateDimension("time", "TEMPORAL", "", 1)
            y = root.CreateDimension("latitude", "HORIZONTAL_Y", "NORTH", 2)
            x = root.CreateDimension("longitude", "HORIZONTAL_X", "EAST", 2)
            entries = [
                ("latitude", [y], [49, 50], "degrees_north"),
                ("longitude", [x], [234, 235], "degrees_east"),
                ("time", [t], [3600], "seconds since 2026-10-11 00:00:00"),
                ("vozocrtx", [t, y, x], [[[1e20, 2], [3, 4]]], "m s-1"),
                ("vomecrty", [t, y, x], [[[1e20, -2], [-3, -4]]], "m s-1"),
            ]
            for name, dimensions, values, unit in entries:
                array = root.CreateMDArray(
                    name, dimensions, gdal.ExtendedDataType.Create(gdal.GDT_Float64)
                )
                array.SetUnit(unit)
                if name in ("vozocrtx", "vomecrty"):
                    array.SetNoDataValueDouble(1e20)
                if name == "time":
                    array.CreateAttribute(
                        "calendar", [], gdal.ExtendedDataType.CreateString()
                    ).Write("gregorian")
                array.Write(np.array(values, dtype=float))
            array = root = dataset = None
            with patch.object(probe, "POINTS", {"land": (49, -126), "sea": (50, -125)}):
                result = probe.inspect_netcdf(path, "X")
                north = probe.inspect_netcdf(path, "Y")
            self.assertEqual(result["valid_time"], "2026-10-11T01:00:00Z")
            self.assertEqual(result["valid_cells"], 3)
            self.assertEqual(result["samples"]["land"]["status"], "masked_or_invalid")
            self.assertIsNone(result["samples"]["land"]["value_m_s"])
            self.assertEqual(result["samples"]["sea"]["value_m_s"], 4)
            self.assertEqual(north["samples"]["sea"]["value_m_s"], -4)
            json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
