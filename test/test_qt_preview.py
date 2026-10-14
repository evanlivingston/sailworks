"""Optional Qt integration runs in isolation from the existing GTK test suite."""

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path


@unittest.skipUnless(
    importlib.util.find_spec("PySide6"), "Install the qt extra to test Qt"
)
class TestQtPreview(unittest.TestCase):
    def test_isolated_qt_frontend(self):
        environment = dict(
            os.environ, QT_QPA_PLATFORM="offscreen", QT_STYLE_OVERRIDE="Fusion"
        )
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "test.qt_preview_checks", "-v"],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,  # Full Core and Qt integration checks in an isolated process.
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
