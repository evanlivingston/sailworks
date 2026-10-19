"""Qt integration runs in an isolated process."""

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

# The isolated Qt suite builds a fresh Core per check, so it grows with the
# number of checks. Measured on the base branch: 144 checks take 574.8 s
# locally, and CI runners are slower than that. The bound is a hang guard,
# not a runtime target, so it is set well above the measured duration; the
# timeout message reports the last check reached.
QT_PREVIEW_TIMEOUT = 1800


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "Install PySide6 to test Qt")
class TestQtPreview(unittest.TestCase):
    def test_isolated_qt_frontend(self):
        environment = dict(
            os.environ, QT_QPA_PLATFORM="offscreen", QT_STYLE_OVERRIDE="Fusion"
        )
        try:
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "test.qt_preview_checks", "-v"],
                cwd=Path(__file__).parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=QT_PREVIEW_TIMEOUT,
            )
        except subprocess.TimeoutExpired as error:
            output = "\n".join(
                part.decode(errors="replace") if isinstance(part, bytes) else part
                for part in (error.stdout, error.stderr)
                if part
            )
            self.fail(f"Qt checks exceeded {error.timeout}s. Partial output:\n{output}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
