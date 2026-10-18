"""Panel interaction checks use their own QApplication process."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "Install PySide6 to test Qt")
class TestQtDocking(unittest.TestCase):
    def test_isolated_panel_interactions(self):
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "test.qt_docking_checks", "-v"],
            cwd=Path(__file__).parents[1],
            env=dict(
                os.environ, QT_QPA_PLATFORM="offscreen", QT_STYLE_OVERRIDE="Fusion"
            ),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
