"""Selecting a saved route with partial points must not crash the panel."""

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QTableWidget

from sailworks.qt.navigationpanel import NavigationPanel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _panel(route):
    return SimpleNamespace(
        route=route,
        route_points=QTableWidget(),
        point_rounding=None,
    )


def test_refresh_route_points_skips_partial_points(app):
    route = SimpleNamespace(
        points=[
            (48.2, -123.7),
            (48.4, -123.5),
        ]
    )
    panel = _panel(route)

    NavigationPanel.refresh_route_points(panel)

    assert panel.route_points.rowCount() == 0


def test_refresh_route_points_keeps_complete_points(app):
    route = SimpleNamespace(
        points=[
            (49.0, -125.0, "2026-01-01 00:00:00", 0, 0, 5.0, 90.0),
            (49.0, -124.8, "2026-01-01 01:00:00", 0, 0, 5.0, 90.0),
        ]
    )
    panel = _panel(route)

    NavigationPanel.refresh_route_points(panel)

    assert panel.route_points.rowCount() == 2
