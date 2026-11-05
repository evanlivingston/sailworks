"""Run Qt portrayal-worker tests in an isolated process."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "Qt dependencies")
class TestQtBaseCharts(unittest.TestCase):
    def test_base_chart_worker(self):
        # The module exits from its own runner: on CPython 3.11 the
        # finalization GC can trip a refcount bug in a C extension after the
        # tests have passed (see test/qt_basechart_checks.main).
        result = subprocess.run(
            [sys.executable, "-m", "test.qt_basechart_checks"],
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
