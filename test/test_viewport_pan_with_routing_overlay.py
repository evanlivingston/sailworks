"""Panning must survive a displayed native routing result (#181).

The retained routing item does not accept mouse buttons. The drag was lost
later, in the presenter's input filter: a left press over a hovered trace was
consumed to pin it, so the viewport never started its pan gesture. These
checks drive the real viewport widget and the real presenter filter.
"""

from __future__ import annotations

from pathlib import Path
from types import MethodType, SimpleNamespace

from test.test_gribviewport import _camera, _paths, _wait_ready
from test.test_weather_staging import gribs  # noqa: F401
from test.test_viewport_routing_overlay import _result

ROOT = Path(__file__).resolve().parents[1]


def _native_routing():
    from sailworks.core.marine.routingrecord import Point, Trace
    from sailworks.core.marine.routingselection import RoutingSelection
    from sailworks.rendering.routingoverlay import RoutingOverlayModel

    trace = Trace("t-0", Point(0.5, 0.5), (Point(0.5, 0.5), Point(0.55, 0.55)))
    result = _result(layer_count=1, traces=(trace,))
    model = RoutingOverlayModel(result)
    selection = RoutingSelection(result)
    return model, selection


def _viewport(gribs):
    from PySide6.QtWidgets import QApplication, QWidget

    from sailworks.qt.gribviewport import (
        GribViewportModel,
        _viewport_widget_class,
    )
    from sailworks.qt.viewportmap import ViewportMap
    from sailworks.qt.weatherpresenter import WeatherPresenter

    app = QApplication.instance() or QApplication([])
    paths, _ = _paths(gribs)
    model = GribViewportModel(paths)
    camera = _camera()
    _wait_ready(model, camera=camera)
    widget = _viewport_widget_class()(model)
    widget.resize(camera.width, camera.height)
    widget.show()
    widget.set_camera(camera)
    app.processEvents()

    routing, selection = _native_routing()
    # The pointer has previewed the trace; the retained overlay is active and
    # the canvas overlay does not duplicate it.
    routing.set_hover_trace("t-0")
    canvas = SimpleNamespace(
        move_callback=None,
        _desktop_viewport=True,
        native_routing=routing,
        controller=SimpleNamespace(routing=SimpleNamespace(native_selection=selection)),
        _retained_active=True,
        _apply_retained_highlight=lambda: None,
        _hover=lambda _position: None,
    )
    canvas.pin_routing_trace = MethodType(ViewportMap.pin_routing_trace, canvas)

    holder = QWidget()
    presenter = WeatherPresenter(holder)
    presenter.canvas = canvas
    presenter.widget = widget
    widget.installEventFilter(presenter)
    return app, model, widget, holder, presenter, routing, selection


def test_viewport_pans_with_a_native_routing_overlay_attached(gribs):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    app, model, widget, holder, presenter, routing, selection = _viewport(gribs)
    try:
        before = (widget._camera.center_lat, widget._camera.center_lon)
        QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(200, 200))
        QTest.mouseMove(widget, QPoint(280, 260), delay=5)
        QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(280, 260))
        app.processEvents()
        after = (widget._camera.center_lat, widget._camera.center_lon)

        assert after != before, "the drag never reached the viewport pan path"
        assert selection.trace_id is None, "a drag must not pin the trace"
        assert routing.hover_trace_id == "t-0", "hover preview was lost"
    finally:
        widget.removeEventFilter(presenter)
        widget.close()
        holder.close()
        model.close()
        app.processEvents()


def test_a_click_still_pins_the_hovered_trace_and_does_not_pan(gribs):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    app, model, widget, holder, presenter, routing, selection = _viewport(gribs)
    try:
        before = (widget._camera.center_lat, widget._camera.center_lon)
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(200, 200))
        app.processEvents()
        after = (widget._camera.center_lat, widget._camera.center_lon)

        assert selection.trace_id == "t-0"
        assert routing.hover_trace_id == "t-0"
        assert after == before, "a click-to-pin must not move the camera"
    finally:
        widget.removeEventFilter(presenter)
        widget.close()
        holder.close()
        model.close()
        app.processEvents()


def test_the_scene_keeps_the_retained_overlay_input_transparent():
    """Nothing above the retained loader can take the viewport drag."""
    from PySide6.QtCore import QUrl
    from PySide6.QtQuick import QQuickItem, QQuickView
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    view = QQuickView()
    view.setSource(
        QUrl.fromLocalFile(str(ROOT / "sailworks/qt/shaders/ViewportChartScene.qml"))
    )
    try:
        assert view.status() == QQuickView.Status.Ready, view.errors()
        root = view.rootObject()
        loader = root.findChild(QQuickItem, "retainedRoutingLoader")
        assert loader is not None
        assert loader.z() == 20
        assert loader.width() == root.width()
        assert loader.height() == root.height()
        assert not loader.isVisible()

        def covers_viewport(item):
            return item.width() >= root.width() and item.height() >= root.height()

        def offenders(item):
            found = []
            for child in item.childItems():
                if (
                    child.isVisible()
                    and child.z() >= 20
                    and covers_viewport(child)
                    and "MouseArea" in child.metaObject().className()
                ):
                    found.append(child)
                found.extend(offenders(child))
            return found

        assert offenders(root) == []
    finally:
        view.close()
        app.processEvents()

    source = (ROOT / "sailworks/qt/shaders/RoutingRetainedOverlay.qml").read_text()
    assert "RoutingRetainedItem" in source
    assert "anchors.fill: parent" in source
    assert "MouseArea" not in source

    item = (ROOT / "native/routing/qt/routingretaineditem.cpp").read_text()
    assert "setAcceptedMouseButtons(Qt::NoButton)" in item
    assert "setAcceptHoverEvents(false)" in item
