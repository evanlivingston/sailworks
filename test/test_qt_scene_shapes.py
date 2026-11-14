# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

For detail about GNU see <http://www.gnu.org/licenses/>.
"""

"""Scene shapes at the Qt seams: window deletion and routing-request wiring."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from sailworks.core.geo.mapshapes import KIND_AVOID, KIND_NOTE
from sailworks.core.geo.shapecollection import Shape
from sailworks.core.scene import KIND_SHAPE
from sailworks.qt.routingpanel import RoutingPanel
from sailworks.qt.scenepanel import ScenePanel
from sailworks.qt.window import PreviewWindow
from test.test_qt_routing_native_backend import FakeController as RoutingController
from test.test_qt_scenepanel import (
    FakeController,
    FakeLibrary,
    FakeSession,
    FakeShapeManager,
    action_named,
    context_menu_actions,
)

# A clockwise square accepted by the shape validator.
SQUARE = ((0.0, 0.0), (0.0, 2.0), (2.0, 2.0), (2.0, 0.0))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class FakeNavigationManager(list):
    def get_active(self):
        return None


def scene_window_stub(tmp_path, shapes):
    """A window-shaped object carrying only what ``_scene_delete`` touches."""
    manager = FakeShapeManager(shapes)
    core = SimpleNamespace(
        trackManager=FakeNavigationManager(),
        routingManager=FakeNavigationManager(),
        poiManager=FakeNavigationManager(),
        shapeManager=manager,
    )
    panel = ScenePanel(
        FakeController(core, FakeLibrary(), FakeSession()),
        scene_path=tmp_path / "scene.json",
    )
    stub = SimpleNamespace(
        controller=SimpleNamespace(core=core),
        scene_panel=panel,
        undo_stack=None,
        map=SimpleNamespace(navigation=None),
        log_map=SimpleNamespace(),
    )
    refreshes = []

    def refresh():
        refreshes.append(True)
        panel.refresh()

    stub._navigation_refresh = refresh
    stub._exit_track_edit = lambda: None
    return stub, manager, panel, refreshes


def choose_endpoints(panel):
    """Select map start/destination without a chart or POI index."""
    panel.map_start = (48.0, -123.5)
    panel.start_mode.addItem("Map start", panel.map_start)
    panel.start_mode.setCurrentIndex(panel.start_mode.count() - 1)
    panel.destination.addItem("Destination", (48.4, -123.1))
    panel.destination.setCurrentIndex(panel.destination.count() - 1)


def test_scene_delete_removes_shape_and_refreshes(app, tmp_path):
    shape = Shape("Reef", SQUARE, kind=KIND_AVOID)
    stub, manager, panel, refreshes = scene_window_stub(tmp_path, [shape])
    assert panel.item_row(KIND_SHAPE, shape.uid) is not None

    PreviewWindow._scene_delete(stub, [("item", KIND_SHAPE, shape.uid)])

    assert list(manager) == []
    assert manager.saves == 1
    assert refreshes == [True]
    app.processEvents()
    assert panel.item_row(KIND_SHAPE, shape.uid) is None


def test_scene_delete_leaves_other_shapes_alone(app, tmp_path):
    kept = Shape("Reef", SQUARE, kind=KIND_AVOID)
    gone = Shape("Sand", SQUARE, kind=KIND_NOTE)
    stub, manager, _panel, refreshes = scene_window_stub(tmp_path, [kept, gone])

    PreviewWindow._scene_delete(stub, [("item", KIND_SHAPE, gone.uid)])

    assert list(manager) == [kept]
    assert refreshes == [True]


def test_native_request_carries_only_enabled_avoid_shapes(app):
    controller = RoutingController()
    avoid = Shape("Reef", SQUARE, kind=KIND_AVOID, note="coral")
    disabled = Shape("Off", SQUARE, kind=KIND_AVOID, enabled=False)
    note = Shape("Note", SQUARE, kind=KIND_NOTE)
    controller.core.shapeManager = FakeShapeManager([avoid, disabled, note])
    panel = RoutingPanel(controller)
    choose_endpoints(panel)

    request, _scenarios = panel._request("native")

    assert len(request.map_shapes) == 1
    assert request.map_shapes[0].id == avoid.uid
    assert request.map_shapes[0].kind == KIND_AVOID
    # The snapshot is immutable: a later edit cannot change a live request.
    avoid.enabled = False
    assert request.map_shapes[0].enabled is True
    # A legacy request never carries shapes, whatever the panel selection.
    legacy_request, _legacy_scenarios = panel._request("legacy")
    assert legacy_request.map_shapes == ()


def test_legacy_calculation_refuses_enabled_avoid_shapes(app):
    controller = RoutingController()
    controller.core.shapeManager = FakeShapeManager(
        [Shape("Reef", SQUARE, kind=KIND_AVOID)]
    )
    panel = RoutingPanel(controller)
    choose_endpoints(panel)
    assert panel.backend.currentData() == "legacy"

    panel._calculate()

    assert controller.routing.calls == []
    assert "Native backend" in panel.status.text()


def test_legacy_calculation_runs_without_enabled_avoid_shapes(app):
    controller = RoutingController()
    controller.core.shapeManager = FakeShapeManager(
        [Shape("Note", SQUARE, kind=KIND_NOTE)]
    )
    panel = RoutingPanel(controller)
    choose_endpoints(panel)

    panel._calculate()

    assert len(controller.routing.calls) == 1
    request, _scenarios, backend = controller.routing.calls[0]
    assert backend == "legacy"
    assert request.map_shapes == ()


class FakeViewportCanvas:
    """Counts the shape-editing requests a viewport receives."""

    def __init__(self, accepts=True):
        self.accepts = accepts
        self.edit_uids = []

    def begin_shape_edit(self, uid):
        self.edit_uids.append(uid)
        return self.accepts


class FakeViews:
    def __init__(self, current):
        self.current = current
        self.shown = []

    def currentWidget(self):
        return self.current

    def setCurrentWidget(self, widget):
        self.current = widget
        self.shown.append(widget)


def shape_edit_stub(tmp_path, shapes, current="map", accepts=True):
    """A window-shaped object with just the maps and views the route needs."""
    map_canvas = FakeViewportCanvas(accepts)
    log_canvas = FakeViewportCanvas()
    logs_view = SimpleNamespace()
    polar_workspace = SimpleNamespace()
    views = FakeViews(
        {"map": map_canvas, "logs": logs_view, "polar": polar_workspace}[current]
    )
    core = SimpleNamespace(
        trackManager=FakeNavigationManager(),
        routingManager=FakeNavigationManager(),
        poiManager=FakeNavigationManager(),
        shapeManager=FakeShapeManager(shapes),
    )
    panel = ScenePanel(
        FakeController(core, FakeLibrary(), FakeSession()),
        scene_path=tmp_path / "scene.json",
    )
    focused = []
    stub = SimpleNamespace(
        controller=SimpleNamespace(core=core),
        scene_panel=panel,
        views=views,
        map=map_canvas,
        log_map=log_canvas,
        logs_view=logs_view,
        polar_workspace=polar_workspace,
        focused=focused,
    )
    stub._active_map = lambda: (
        log_canvas if views.currentWidget() is logs_view else map_canvas
    )
    stub._focus_map_surface = lambda: focused.append(True)
    return stub, map_canvas, log_canvas


def test_shape_edit_routes_uid_to_the_visible_chart_map(app, tmp_path):
    shape = Shape("Reef", SQUARE, kind=KIND_AVOID)
    stub, map_canvas, log_canvas = shape_edit_stub(tmp_path, [shape])
    stub.scene_panel.shape_edit_requested.connect(
        lambda uid: PreviewWindow._scene_edit_shape_vertices(stub, uid)
    )

    row = stub.scene_panel.item_row(KIND_SHAPE, shape.uid)
    actions = context_menu_actions(stub.scene_panel, row)
    action_named(actions, "Edit vertices").trigger()

    assert map_canvas.edit_uids == [shape.uid]
    assert log_canvas.edit_uids == []
    assert stub.focused == [True]


def test_shape_edit_uses_the_log_map_only_while_logs_are_visible(app, tmp_path):
    shape = Shape("Reef", SQUARE, kind=KIND_AVOID)
    stub, map_canvas, log_canvas = shape_edit_stub(tmp_path, [shape], current="logs")

    PreviewWindow._scene_edit_shape_vertices(stub, shape.uid)

    assert log_canvas.edit_uids == [shape.uid]
    assert map_canvas.edit_uids == []


def test_shape_edit_brings_the_chart_map_into_view_from_polars(app, tmp_path):
    shape = Shape("Reef", SQUARE, kind=KIND_AVOID)
    stub, map_canvas, _log_canvas = shape_edit_stub(tmp_path, [shape], current="polar")

    PreviewWindow._scene_edit_shape_vertices(stub, shape.uid)

    assert stub.views.shown == [map_canvas]
    assert map_canvas.edit_uids == [shape.uid]


def test_shape_edit_keeps_the_surface_when_the_viewport_refuses(app, tmp_path):
    shape = Shape("Reef", SQUARE, kind=KIND_AVOID)
    stub, map_canvas, _log_canvas = shape_edit_stub(tmp_path, [shape], accepts=False)

    PreviewWindow._scene_edit_shape_vertices(stub, shape.uid)

    assert map_canvas.edit_uids == [shape.uid]
    assert stub.focused == []
