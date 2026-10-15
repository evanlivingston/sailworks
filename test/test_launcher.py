"""Exercise the default CLI in a clean process without loading either GUI loop."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


class TestDefaultLauncher(unittest.TestCase):
    def test_module_help_is_qt_cli_without_gtk_imports(self):
        code = """
import runpy, sys
sys.argv = ["gweatherrouting", "--help"]
try:
    runpy.run_module("gweatherrouting", run_name="__main__")
except SystemExit as exc:
    assert exc.code == 0
else:
    raise AssertionError("help must exit before starting the GUI")
assert "gi.repository.Gtk" not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).parents[1],
            env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--offline", result.stdout)
        self.assertIn("--log", result.stdout)
