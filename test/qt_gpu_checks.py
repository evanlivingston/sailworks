"""Isolated Qt application checks for the experimental embedded GPU map."""

import os
import time
import unittest
from unittest.mock import patch

import numpy as np
from PySide6.QtCore import Qt

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


@unittest.skipUnless(os.environ.get("GWR_RUN_GPU_TESTS") == "1", "Native GPU opt-in")
class NativeGpuChecks(Fixture):
    renderer = "gpu"

    def setUp(self):
        super().setUp()
        self.wait(
            lambda: self.window.map._gpu is not None
            and self.window.map._gpu.initialized
        )
        self.assertTrue(self.window.map._gpu.active, self.controller.renderer_status())

    def test_embedded_map_uses_hardware_and_coalesces_updates(self):
        canvas = self.window.map
        gpu = canvas._gpu
        self.assertIn("GPU:", self.controller.renderer_status())
        before = canvas._gpu_sequence
        for _ in range(30):
            canvas.update()
        self.wait(lambda: canvas._gpu_sequence > before)
        self.assertLess(canvas._gpu_sequence - before, 5)
        self.assertEqual(gpu.widget.geometry(), canvas.rect())
        self.assertTrue(
            gpu.widget.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        )
        self.assertLessEqual(gpu.estimate["gpu_estimate_bytes"], gpu.GPU_LIMIT)

    def test_gestures_forward_from_host_and_ui_stays_above_it(self):
        canvas, gpu = self.window.map, self.window.map._gpu
        before = canvas._zoom
        anchor = canvas.convert_screen_to_geographic(83, 71)
        for kind, value in (
            (Qt.NativeGestureType.BeginNativeGesture, 0),
            (Qt.NativeGestureType.ZoomNativeGesture, 0.2),
            (Qt.NativeGestureType.EndNativeGesture, 0),
        ):
            self.app.sendEvent(gpu.widget, QtPreviewChecks._native_pinch(kind, value))
        self.assertGreater(canvas._zoom, before)
        np.testing.assert_allclose(
            canvas.convert_geographic_to_screen(anchor), (83, 71), atol=1e-6
        )
        canvas.coverage_fetch.setGeometry(20, 20, 80, 30)
        canvas.coverage_fetch.show()
        canvas.coverage_fetch.raise_()
        self.assertIs(canvas.childAt(40, 30), canvas.coverage_fetch)
        canvas.begin_move(lambda _point: None)
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent

        self.app.sendEvent(
            gpu.overlay,
            QKeyEvent(
                QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
            ),
        )
        self.assertIsNone(canvas.move_callback)

    def test_hide_show_log_view_resize_and_close_release_scene(self):
        canvas, gpu = self.window.map, self.window.map._gpu
        self.window.views.setCurrentWidget(self.window.logs_view)
        self.drain()
        self.assertIsNone(gpu.scene.frame)
        self.assertFalse(gpu.scene.layers)
        self.wait(lambda: self.window.log_map._gpu.initialized)
        self.window.views.setCurrentWidget(canvas)
        self.wait(lambda: gpu.scene.frame is not None)
        self.window.resize(self.window.width() + 80, self.window.height() + 40)
        self.wait(lambda: gpu.widget.size() == canvas.size())
        self.window.close()
        self.assertTrue(gpu.closed)
        self.assertFalse(gpu.timer.isActive())

    def test_budget_failure_falls_back_and_can_close_safely(self):
        canvas, gpu = self.window.map, self.window.map._gpu
        gpu.GPU_LIMIT = 1
        canvas.update()
        self.wait(lambda: not gpu.active)
        self.drain()
        self.assertIn("resource limits", self.controller.renderer_status())
        self.assertIsNone(gpu.widget)
        canvas.grab()
        self.window.close()

    def test_scene_graph_error_preserves_view_selection_and_saved_choice(self):
        canvas, gpu = self.window.map, self.window.map._gpu
        from PySide6.QtQuick import QQuickWindow

        self.controller.settings.mapRenderer = "gpu"
        canvas.set_route_plan(((48.4, -123.4), (48.5, -123.5)))
        state = canvas._center_lat, canvas._center_lon, canvas._zoom, canvas.route_plan
        gpu.widget.sceneGraphError.emit(
            QQuickWindow.SceneGraphError.ContextNotAvailable, "test context failure"
        )
        self.wait(lambda: gpu.widget is None)
        self.assertEqual(
            (canvas._center_lat, canvas._center_lon, canvas._zoom, canvas.route_plan),
            state,
        )
        self.assertEqual(self.controller.settings.mapRenderer, "gpu")
        self.assertIn("test context failure", self.controller.renderer_status())


if __name__ == "__main__":
    unittest.main()
