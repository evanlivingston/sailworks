"""Focused checks for the isolated ICOADS display state."""

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import numpy as np

from sailworks.core.marine.icoads import ShipReport, parse_dataset
from sailworks.qt.icoadsquick import IcoadsQuickItem, IcoadsQuickLayer
from sailworks.qt.icoadsstate import IcoadsState
from sailworks.qt.marinepanel import BusyButton


def setUpModule():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    global app
    app = QApplication.instance() or QApplication([])


class IcoadsStateTests(TestCase):
    def test_position_cache_invalidation_and_covered_pan(self):
        from dataclasses import replace
        from PySide6.QtQuick import QQuickItem
        from sailworks.core.mapviewport import MercatorProjection
        from sailworks.qt.gribviewport import ViewportCamera

        instant = datetime(2026, 10, 27, tzinfo=timezone.utc)
        reference = ViewportCamera(10, 20, 9, 900, 700, instant)
        reports = (ShipReport(10, 20, instant), ShipReport(10, 21, instant))
        parent = QQuickItem()
        item = IcoadsQuickItem(parent)
        item.set_camera(reference, reference=reference)
        item.set_reports(reports)
        original = MercatorProjection.convert_geographic_to_screen
        calls = []

        def project(view, point):
            calls.append(point)
            return original(view, point)

        with patch.object(MercatorProjection, "convert_geographic_to_screen", project):
            positions = item._positions()
            self.assertIs(item._positions(), positions)
            initial_count = len(calls)
            self.assertGreater(initial_count, 0)
            item.set_camera(replace(reference, center_lon=20.01), reference=reference)
            self.assertIs(item._positions(), positions)
            self.assertEqual(len(calls), initial_count)
            item.set_reports(reports)
            item._positions()
            self.assertEqual(len(calls), 2 * initial_count)
            for change in (
                {"center_lat": 11},
                {"center_lon": 21},
                {"zoom": 8},
                {"width": 800},
                {"height": 600},
            ):
                camera = replace(reference, **change)
                item.set_camera(camera, reference=camera)
                count = len(calls)
                item._positions()
                self.assertGreater(len(calls), count)
            item._padding += 1
            count = len(calls)
            item._positions()
            self.assertGreater(len(calls), count)
            count = len(calls)
            item.setWidth(item.width() + 1)
            item._positions()
            self.assertGreater(len(calls), count)
            count = len(calls)
            item.setHeight(item.height() + 1)
            item._positions()
            self.assertGreater(len(calls), count)

    def test_cached_positions_hover_and_paint_match_projection(self):
        from PySide6.QtCore import QPointF
        from unittest.mock import Mock

        instant = datetime(2026, 10, 27, tzinfo=timezone.utc)
        reports = tuple(
            ShipReport(lat, lon, instant)
            for lat, lon in ((12, 18), (15, 20), (-1, 0), (101, 1), (100, 100))
        )
        project = Mock(side_effect=lambda point: point.get_degrees())
        camera = SimpleNamespace(
            view=lambda: SimpleNamespace(convert_geographic_to_screen=project)
        )
        item = IcoadsQuickItem()
        item.setWidth(100)
        item.setHeight(100)
        item.set_reports(reports, camera)
        expected = ((12, 18, reports[0]), (15, 20, reports[1]), (100, 100, reports[4]))
        self.assertEqual(item._positions(), expected)
        self.assertIs(item._report_at(15, 20), reports[1])
        item.hoverMoveEvent(SimpleNamespace(position=lambda: QPointF(15, 20)))
        self.assertIs(item._selected_report, reports[1])
        item._selected_report = None
        painter = Mock()
        item.paint(painter)
        self.assertEqual(painter.drawEllipse.call_count, 3)
        self.assertEqual(project.call_count, len(reports))

    def test_covered_pan_does_not_repaint_unchanged_marker_surface(self):
        from PySide6.QtQuick import QQuickItem
        from sailworks.qt.gribviewport import ViewportCamera

        instant = datetime(2026, 10, 27, tzinfo=timezone.utc)
        reference = ViewportCamera(10, 20, 9, 900, 700, instant)
        current = ViewportCamera(10, 20.03, 9, 900, 700, instant)
        parent = QQuickItem()
        item = IcoadsQuickItem(parent)
        item.set_camera(reference, reference=reference)
        with patch.object(item, "update") as update:
            item.set_camera(current, reference=reference)
            update.assert_not_called()
            item.set_camera(current, reference=current)
            update.assert_called_once_with()

    def test_marker_entering_view_has_rendered_surface_coverage(self):
        from PySide6.QtCore import QPointF
        from PySide6.QtQuick import QQuickItem
        from sailworks.qt.gribviewport import ViewportCamera
        from sailworks.qt.viewporttransform import PanReference

        instant = datetime(2026, 10, 27, tzinfo=timezone.utc)
        reference = ViewportCamera(10, 20, 9, 900, 700, instant)
        current = ViewportCamera(10, 21, 9, 900, 700, instant)
        report = ShipReport(10, 22, instant)
        root = QQuickItem()
        parent = QQuickItem(root)
        item = IcoadsQuickItem(parent)
        item.set_camera(reference, reference=reference)
        item.set_reports((report,), reference)
        dx, dy = PanReference.from_camera(reference).translation_to(current)
        parent.setX(dx)
        parent.setY(dy)
        item.set_camera(current, reference=reference)
        x, y, _ = item._positions()[0]
        self.assertTrue(0 <= x <= item.width() and 0 <= y <= item.height())
        point = item.mapToItem(root, QPointF(x, y))
        self.assertTrue(0 <= point.x() <= current.width)
        self.assertIs(item._report_at(x, y), report)

    def test_marker_uses_shared_translation_once(self):
        from PySide6.QtCore import QPointF
        from PySide6.QtQuick import QQuickItem
        from sailworks.qt.gribviewport import ViewportCamera
        from sailworks.qt.viewporttransform import PanReference
        from sailworks.core.mapviewport import MapPoint

        time = datetime(2026, 10, 27, tzinfo=timezone.utc)
        reference = ViewportCamera(10, 20, 9, 900, 700, time)
        current = ViewportCamera(10, 20.03, 9, 900, 700, time)
        report = ShipReport(10, 20, time)
        root = QQuickItem()
        parent = QQuickItem(root)
        item = IcoadsQuickItem(parent)
        item.setWidth(900)
        item.setHeight(700)
        item.set_camera(reference, reference=reference)
        item.set_reports((report,), reference)
        dx, dy = PanReference.from_camera(reference).translation_to(current)
        parent.setX(dx)
        parent.setY(dy)
        item.set_camera(current, reference=reference)
        x, y, _ = item._positions()[0]
        scene_point = item.mapToItem(root, QPointF(x, y))
        expected_x, expected_y = current.view().convert_geographic_to_screen(
            MapPoint(report.latitude, report.longitude)
        )
        self.assertAlmostEqual(scene_point.x(), expected_x, delta=0.001)
        self.assertAlmostEqual(scene_point.y(), expected_y, delta=0.001)

        # The viewport has reset its parent reference to the current camera.
        parent.setX(0)
        parent.setY(0)
        item.set_camera(current, reference=current)
        x, y, _ = item._positions()[0]
        scene_point = item.mapToItem(root, QPointF(x, y))
        self.assertAlmostEqual(scene_point.x(), expected_x, delta=0.001)
        self.assertAlmostEqual(scene_point.y(), expected_y, delta=0.001)

    def test_markers_accept_map_viewport_screen_coordinate_tuples(self):
        from PySide6.QtCore import QPointF, Qt

        report = ShipReport(10, 20, datetime(2026, 10, 27, tzinfo=timezone.utc))
        view = SimpleNamespace(convert_geographic_to_screen=lambda _point: (12, 18))
        camera = SimpleNamespace(view=lambda: view)
        item = IcoadsQuickItem()
        item.setWidth(100)
        item.setHeight(100)
        item.set_reports((report,), camera)
        self.assertEqual(item._positions(), ((12, 18, report),))
        item.hoverMoveEvent(SimpleNamespace(position=lambda: QPointF(12, 18)))
        self.assertEqual(item.cursor().shape(), Qt.CursorShape.PointingHandCursor)
        self.assertIs(item._selected_report, report)
        item.hoverMoveEvent(SimpleNamespace(position=lambda: QPointF(50, 50)))
        self.assertNotEqual(item.cursor().shape(), Qt.CursorShape.PointingHandCursor)

    def test_marker_click_selects_report_for_visible_details_card(self):
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        report = ShipReport(
            10,
            20,
            datetime(2026, 10, 27, tzinfo=timezone.utc),
            wind_speed=5,
        )
        view = SimpleNamespace(convert_geographic_to_screen=lambda _point: (12, 18))
        item = IcoadsQuickItem()
        item.setWidth(100)
        item.setHeight(100)
        item.set_reports((report,), SimpleNamespace(view=lambda: view))
        event = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(12, 18),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        item.mousePressEvent(event)
        self.assertIs(item._selected_report, report)
        self.assertIn("Wind speed: 5 m/s", item._detail_lines(report))

    def test_refresh_button_keeps_size_and_label_while_showing_busy_state(self):
        button = BusyButton("Refresh reports")
        button.resize(130, 32)
        size = button.size()
        button.set_busy(True)
        self.assertEqual(button.text(), "Refresh reports")
        self.assertEqual(button.size(), size)
        self.assertTrue(button._animation.isActive())
        button.set_busy(False)
        self.assertFalse(button._animation.isActive())

    def test_quick_layer_is_independent_and_hides_when_closed(self):
        from PySide6.QtCore import QObject
        from PySide6.QtQuick import QQuickItem

        root = QQuickItem()
        layer = IcoadsQuickLayer(root, QObject())
        report = ShipReport(10, 20, datetime(2026, 10, 27, tzinfo=timezone.utc))
        layer.present((report,), True, None)
        self.assertTrue(layer.item.isVisible())
        layer.close()
        self.assertFalse(layer.item.isVisible())

    def test_parser_filters_nonships_duplicates_and_invalid_rows(self):
        dataset = SimpleNamespace(
            variables={
                "date": np.array([b"20261027", b"20261027", b"20261027", b"bad-date"]),
                "HR": np.array([6.5, 7.0, 8.0, 9.0]),
                "lat": np.array([12.0, 13.0, 14.0, 15.0]),
                "lon": np.array([235.0, 20.0, 30.0, 40.0]),
                "PT": np.array([5, 6, 1, 1]),
                "DUPS": np.array([0, 0, 3, 0]),
                "W": np.ma.array([4.0, 5.0, 6.0, 7.0], mask=[0, 0, 0, 1]),
            }
        )
        reports = parse_dataset(dataset)
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual(report.platform_type, 5)
        self.assertEqual(report.longitude, -125.0)
        self.assertEqual(
            report.observed_at,
            datetime(2026, 10, 27, 6, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(report.wind_speed, 4.0)
        self.assertIsNone(report.air_temperature)

    def test_report_measurements_only_include_available_fields_and_units(self):
        report = ShipReport(
            10,
            20,
            datetime(2026, 10, 27, tzinfo=timezone.utc),
            wind_speed=5.5,
            pressure=1012,
        )
        self.assertEqual(
            report.measurements,
            (("Wind speed", "5.5 m/s"), ("Sea-level pressure", "1012 hPa")),
        )

    def test_fetch_window_is_bounded(self):
        self.assertEqual(IcoadsState(days=-2).days, 1)
        self.assertEqual(IcoadsState(days=99).days, 7)

    def test_enable_starts_fetch_and_refresh_reports_loading(self):
        with patch.object(IcoadsState, "refresh") as refresh:
            state = IcoadsState()
            state.set_visible(True)
        refresh.assert_called_once_with()
        self.assertTrue(state.visible)

    def test_hidden_state_does_not_start_fetch(self):
        with patch.object(IcoadsState, "refresh") as refresh:
            state = IcoadsState()
            state.set_visible(False)
        refresh.assert_not_called()
