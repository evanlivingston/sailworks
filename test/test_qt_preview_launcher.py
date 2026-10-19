"""Timeout diagnostics must survive subprocess termination in CI."""

import subprocess
import unittest
from unittest.mock import patch

from test import test_qt_preview


class TestPreviewTimeoutDiagnostics(unittest.TestCase):
    def test_timeout_preserves_last_test_and_worker_output(self):
        bound = test_qt_preview.QT_PREVIEW_TIMEOUT
        for output in (b"last_test ...", "last_test ..."):
            with self.subTest(output=output):
                error = subprocess.TimeoutExpired(
                    "qt checks", bound, output=output, stderr=b"worker stalled"
                )
                with patch("test.test_qt_preview.subprocess.run", side_effect=error):
                    with self.assertRaisesRegex(
                        AssertionError, f"(?s){bound}s.*last_test.*worker stalled"
                    ):
                        test_qt_preview.TestQtPreview().test_isolated_qt_frontend()

    def test_timeout_without_output_is_readable(self):
        bound = test_qt_preview.QT_PREVIEW_TIMEOUT
        error = subprocess.TimeoutExpired("qt checks", bound)
        with patch("test.test_qt_preview.subprocess.run", side_effect=error):
            with self.assertRaisesRegex(AssertionError, f"Qt checks exceeded {bound}s"):
                test_qt_preview.TestQtPreview().test_isolated_qt_frontend()
