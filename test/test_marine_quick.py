"""Marine Quick snapshots, latest requests and camera-driven vector geometry."""

import os
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from sailworks.core.mapviewport import MapPoint, MapViewport
from sailworks.core.marine.manager import MarineManager
from sailworks.core.marine.models import Capability
from sailworks.qt.gribviewport import ViewportCamera
from sailworks.rendering.marinepresentation import MarineSources, chart_instant
from sailworks.rendering.marinesnapshot import (
    CurrentArrow,
    MarineSnapshot,
    current_triangles,
    sample_currents,
)
from test.test_marine import START, dataset
from test.test_marine_fields import field


def setUpModule():
    global app
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])


class TestMarineQuick(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = MarineManager(directory.name)
        station = replace(dataset().station, latitude=49.5, longitude=-125.5)
        value = dataset(station=station)
        self.manager.add(value)
        self.manager.enable(value.id)
        self.sources = MarineSources(
            (field(),),
            self.manager.snapshot(Capability.WATER_LEVEL),
            self.manager.snapshot(Capability.STATION_CURRENT),
        )
        self.camera = ViewportCamera(49.5, -125.5, 10, 256, 192, START)

    def sample(self, sources=None, instant=START):
        return sample_currents(
            sources or self.sources, instant, self.camera, lambda: False
        )

    def test_candidate_membership_key_agrees_with_current_view_sampling(self):
        from sailworks.qt.marinequick import _membership_key

        for density in ("sparse", "standard", "dense", "maximum"):
            sources = replace(self.sources, density=density)
            seen = {}
            for delta in (0, 0.00001, 0.001, 0.01, -0.01, 0.1):
                camera = replace(self.camera, center_lon=self.camera.center_lon + delta)
                key = _membership_key(sources, camera)
                snapshot = sample_currents(sources, START, camera, lambda: False)
                if key in seen:
                    self.assertEqual(snapshot.arrows, seen[key])
                else:
                    seen[key] = snapshot.arrows

    def test_covered_pan_with_identical_candidates_reuses_current_portrayal(self):
        from PySide6.QtCore import QObject
        from PySide6.QtQuick import QQuickItem
        from sailworks.qt.marinequick import MarineQuickLayer, _membership_key

        moved = replace(self.camera, center_lon=self.camera.center_lon + 0.00001)
        self.assertEqual(
            _membership_key(self.sources, self.camera),
            _membership_key(self.sources, moved),
        )
        parent, root = QObject(), QQuickItem()
        parent._map_content = root
        parent._map_anchor_camera = self.camera
        layer = MarineQuickLayer(root, parent)
        try:
            layer.camera = self.camera
            layer._ref_camera = self.camera
            layer._content = (self.sources, START, True)
            layer.snapshot = self.sample()
            layer._key = (
                id(self.sources),
                START,
                True,
                self.camera.zoom,
                self.camera.width,
                self.camera.height,
                _membership_key(self.sources, self.camera),
            )
            with patch.object(layer, "_present") as present:
                layer.set_camera(moved)
                present.assert_not_called()
            self.assertIsNone(layer._pending)
            self.assertEqual(layer.camera, moved)
        finally:
            layer.close()
            layer._thread.join(2)

    def test_current_geometry_follows_viewport_reference_across_rebase(self):
        from PySide6.QtCore import QObject
        from PySide6.QtQuick import QQuickItem
        from sailworks.qt.marinequick import MarineQuickLayer
        from sailworks.qt.viewporttransform import PanReference

        parent, root = QObject(), QQuickItem()
        root.setWidth(self.camera.width)
        root.setHeight(self.camera.height)
        parent._map_content = root
        parent._map_anchor_camera = self.camera
        layer = MarineQuickLayer(root, parent)
        self.addCleanup(lambda root=root, parent=parent: layer.close())
        snapshot = MarineSnapshot(
            self.sources,
            START,
            (CurrentArrow(49.5, -125.5, 1, 0),),
            "available",
        )
        layer.snapshot = snapshot
        layer.set_camera(self.camera)
        moved = replace(self.camera, center_lon=self.camera.center_lon + 0.01)
        dx, dy = PanReference.from_camera(self.camera).translation_to(moved)
        root.setX(dx)
        root.setY(dy)
        layer.set_camera(moved)
        # Vertices live in the item's padded surface, so add the item origin.
        actual = (
            layer.item.vertices["position"].astype(float)
            + (layer.item.x(), layer.item.y())
            + (dx, dy)
        )
        expected = np.asarray(current_triangles(snapshot, moved))[:, :2]
        np.testing.assert_allclose(actual, expected, rtol=0, atol=0.001)

        # Once the viewport rebases, its parent returns to zero and both
        # marine geometry and the map must use the new anchor camera.
        parent._map_anchor_camera = moved
        root.setX(0)
        root.setY(0)
        layer.set_camera(moved)
        np.testing.assert_allclose(
            layer.item.vertices["position"].astype(float)
            + (layer.item.x(), layer.item.y()),
            expected,
            rtol=0,
            atol=0.001,
        )

    def test_reference_projection_includes_current_view_edge_after_pan(self):
        from PySide6.QtQuick import QQuickItem
        from sailworks.qt.marinequick import MarineCurrentItem
        from sailworks.qt.viewporttransform import PanReference

        current = replace(self.camera, center_lon=self.camera.center_lon + 0.3)
        longitude = current.center_lon + 0.15
        snapshot = MarineSnapshot(
            self.sources,
            START,
            (CurrentArrow(current.center_lat, longitude, 1, 0),),
            "available",
        )
        # At the old reference this point is outside its normal 40px margin,
        # yet it is visible in the current camera after a covered pan.
        assert not current_triangles(snapshot, self.camera)
        expected = np.asarray(current_triangles(snapshot, current))[:, :2]
        assert len(expected)
        parent = QQuickItem()
        item = MarineCurrentItem(parent)
        item.present(snapshot, self.camera, current)
        dx, dy = PanReference.from_camera(self.camera).translation_to(current)
        np.testing.assert_allclose(
            item.vertices["position"].astype(float) + (item.x(), item.y()) + (dx, dy),
            expected,
            rtol=0,
            atol=0.001,
        )

    def test_marine_density_grid_and_station_batches(self):
        for density, spacing in (
            ("sparse", 128),
            ("standard", 64),
            ("dense", 32),
            ("maximum", 16),
        ):
            with self.subTest(density=density):
                sources = replace(self.sources, density=density)
                result = self.sample(sources)
                grid = [a for a in result.arrows if not a.station]
                stations = [a for a in result.arrows if a.station]
                self.assertGreater(len(grid), 0)
                positions = [
                    self.camera.view().convert_geographic_to_screen(
                        MapPoint(a.latitude, a.longitude)
                    )
                    for a in grid
                ]
                xs = sorted(set(round(x, 6) for x, _ in positions))
                ys = sorted(set(round(y, 6) for _, y in positions))
                np.testing.assert_allclose(np.diff(xs), spacing)
                np.testing.assert_allclose(np.diff(ys), spacing)
                self.assertLessEqual(
                    abs(len(grid) - 256 * 192 / spacing**2), len(xs) + len(ys) + 1
                )
                self.assertEqual(len(stations), 1)
                self.assertEqual(
                    (stations[0].latitude, stations[0].longitude), (49.5, -125.5)
                )
                self.assertIs(result.sources, sources)
                self.assertEqual(result.instant, START)
                self.assertTrue(current_triangles(result, self.camera))

    def test_time_coverage_and_field_anchors_are_sampled(self):
        result = self.sample()
        self.assertGreater(sum(not a.station for a in result.arrows), 0)
        self.assertEqual(sum(a.station for a in result.arrows), 1)
        self.assertEqual(self.sample(instant=START + timedelta(days=1)).arrows, ())
        self.assertEqual(
            self.sample(instant=START + timedelta(days=1)).state, "no valid data"
        )

    def test_source_policy_and_effective_instant_are_not_wind_camera_time(self):
        instant = chart_instant(START.astimezone().replace(tzinfo=None))
        camera = replace(self.camera, time=START + timedelta(days=100), density="all")
        selected = Mock()
        selected.sample_arrays.side_effect = lambda positions, times: SimpleNamespace(
            status=np.full(len(positions), "available", dtype=object),
            values=np.tile([[3.0, 4.0]], (len(positions), 1)),
        )
        with patch(
            "sailworks.rendering.marinesnapshot.FieldSelection",
            return_value=selected,
        ) as selection:
            result = sample_currents(self.sources, instant, camera, lambda: False)
        selection.assert_called_once_with(self.sources.fields)
        self.assertTrue(
            all(
                np.allclose(call.args[1], instant.timestamp())
                for call in selected.sample_arrays.call_args_list
            )
        )
        self.assertTrue(
            all((a.east, a.north) == (3, 4) for a in result.arrows if not a.station)
        )
        self.assertGreater(len([a for a in result.arrows if not a.station]), 0)

    def test_zoom_reprojects_anchors_without_scaling_arrow_dimensions(self):
        snapshot = MarineSnapshot(
            self.sources, START, (CurrentArrow(49.5, -125.48, 1, 0),), "available"
        )
        first = np.asarray(current_triangles(snapshot, self.camera))[:, :2]
        second = np.asarray(
            current_triangles(snapshot, replace(self.camera, zoom=self.camera.zoom + 1))
        )[:, :2]
        np.testing.assert_allclose(np.ptp(first, axis=0), np.ptp(second, axis=0))
        expected_shift = 0.02 / 360 * 256 * 2**self.camera.zoom
        np.testing.assert_allclose(second - first, np.tile((expected_shift, 0), (9, 1)))

    def test_published_field_anchors_survive_zoom_and_level_refinement(self):
        def sample(camera):
            return sample_currents(self.sources, START, camera, lambda: False)

        def anchors(snapshot):
            return {(a.latitude, a.longitude) for a in snapshot.arrows if not a.station}

        first = sample(self.camera)
        for zoom in (10.25, 10.49, 10.5, 10.75, 11):
            with self.subTest(zoom=zoom):
                camera = replace(self.camera, zoom=zoom)
                latest = sample(camera)
                visible = {
                    (lat, lon)
                    for lat, lon in anchors(first)
                    if all(
                        0 <= p <= limit
                        for p, limit in zip(
                            camera.view().convert_geographic_to_screen(
                                MapPoint(lat, lon)
                            ),
                            (camera.width, camera.height),
                        )
                    )
                }
                self.assertTrue(visible)
                self.assertLessEqual(visible, anchors(latest))
                if zoom < 10.5:
                    self.assertEqual(anchors(latest), visible)
                self.assertEqual(
                    [a for a in first.arrows if a.station],
                    [a for a in latest.arrows if a.station],
                )
                # Publishing a replacement must not move surviving geometry.
                arrow = next(
                    a for a in first.arrows if (a.latitude, a.longitude) in visible
                )
                old = replace(first, arrows=(arrow,))
                new = replace(latest, arrows=(arrow,))
                self.assertEqual(
                    current_triangles(old, camera), current_triangles(new, camera)
                )

    def test_field_lattice_wraps_clips_and_samples_actual_anchors(self):
        selection = Mock()
        selection.sample_arrays.side_effect = lambda positions, times: SimpleNamespace(
            status=np.full(len(positions), "available", dtype=object),
            values=np.tile([[1.0, 0.0]], (len(positions), 1)),
        )
        for latitude in (-85, 0, 85):
            camera = replace(self.camera, center_lat=latitude, center_lon=179, zoom=2)
            selection.sample_arrays.reset_mock()
            with patch(
                "sailworks.rendering.marinesnapshot.FieldSelection",
                return_value=selection,
            ):
                snapshot = sample_currents(self.sources, START, camera, lambda: False)
            anchors = [
                (a.latitude, a.longitude) for a in snapshot.arrows if not a.station
            ]
            self.assertTrue(anchors)
            self.assertEqual(len(anchors), len(set(anchors)))
            self.assertEqual(
                anchors,
                [
                    tuple(position)
                    for call in selection.sample_arrays.call_args_list
                    for position in call.args[0]
                ],
            )
            self.assertTrue(
                all(
                    -180 <= lon < 180 and abs(lat) <= MapViewport.MAX_LAT
                    for lat, lon in anchors
                )
            )
            for lat, lon in anchors:
                wrapped = (
                    camera.center_lon + (lon - camera.center_lon + 180) % 360 - 180
                )
                x, y = camera.view().convert_geographic_to_screen(
                    MapPoint(lat, wrapped)
                )
                self.assertTrue(-1e-6 <= x <= camera.width + 1e-6)
                self.assertTrue(-1e-6 <= y <= camera.height + 1e-6)
            if latitude == 0:
                self.assertTrue(any(lon < 0 for _, lon in anchors))
                self.assertTrue(any(lon > 0 for _, lon in anchors))

    def test_cancellation_and_station_deduplication(self):
        self.assertIsNone(
            sample_currents(self.sources, START, self.camera, lambda: True)
        )
        currents = replace(
            self.sources.currents, datasets=self.sources.currents.datasets * 2
        )
        result = self.sample(replace(self.sources, currents=currents))
        self.assertEqual(sum(a.station for a in result.arrows), 1)

    def test_current_surface_paints_an_image_without_a_quick_item(self):
        from PySide6.QtQuick import QQuickItem
        from sailworks.qt.marinequick import MarineCurrentItem, MarineImageProvider

        item = MarineCurrentItem(QQuickItem())
        snapshot = MarineSnapshot(
            self.sources,
            START,
            (CurrentArrow(49.5, -125.5, 1, 0),),
            "available",
        )
        item.present(snapshot, self.camera)
        self.assertNotIsInstance(item, QQuickItem)
        self.assertFalse(item.image.isNull())
        provider = MarineImageProvider()
        token = provider.publish(item.image)
        image = provider.requestImage(token.rsplit("/", 1)[-1], None, None)
        self.assertEqual(image.size(), item.image.size())

    def test_empty_content_hides_and_clears_the_painted_surface(self):
        from PySide6.QtQuick import QQuickItem
        from sailworks.qt.marinequick import MarineCurrentItem

        item = MarineCurrentItem(QQuickItem())
        snapshot = MarineSnapshot(
            self.sources,
            START,
            (CurrentArrow(49.5, -125.5, 1, 0),),
            "available",
        )
        for _ in range(3):
            item.present(snapshot, self.camera)
            self.assertTrue(item.isVisible())
            self.assertGreater(len(item.vertices), 0)
            self.assertFalse(item.image.isNull())
            item.present(None, self.camera)
            self.assertFalse(item.isVisible())
            self.assertEqual(len(item.vertices), 0)

    @unittest.skipUnless(
        os.environ.get("GWR_MARINE_QUICK_SNAPSHOT") == "1",
        "opt-in real Qt Quick window readback",
    )
    def test_quick_scene_renders_grid_and_station_and_reprojects(self):
        from PySide6.QtGui import QColor
        from PySide6.QtQuick import QQuickWindow
        from sailworks.qt.marinequick import MarineCurrentItem

        window = QQuickWindow()
        window.resize(self.camera.width, self.camera.height)
        window.setColor(QColor("transparent"))
        item = MarineCurrentItem(window.contentItem())
        failures = []
        item.failed.connect(failures.append)
        snapshot = MarineSnapshot(
            self.sources,
            START,
            (
                CurrentArrow(49.5, -125.55, 1, 0),
                CurrentArrow(49.5, -125.45, 0, 1, True),
            ),
            "available",
        )
        window.show()
        try:
            images = []
            for camera in (
                self.camera,
                replace(self.camera, zoom=self.camera.zoom + 1),
            ):
                item.present(snapshot, camera)
                deadline = time.monotonic() + 0.3
                while time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(0.005)
                image = window.grabWindow()
                self.assertFalse(image.isNull(), "Quick window readback unavailable")
                self.assertFalse(failures, failures)
                ratio = image.devicePixelRatio()
                center = int(camera.width * ratio / 2)
                for left, right in ((0, center), (center, image.width())):
                    count = sum(
                        image.pixelColor(x, y).alpha() > 0
                        for x in range(left, right)
                        for y in range(image.height())
                    )
                    self.assertGreater(count, 20, "missing grid or station geometry")
                images.append(image)
            self.assertNotEqual(images[0], images[1])
        finally:
            window.close()
            window.releaseResources()

    def test_time_request_keeps_previous_arrows_until_latest_frame_publishes(self):
        from PySide6.QtCore import QObject
        from PySide6.QtQuick import QQuickItem
        from sailworks.qt.marinequick import MarineQuickLayer

        parent, root = QObject(), QQuickItem()
        entered, release = Event(), Event()

        latest = START + timedelta(minutes=5)

        def sampling(sources, instant, camera, cancelled):
            entered.set()
            release.wait(2)
            return MarineSnapshot(sources, instant, (), "no valid data")

        with patch("sailworks.qt.marinequick.sample_currents", side_effect=sampling):
            layer = MarineQuickLayer(root, parent)
            sources = replace(self.sources, fields=())
            old_snapshot = MarineSnapshot(
                sources,
                START,
                (CurrentArrow(49.5, -125.5, 1, 0),),
                "available",
            )
            layer._content = (sources, START, True)
            layer.snapshot = old_snapshot
            layer.item.present(old_snapshot, self.camera)
            old_vertices = layer.item.vertices.copy()
            layer.request(sources, latest, True, self.camera)
            self.assertTrue(entered.wait(2))
            self.assertIs(layer.snapshot, old_snapshot)
            np.testing.assert_array_equal(layer.item.vertices, old_vertices)
            self.assertEqual(layer.state, "loading (showing previous frame)")
            release.set()
            deadline = time.monotonic() + 3
            while layer.snapshot is old_snapshot and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)
            self.assertIsNotNone(layer.snapshot)
            self.assertEqual(layer.snapshot.instant, latest)
            self.assertEqual(len(layer.item.vertices), 0)
            layer.snapshot = old_snapshot
            layer.item.present(old_snapshot, self.camera)
            new_sources = replace(sources, density="dense")
            layer.request(new_sources, latest, True, self.camera)
            self.assertIsNone(layer.snapshot)
            self.assertEqual(len(layer.item.vertices), 0)
            layer.request(new_sources, latest, False, self.camera)
            self.assertIsNone(layer.snapshot)
            self.assertEqual(layer.state, "hidden")
            layer.close()
            layer._thread.join(2)
            self.assertFalse(layer._thread.is_alive())

    def test_pan_keeps_previous_view_visible_while_current_sample_is_loading(self):
        from PySide6.QtCore import QObject
        from PySide6.QtQuick import QQuickItem
        from sailworks.qt.marinequick import MarineQuickLayer, _membership_key

        parent, root = QObject(), QQuickItem()
        entered, release = Event(), Event()
        sources = self.sources
        old_snapshot = MarineSnapshot(
            sources, START, (CurrentArrow(49.5, -125.5, 1, 0),), "available"
        )

        def sampling(sources, instant, camera, cancelled):
            entered.set()
            release.wait(2)
            return MarineSnapshot(sources, instant, (), "no valid data")

        with patch("sailworks.qt.marinequick.sample_currents", side_effect=sampling):
            layer = MarineQuickLayer(root, parent)
            try:
                layer._content = (sources, START, True)
                layer._key = (
                    id(sources),
                    START,
                    True,
                    self.camera.zoom,
                    self.camera.width,
                    self.camera.height,
                    _membership_key(sources, self.camera),
                )
                layer.snapshot = old_snapshot
                layer.item.present(old_snapshot, self.camera)
                moved = replace(self.camera, center_lon=self.camera.center_lon + 0.1)
                self.assertNotEqual(
                    _membership_key(sources, self.camera),
                    _membership_key(sources, moved),
                )
                layer.set_camera(moved)
                self.assertTrue(entered.wait(2))
                self.assertIs(layer.snapshot, old_snapshot)
                self.assertGreater(len(layer.item.vertices), 0)
                self.assertEqual(layer.state, "loading (showing previous frame)")
            finally:
                release.set()
                layer.close()
                layer._thread.join(2)

    def test_scene_failure_reports_unavailable_without_cairo_fallback(self):
        from PySide6.QtCore import QObject
        from PySide6.QtQuick import QQuickItem
        from sailworks.qt.marinequick import MarineQuickLayer

        parent, root = QObject(), QQuickItem()
        layer = MarineQuickLayer(root, parent)
        layer._fail("injected geometry failure")
        self.assertIn("Marine rendering unavailable", layer.state)
        self.assertIn("injected geometry failure", layer.state)
        self.assertFalse(layer.item.isVisible())
        self.assertFalse(layer.request(self.sources, START, True, self.camera))
        layer.close()

    def test_quick_label_item_receives_station_annotations(self):
        from PySide6.QtQuick import QQuickItem
        from sailworks.qt.marinequick import MarineLabelItem

        root = QQuickItem()
        item = MarineLabelItem(root)
        item.present(self.sample(), self.camera)
        self.assertTrue(item.labels)
        self.assertTrue(any("station" in text for _, _, text in item.labels))

    def test_worker_failure_relinquishes_ownership(self):
        from PySide6.QtCore import QObject
        from PySide6.QtQuick import QQuickItem
        from sailworks.qt.marinequick import MarineQuickLayer

        parent, root = QObject(), QQuickItem()
        layer = MarineQuickLayer(root, parent)
        self.addCleanup(lambda root=root, parent=parent: layer.close())
        sources = replace(self.sources, fields=())
        old_snapshot = self.sample(sources=sources)
        layer._content = (sources, START, True)
        layer.snapshot = old_snapshot
        layer.item.present(old_snapshot, self.camera)
        layer.labels.present(old_snapshot, self.camera)
        self.assertTrue(layer.labels.labels)
        with patch(
            "sailworks.qt.marinequick.sample_currents",
            side_effect=RuntimeError("injected sampling failure"),
        ):
            layer.request(sources, START + timedelta(minutes=5), True, self.camera)
            deadline = time.monotonic() + 2
            while not layer.error and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)
        self.assertIn("Marine rendering unavailable", layer.state)
        self.assertIn("injected sampling failure", layer.state)
        self.assertFalse(layer.labels.labels)
        self.assertFalse(layer.labels.isVisible())
        self.assertFalse(layer.request(*layer._content, self.camera))
