"""Current field density is persistent, independently owned and station-neutral."""

import os
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cairo

from gweatherrouting.core.marine.manager import MarineManager
from gweatherrouting.core.settings import SettingsManager
from gweatherrouting.core.timecontrol import TimeControl
from gweatherrouting.rendering.marinemaplayer import (
    CURRENT_DENSITIES,
    MarineRenderer,
    current_grid_spacing,
)
from test import test_marine_display as display
from test.test_marine import dataset
from test.test_marine_fields import field


def setUpModule():
    # Start Widgets before TimeControl's scheduling fallback can create a
    # QCoreApplication, which cannot subsequently be upgraded to QApplication.
    global app
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])


class TestCurrentDensity(unittest.TestCase):
    def test_density_rejects_stale_frames_and_cancels_old_field_work(self):
        from test.test_marine_scheduling import ManualMarineRenderer
        from test.test_overlay_scheduling import Canvas

        fixture = display.TestMarineDisplay()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        sources = fixture.job((field(),)).charts[0][0]
        renderer = ManualMarineRenderer()
        canvas = Canvas()
        cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 320, 240))
        renderer.draw(canvas, cr, ((sources,),), display.START)
        old_job = renderer._active_job
        dense = replace(sources, density="dense")
        renderer.draw(canvas, cr, ((dense,),), display.START)
        self.assertTrue(old_job.cancel.is_set())
        self.assertFalse(renderer._can_preview(old_job, renderer._states[canvas].key))
        renderer.finish()
        self.assertIsNone(renderer._states[canvas].frame)
        self.assertEqual(renderer._active_job.charts[0][0].density, "dense")
        renderer.finish()
        self.assertEqual(
            renderer._states[canvas].frame[0].charts[0][0].density, "dense"
        )

    def test_presets_sample_expected_grid_without_thinning_stations(self):
        fixture = display.TestMarineDisplay()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        original = dataset()
        value = replace(
            original,
            station=replace(original.station, latitude=49.5, longitude=-125.5),
        )
        fixture.manager.add(value)
        fixture.manager.enable(value.id)
        job = fixture.job((field(),))
        renderer = MarineRenderer()
        land = cairo.ImageSurface(
            cairo.FORMAT_A8, job.viewport.width, job.viewport.height
        )
        expected = {"sparse": 128, "standard": 64, "dense": 32, "maximum": 16}
        for key, _label, spacing in CURRENT_DENSITIES:
            with self.subTest(key=key):
                self.assertEqual(spacing, expected[key])
                self.assertEqual(current_grid_spacing(key), expected[key])
                job.charts = ((replace(job.charts[0][0], density=key),),)
                selection = Mock()
                selection.sample.return_value = SimpleNamespace(
                    status="available", values=(1, 0)
                )
                with (
                    patch.object(renderer._land_mask, "surface", return_value=land),
                    patch(
                        "gweatherrouting.rendering.marinemaplayer.FieldSelection",
                        return_value=selection,
                    ),
                    patch("gweatherrouting.rendering.marinemaplayer.arrow") as arrow,
                ):
                    renderer._render(job)
                positions = [
                    call.args[1:3] for call in arrow.call_args_list if not call.kwargs
                ]
                self.assertEqual(
                    positions,
                    [
                        (x, y)
                        for y in range(28, job.viewport.height, spacing)
                        for x in range(28, job.viewport.width, spacing)
                    ],
                )
                stations = [c for c in arrow.call_args_list if c.kwargs.get("station")]
                self.assertEqual(len(stations), 1)
                self.assertEqual(
                    stations[0].args[1:3],
                    job.viewport.convert_geographic_to_screen(
                        display.MapPoint(49.5, -125.5)
                    ),
                )
        self.assertEqual(current_grid_spacing("unknown"), 64)


class TestCurrentDensitySettings(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = patch("gweatherrouting.core.storage.DATA_DIR", temporary.name)
        storage.start()
        self.addCleanup(storage.stop)
        self.settings = SettingsManager()
        self.manager = MarineManager(temporary.name)
        self.controller = SimpleNamespace(
            settings=self.settings,
            core=SimpleNamespace(marine_manager=self.manager),
            data=Mock(),
            scheduler=SimpleNamespace(submit=lambda callback: callback()),
            connections=Mock(),
            time_control=TimeControl(),
        )

    def panel(self, kind="currents"):
        from gweatherrouting.qt.marinepanel import MarinePanel

        # Coverage calculations are independent of the density control.
        with patch.object(MarinePanel, "refresh"):
            panel = MarinePanel(self.controller, Mock(), kind, Mock())
        self.addCleanup(panel.deleteLater)
        self.addCleanup(panel.dispose)
        return panel

    def services(self):
        from gweatherrouting.qt.mapoverlays import MapOverlayServices

        with ExitStack() as stack:
            for module, name in (
                ("navigation", "NavigationRenderer"),
                ("ais", "AISRenderer"),
                ("dashboard", "DashboardRenderer"),
                ("isochrones", "IsochronesRenderer"),
            ):
                stack.enter_context(patch(f"gweatherrouting.rendering.{module}.{name}"))
            services = MapOverlayServices(self.controller, TimeControl())
        self.addCleanup(services.marine.close)
        return services

    def test_dropdown_persists_and_invalidates_each_owned_overlay(self):
        from gweatherrouting.qt.mapenvironment import MapEnvironmentMixin

        self.assertEqual(self.settings.marineCurrentDensity, "standard")
        self.settings.gribBarbDensity = "sparse"
        overlays = [self.services().marine for _ in range(2)]
        canvases = [Mock(), Mock()]
        for overlay, canvas in zip(overlays, canvases):
            overlay._maps.add(canvas)
        panel = self.panel()
        panel.show()
        self.addCleanup(panel.hide)
        self.assertTrue(panel.current_density.isVisible())
        self.assertEqual(panel.current_density.currentData(), "standard")
        self.assertEqual(
            [panel.current_density.itemText(i) for i in range(4)],
            ["Sparse", "Standard", "Dense", "Maximum"],
        )
        for key in ("sparse", "dense", "maximum", "standard"):
            old_sources = [overlay.sources for overlay in overlays]
            old_keys = [
                MapEnvironmentMixin._marine_key(SimpleNamespace(marine=overlay))
                for overlay in overlays
            ]
            self.controller.data.redraw.reset_mock()
            panel.current_density.setCurrentIndex(panel.current_density.findData(key))
            self.controller.data.redraw.assert_called_once_with()
            self.assertEqual(SettingsManager().marineCurrentDensity, key)
            self.assertEqual(self.settings.gribBarbDensity, "sparse")
            for i, (overlay, canvas) in enumerate(zip(overlays, canvases)):
                self.assertEqual(overlay.sources.density, key)
                self.assertIsNot(overlay.sources, old_sources[i])
                self.assertNotEqual(
                    MapEnvironmentMixin._marine_key(SimpleNamespace(marine=overlay)),
                    old_keys[i],
                )
                canvas.queue_draw.assert_called()
                canvas.queue_draw.reset_mock()
        self.controller.data.redraw.assert_called()
        self.settings.marineCurrentDensity = "maximum"
        reopened = self.panel()
        self.assertEqual(reopened.current_density.currentData(), "maximum")
        self.assertEqual(self.services().marine.sources.density, "maximum")

    def test_failed_save_restores_preference_and_selection(self):
        overlay = self.services().marine
        panel = self.panel()
        with (
            patch.object(SettingsManager, "save", side_effect=OSError("disk full")),
            patch("gweatherrouting.qt.marinepanel.QMessageBox.warning") as warning,
        ):
            panel.current_density.setCurrentIndex(
                panel.current_density.findData("dense")
            )
        warning.assert_called_once()
        self.assertEqual(self.settings.marineCurrentDensity, "standard")
        self.assertEqual(panel.current_density.currentData(), "standard")
        self.assertEqual(overlay.sources.density, "standard")

    def test_persisted_startup_and_live_density_reach_quick_snapshots(self):
        from PySide6.QtCore import QObject
        from PySide6.QtQuick import QQuickItem
        from gweatherrouting.qt.gribviewport import ViewportCamera
        from gweatherrouting.qt.marinequick import MarineQuickLayer
        from gweatherrouting.rendering.marinesnapshot import MarineSnapshot
        from test.test_marine import START
        import time

        self.settings.marineCurrentDensity = "dense"
        self.controller.settings = SettingsManager()  # Reload persisted preference.
        marine = self.services().marine
        self.assertEqual(marine.sources.density, "dense")
        canvas = Mock()
        marine._maps.add(canvas)
        parent, root = QObject(), QQuickItem()
        layer = MarineQuickLayer(root, parent)
        self.addCleanup(lambda root=root, parent=parent: layer.close())
        camera = ViewportCamera(49.5, -125.5, 10, 256, 192, START)

        def sample(sources, instant, *_args):
            return MarineSnapshot(sources, instant, (), "available")

        with patch(
            "gweatherrouting.qt.marinequick.sample_currents", side_effect=sample
        ):
            for density in ("dense", "maximum"):
                self.controller.settings.marineCurrentDensity = density
                layer.request(marine.sources, START, True, camera)
                deadline = time.monotonic() + 2
                while layer.snapshot is None and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(0.005)
                self.assertIsNotNone(layer.snapshot)
                self.assertEqual(layer.snapshot.sources.density, density)
        canvas.queue_draw.assert_called()

    def test_density_is_not_duplicated_in_settings_or_tides_panel(self):
        from PySide6.QtWidgets import QComboBox
        from gweatherrouting.qt.marinesettings import MarineSettings

        dialog = MarineSettings(self.controller)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.dispose)
        tides = self.panel("tides")
        for widget in (dialog, tides):
            self.assertFalse(hasattr(widget, "current_density"))
            self.assertFalse(
                any(
                    combo.accessibleName() == "Current field arrow density"
                    for combo in widget.findChildren(QComboBox)
                )
            )
