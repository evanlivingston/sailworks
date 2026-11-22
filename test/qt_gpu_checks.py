"""Isolated Qt application checks for the embedded GPU map."""

import json
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
    @classmethod
    def setUpClass(cls):
        QtPreviewChecks.setUpClass.__func__(cls)

    def setUp(self):
        from sailworks.core.marine.credentials import SystemCredentialStore

        guard = patch.object(SystemCredentialStore, "load", return_value=None)
        guard.start()
        self.addCleanup(guard.stop)
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
    def test_gpu_is_the_only_map_renderer(self):
        from sailworks.core.settings import SettingsManager

        self.assertEqual(self.controller.map_renderer, "gpu")
        self.window.show_settings()
        dialog = self.window.settings_dialog
        self.assertFalse(hasattr(dialog, "gpu_rendering"))
        dialog.refresh_renderer_status()
        self.assertEqual(
            dialog.renderer_status.text(), self.controller.renderer_status()
        )
        self.assertNotIn("Restart to apply.", dialog.renderer_status.text())
        with (
            patch("sailworks.core.storage.DATA_DIR", str(self.path)),
            patch.object(Storage, "save", ORIGINAL_STORAGE_SAVE),
        ):
            self.assertEqual(SettingsManager().mapRenderer, "gpu")
            override = SessionController(
                load_charts=False,
                map_renderer="cpu",
                recording_directory=self.path / "override-recordings",
            )
            self.addCleanup(override.close)
            self.assertEqual(override.map_renderer, "gpu")

    def test_saved_cpu_choice_cannot_select_cpu(self):
        from sailworks.core.settings import SettingsManager

        # Seed the settings file a pre-GPU install leaves behind. The shared
        # setUp mocks load_or_save_default (which also leaves save() a no-op
        # because the storage instance is never marked initialized), so
        # restore it on SettingsManager only. Other storage fixtures in this
        # test stay mocked; the seeded value must be read back here.
        saved = self.path / "settings.json"
        saved.write_text(json.dumps({"mapRenderer": "cpu"}))

        with (
            patch("sailworks.core.storage.DATA_DIR", str(self.path)),
            patch.object(Storage, "save", ORIGINAL_STORAGE_SAVE),
            patch.object(
                SettingsManager, "load_or_save_default", ORIGINAL_STORAGE_LOAD
            ),
        ):
            self.assertEqual(SettingsManager().mapRenderer, "cpu")
            legacy = SessionController(
                load_charts=False,
                recording_directory=self.path / "legacy-cpu-recordings",
            )
            self.addCleanup(legacy.close)
            self.assertEqual(legacy.map_renderer, "gpu")
            self.assertEqual(legacy.settings.mapRenderer, "cpu")

        # Loading and choosing the renderer must not normalize the saved value.
        self.assertEqual(json.loads(saved.read_text())["mapRenderer"], "cpu")

    def test_unsupported_gpu_does_not_rewrite_renderer_choice(self):
        from sailworks.qt.viewportmap import ViewportMap

        self.controller.settings.mapRenderer = "gpu"
        canvas = ViewportMap(self.controller)
        self.addCleanup(canvas.dispose)
        self.assertFalse(hasattr(canvas, "_gpu"))
        self.assertNotIn("CPU fallback", self.controller.renderer_status())
        self.assertEqual(self.controller.settings.mapRenderer, "gpu")


if __name__ == "__main__":
    unittest.main()
