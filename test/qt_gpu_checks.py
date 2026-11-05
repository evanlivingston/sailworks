"""Isolated Qt application checks for the experimental embedded GPU map."""

import time
import unittest
from unittest.mock import patch

from sailworks.qt.controller import SessionController
from test.qt_preview_checks import (
    QtPreviewChecks,
    ORIGINAL_STORAGE_LOAD,
    ORIGINAL_STORAGE_SAVE,
    Storage,
)


class Fixture(unittest.TestCase):
    renderer = None

    @classmethod
    def setUpClass(cls):
        QtPreviewChecks.setUpClass.__func__(cls)

    def setUp(self):
        from sailworks.core.marine.credentials import SystemCredentialStore

        guard = patch.object(SystemCredentialStore, "load", return_value=None)
        guard.start()
        self.addCleanup(guard.stop)
        with patch(
            "test.qt_preview_checks.SessionController",
            side_effect=lambda *a, **kw: SessionController(
                *a, **kw, map_renderer=self.renderer
            ),
        ):
            QtPreviewChecks.setUp(self)

    drain = QtPreviewChecks.drain
    # The shared setUp settles the display forecast; these fixtures reuse the
    # same waits without inheriting from QtPreviewChecks.
    forecast_scan_pending = QtPreviewChecks.forecast_scan_pending
    settle_forecast = QtPreviewChecks.settle_forecast
    SETTLE_POLLS = QtPreviewChecks.SETTLE_POLLS
    SETTLE_POLL_MS = QtPreviewChecks.SETTLE_POLL_MS
    SETTLE_DEADLINE = QtPreviewChecks.SETTLE_DEADLINE

    def wait(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.drain()
            if time.monotonic() > deadline:
                self.fail("Timed out: " + self.controller.renderer_status())
            time.sleep(0.002)


class PreferenceChecks(Fixture):
    def test_default_cpu_and_saved_gpu_requires_restart(self):
        from sailworks.core.settings import SettingsManager

        self.assertEqual(self.controller.map_renderer, "cpu")
        self.assertIsNone(self.window.map._gpu)
        self.window.show_settings()
        dialog = self.window.settings_dialog
        with (
            patch("sailworks.core.storage.DATA_DIR", str(self.path)),
            patch.object(Storage, "save", ORIGINAL_STORAGE_SAVE),
        ):
            ORIGINAL_STORAGE_LOAD(self.controller.settings)
            with patch.object(self.controller.data, "redraw") as redraw:
                dialog.gpu_rendering.setChecked(True)
                redraw.assert_not_called()
            self.assertEqual(self.controller.settings.mapRenderer, "gpu")
            self.assertEqual(self.controller.map_renderer, "cpu")
            self.assertEqual(dialog.renderer_status.text(), "Restart to apply.")
            with patch.object(Storage, "load_or_save_default", ORIGINAL_STORAGE_LOAD):
                loaded = SettingsManager()
                self.assertEqual(loaded.mapRenderer, "gpu")
                saved = SessionController(
                    load_charts=False,
                    recording_directory=self.path / "saved-recordings",
                )
                self.addCleanup(saved.close)
                self.assertEqual(saved.map_renderer, "gpu")
                override = SessionController(
                    load_charts=False,
                    map_renderer="cpu",
                    recording_directory=self.path / "override-recordings",
                )
                self.addCleanup(override.close)
                self.assertEqual(override.map_renderer, "cpu")
                self.assertEqual(override.settings.mapRenderer, "gpu")
            dialog.gpu_rendering.setChecked(False)
            self.assertEqual(self.controller.settings.mapRenderer, "cpu")

    def test_save_error_restores_toggle_without_changing_renderer(self):
        self.window.show_settings()
        dialog = self.window.settings_dialog
        with (
            patch.object(Storage, "save", side_effect=OSError("disk full")),
            patch("sailworks.qt.settingsdialog.QMessageBox.warning") as warning,
        ):
            dialog.gpu_rendering.setChecked(True)
        warning.assert_called_once()
        self.assertFalse(dialog.gpu_rendering.isChecked())
        self.assertEqual(self.controller.settings.mapRenderer, "cpu")
        self.assertEqual(self.controller.map_renderer, "cpu")

    def test_unsupported_gpu_does_not_rewrite_renderer_choice(self):
        from sailworks.qt.viewportmap import ViewportMap

        self.controller.map_renderer = "gpu"
        self.controller.settings.mapRenderer = "gpu"
        with patch(
            "sailworks.qt.gpupresenter.QGuiApplication.platformName",
            return_value="offscreen",
        ):
            canvas = ViewportMap(self.controller)
        self.addCleanup(canvas.dispose)
        self.assertIsNone(canvas._gpu)
        self.assertNotIn("CPU fallback", self.controller.renderer_status())
        self.assertEqual(self.controller.settings.mapRenderer, "gpu")


if __name__ == "__main__":
    unittest.main()
