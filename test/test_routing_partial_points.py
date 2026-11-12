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


def _window(route):
    """Minimal window state for the saved-route selection path."""
    from types import SimpleNamespace

    from sailworks.qt.window import PreviewWindow

    calls = []

    class FakeMap:
        def __init__(self):
            self.navigation = SimpleNamespace(hl_routing=None)

        def set_route(self, coordinates, speeds):
            calls.append((coordinates, speeds))

    window = SimpleNamespace(
        map=FakeMap(),
        _sync_route_isochrones=lambda: None,
    )
    PreviewWindow._navigation_route_selected(window, route)
    return calls


def test_saved_route_with_partial_points_does_not_crash(app):
    route = SimpleNamespace(
        name="gone",
        points=[
            (48.2, -123.7),
            (48.4, -123.5),
        ],
    )

    # The window selects a saved route at startup; coordinate-only points have
    # no speed and must be skipped instead of raising IndexError.
    assert _window(route) == [((), ())]


def test_saved_route_skips_only_the_partial_points(app):
    route = SimpleNamespace(
        name="mixed",
        points=[
            (49.0, -125.0, "2026-01-01 00:00:00", 0, 0, 5.0, 90.0),
            (49.0, -124.9),
            (49.0, -124.8, "2026-01-01 01:00:00", 0, 0, 6.0, 90.0),
        ],
    )

    coordinates, speeds = _window(route)[0]

    assert coordinates == ((49.0, -125.0), (49.0, -124.8))
    assert speeds == (5.0, 6.0)
