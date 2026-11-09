"""Connection editor probe button behavior."""

import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from sailworks.qt.settingsdialog import ConnectionDialog


class FakeConnections:
    def __init__(self, statuses):
        self.statuses = statuses

    def get_status(self):
        return self.statuses


class TestConnectionDialogProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for_test(self, dialog, timeout=2.0):
        deadline = time.monotonic() + timeout
        while dialog.test_task.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()

    def test_button_reports_received_messages(self):
        dialog = ConnectionDialog(None)
        self.addCleanup(dialog.reject)
        self.assertEqual(dialog.test_button.text(), "Test connection")
        with patch(
            "sailworks.qt.settingsdialog.probe_connection",
            return_value=(True, 5, None),
        ) as probe:
            dialog.test_button.click()
            self.assertIn("Testing connection", dialog.test_status.text())
            self.wait_for_test(dialog)
        self.assertIn("Connected", dialog.test_status.text())
        self.assertIn("5", dialog.test_status.text())
        self.assertTrue(dialog.test_button.isEnabled())
        probe.assert_called_once()

    def test_button_reports_failure(self):
        dialog = ConnectionDialog(None)
        self.addCleanup(dialog.reject)
        with patch(
            "sailworks.qt.settingsdialog.probe_connection",
            return_value=(False, 0, "Connection refused"),
        ):
            dialog.test_button.click()
            self.wait_for_test(dialog)
        self.assertIn("Test failed", dialog.test_status.text())
        self.assertIn("Connection refused", dialog.test_status.text())

    def test_already_connected_source_skips_probe(self):
        dialog = ConnectionDialog(
            None,
            connections=FakeConnections(
                [
                    {
                        "name": "localhost:10110",
                        "connected": True,
                        "packets": 42,
                        "error": None,
                    }
                ]
            ),
        )
        self.addCleanup(dialog.reject)
        with patch("sailworks.qt.settingsdialog.probe_connection") as probe:
            dialog.test()
        self.assertIn("Already connected", dialog.test_status.text())
        self.assertIn("42", dialog.test_status.text())
        probe.assert_not_called()

    def test_empty_host_reports_validation_error(self):
        dialog = ConnectionDialog(None)
        self.addCleanup(dialog.reject)
        dialog.host.setText("   ")
        with patch("sailworks.qt.settingsdialog.probe_connection") as probe:
            dialog.test()
        self.assertTrue(dialog.error.text())
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
