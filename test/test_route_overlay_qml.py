"""The route overlay's QML property must not be shadowed by a component id."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickView

from sailworks.qt.mapstate import MapStateMixin
from sailworks.qt.viewportmap import ViewportMap

SCENE = (
    Path(__file__).resolve().parents[1] / "sailworks/qt/shaders/ViewportChartScene.qml"
)


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    yield QGuiApplication.instance() or QGuiApplication([])


def test_route_canvas_does_not_shadow_its_routes_property(app):
    """A Canvas ``id: routes`` hides the ``routes`` property inside onPaint.

    QML ids take precedence over properties in the component scope, so the
    route-drawing block read the Canvas item instead of the routes array and
    never drew saved routes.
    """
    source = SCENE.read_text()
    assert "id: routes\n" not in source
    assert "property var routes: []" in source

    view = QQuickView()
    try:
        view.setSource(QUrl.fromLocalFile(str(SCENE)))
        assert not view.errors(), view.errors()
        root = view.rootObject()
        assert root is not None
        overlay = root.findChild(QObject, "routeOverlay")
        assert overlay is not None

        routes = [
            {
                "name": "test-route",
                "points": [[48.0, -124.0, 4.0], [48.1, -123.9, 5.0]],
                "highlight": False,
                "boat": None,
            }
        ]
        assert overlay.setProperty("routes", routes)
        assert overlay.property("routes") == routes
    finally:
        view.close()
        view.releaseResources()


def test_state_change_prefers_a_forced_overlay_refresh():
    host = SimpleNamespace(refresh_route_overlay=Mock(), publish_route_overlay=Mock())

    MapStateMixin._publish_route_overlay(host)

    host.refresh_route_overlay.assert_called_once_with()
    host.publish_route_overlay.assert_not_called()


def test_refresh_route_overlay_clears_the_identity_cache():
    host = SimpleNamespace(
        _viewport_overlay_cache=object(), publish_route_overlay=Mock()
    )

    ViewportMap.refresh_route_overlay(host)

    assert host._viewport_overlay_cache is None
    host.publish_route_overlay.assert_called_once_with(force=True)
