"""Atomic workspace storage and recovery from unavailable monitor layouts."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gweatherrouting.core.workspacestate import WorkspaceStateStore, window_geometry


class TestWorkspaceStateStore(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = WorkspaceStateStore(Path(directory.name) / "workspace.json")

    def test_missing_corrupt_and_unknown_versions_fall_back(self):
        self.assertEqual(self.store.load(), {})
        for contents in (
            "{partial",
            "[]",
            '{"version": 999, "state": {}}',
            '{"version": 1, "state": []}',
        ):
            self.store.path.write_text(contents)
            self.assertEqual(self.store.load(), {})

    def test_roundtrip_and_atomic_failure_preserve_previous_file(self):
        state = {"active_tab": "Logs", "dock": {"width": 340}}
        self.store.save(state)
        self.assertEqual(self.store.load(), state)
        self.assertEqual(json.loads(self.store.path.read_text())["version"], 1)
        with patch(
            "gweatherrouting.core.workspacestate.os.replace",
            side_effect=OSError("disk error"),
        ):
            with self.assertRaises(OSError):
                self.store.save({"active_tab": "Polars"})
        self.assertEqual(self.store.load(), state)
        self.assertEqual(list(self.store.path.parent.glob(".workspace-*.tmp")), [])

    def test_invalid_numbers_cannot_replace_good_state(self):
        self.store.save({"active_tab": "Charts"})
        with self.assertRaises(ValueError):
            self.store.save({"zoom": float("nan")})
        self.assertEqual(self.store.load(), {"active_tab": "Charts"})

    def test_disconnected_monitors_and_oversized_windows_are_recovered(self):
        areas = [(0, 0, 1440, 860), (-1920, 0, 1920, 1080)]
        self.assertEqual(
            window_geometry(dict(x=3000, y=2000, width=2000, height=2000), areas),
            dict(x=0, y=0, width=1440, height=860),
        )
        self.assertEqual(
            window_geometry(dict(x=-1800, y=100, width=500, height=600), areas),
            dict(x=-1800, y=100, width=500, height=600),
        )
        bounds = window_geometry(
            dict(x="bad", y=None, width=float("inf"), height=-10), areas
        )
        self.assertEqual(bounds, dict(x=0, y=0, width=1280, height=100))
