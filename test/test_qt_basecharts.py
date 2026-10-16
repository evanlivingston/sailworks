"""Isolate the Qt portrayal-worker tests from GTK's process-wide GUI state."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "Qt dependencies")
class TestQtBaseCharts(unittest.TestCase):
    def test_base_chart_worker(self):
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "test.qt_basechart_checks", "-q"],
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
