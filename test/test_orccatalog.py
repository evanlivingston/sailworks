"""ORC tree catalog, merged names, cache and identity lookups."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from sailworks.core import orccatalog
from test.test_polarmanager import ORC_DATA


def response(data):
    result = MagicMock()
    result.__enter__.return_value = result
    result.json.return_value = data
    return result


TREE = {
    "tree": [
        {"path": "site/data/USA/123.json"},
        {"path": "site/data/CAN/456.json"},
        {"path": "site/data/USA/123.pol"},
        {"path": "site/index.json"},
        {"path": "site/data/USA/bad name.json"},
        {"path": "site/data/AUS/789.json"},
    ]
}


class TestOrcCatalog(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "orc-polars.json"
        mock = patch.object(orccatalog, "ORC_CATALOG_FILE", self.path)
        mock.start()
        self.addCleanup(mock.stop)

    def test_tree_catalog_keeps_only_boat_json_paths(self):
        with patch.object(
            orccatalog.requests, "get", return_value=response(TREE)
        ) as get:
            self.assertEqual(
                orccatalog.orc_tree_catalog(),
                ["AUS/789", "CAN/456", "USA/123"],
            )
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["timeout"], (10, 30))

    def test_invalid_tree_payload_raises(self):
        with patch.object(orccatalog.requests, "get", return_value=response({})):
            with self.assertRaisesRegex(ValueError, "Invalid ORC catalog"):
                orccatalog.orc_tree_catalog()

    def test_merged_catalog_uses_published_names_and_blanks_others(self):
        index = [["USA/123", "Test yacht", "First 31.7"]]
        with patch.object(
            orccatalog.requests,
            "get",
            side_effect=[response(index), response(TREE)],
        ):
            rows = orccatalog.orc_catalog_merged()
        self.assertEqual(
            rows,
            [
                ("AUS/789", "", ""),
                ("CAN/456", "", ""),
                ("USA/123", "Test yacht", "First 31.7"),
            ],
        )

    def test_cache_round_trip_including_timestamp(self):
        self.assertEqual(orccatalog.load_orc_catalog(), [])
        self.assertEqual(orccatalog.orc_catalog_updated(), "")
        rows = [("USA/123", "Test yacht", "First 31.7"), ("CAN/456", "", "")]
        updated = orccatalog.save_orc_catalog(rows, "2026-11-14T10:00:00+00:00")
        self.assertEqual(updated, "2026-11-14T10:00:00+00:00")
        self.assertEqual(orccatalog.load_orc_catalog(), rows)
        self.assertEqual(
            orccatalog.orc_catalog_updated(), "2026-11-14T10:00:00+00:00"
        )
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["rows"], [list(row) for row in rows])

    def test_cache_tolerates_missing_and_malformed_files(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(orccatalog.load_orc_catalog(), [])
        self.assertEqual(orccatalog.orc_catalog_updated(), "")
        self.path.write_text(json.dumps({"rows": [["bad"], 3]}), encoding="utf-8")
        self.assertEqual(orccatalog.load_orc_catalog(), [])

    def test_boat_identity_reads_fields(self):
        with patch.object(
            orccatalog.requests, "get", return_value=response(ORC_DATA)
        ) as get:
            self.assertEqual(
                orccatalog.orc_boat_identity("USA/123"),
                ("123", "Test yacht", "First 31.7"),
            )
        self.assertEqual(
            get.call_args.args[0], orccatalog.ORC_ROOT + "/data/USA/123.json"
        )

    def test_boat_identity_surfaces_http_failure(self):
        with patch.object(
            orccatalog.requests,
            "get",
            side_effect=requests.ConnectionError("offline"),
        ):
            with self.assertRaises(requests.ConnectionError):
                orccatalog.orc_boat_identity("USA/123")

    def test_boat_identity_rejects_invalid_identifier(self):
        with self.assertRaises(ValueError):
            orccatalog.orc_boat_identity("../123")


if __name__ == "__main__":
    unittest.main()
