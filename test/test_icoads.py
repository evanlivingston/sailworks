"""Focused checks for the isolated ICOADS display state."""

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import numpy as np

from gweatherrouting.core.marine.icoads import ShipReport, parse_dataset
from gweatherrouting.qt.icoadsquick import IcoadsQuickItem, IcoadsQuickLayer
from gweatherrouting.qt.icoadsstate import IcoadsState
from gweatherrouting.qt.marinepanel import BusyButton


def setUpModule():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    global app
    app = QApplication.instance() or QApplication([])


class IcoadsStateTests(TestCase):
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
