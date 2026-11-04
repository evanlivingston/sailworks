"""Reference-local retained imagery and fill across pan and padded rebases."""

from dataclasses import replace
from types import MethodType, SimpleNamespace

import pytest
from PySide6.QtCore import QRectF

from sailworks.qt.gribviewport import _grid_rect, _viewport_widget_class
from sailworks.qt.viewporttransform import PanReference, PaddedPanWindow
from test.test_gribviewport import _camera, _field


class Item:
    def __init__(self):
        self.values = {"gridX": 0.0, "gridY": 0.0}
        self.writes = []
        self.visible = True

    def setProperty(self, name, value):
        self.values[name] = value
        self.writes.append((name, value))
        return True

    def property(self, name):
        return self.values.get(name)

    def setVisible(self, visible):
        self.visible = visible


def _widget(camera):
    cls = _viewport_widget_class()
    reference = PanReference.from_camera(camera)
    widget = SimpleNamespace(
        _camera=camera,
        _place_world=lambda camera: None,
        _map_anchor_camera=camera,
        _map_pan_reference=reference,
        _map_pan_window=PaddedPanWindow(reference, 120),
        _map_content=Item(),
        _base=Item(),
        _environment=Item(),
        _fill=Item(),
        _fill_back=Item(),
        _base_provider=SimpleNamespace(publish=lambda *args: "image://base/1"),
        _environment_provider=SimpleNamespace(
            publish=lambda *args: "image://environment/1"
        ),
        _base_key=None,
        _environment_key=None,
        _base_placement=None,
        _environment_placement=None,
        _fill_placements={},
        devicePixelRatio=lambda: 1.0,
    )
    for name in (
        "_place_retained_image",
        "_rebase_retained_imagery",
        "_place_fill_rect",
        "_preview_retained_imagery",
    ):
        setattr(widget, name, MethodType(getattr(cls, name), widget))
    return cls, widget


@pytest.mark.parametrize("role", ("base", "environment"))
def test_intermediate_zoom_reprojects_cached_image_before_host_callback(role):
    camera = _camera()
    cls, widget = _widget(camera)
    image = SimpleNamespace(
        cacheKey=lambda: 21,
        deviceIndependentSize=lambda: SimpleNamespace(
            width=lambda: 800, height=lambda: 620
        ),
    )
    target = QRectF(-80, -70, 800, 620)
    source = (camera.center_lat, camera.center_lon, camera.zoom)
    publish = getattr(cls, f"set_{role}")
    publish(widget, image, target, source)
    item = getattr(widget, f"_{role}")
    item.writes.clear()

    # The producer has not published a target for this intermediate camera.
    # Zoom is off-centre and follows an earlier pan of the same image.
    zoomed = replace(
        camera, center_lon=camera.center_lon + 0.015, zoom=camera.zoom + 0.35
    )
    widget._camera = zoomed
    cls._update_shared_map_pan(widget, zoomed)
    widget._preview_retained_imagery(zoomed)
    view = zoomed.view()
    scale = 2.0 ** (zoomed.zoom - camera.zoom)
    geo_x = view._lon_to_x(camera.center_lon) - view._lon_to_x(zoomed.center_lon)
    center_x = item.values["imageX"] + item.values["imageWidth"] / 2
    assert center_x == pytest.approx(zoomed.width / 2 + geo_x, abs=0.001)
    assert item.values["imageWidth"] == pytest.approx(800 * scale)

    # A later native-scale producer replacement adopts the same geographic
    # centre; the preview cannot accumulate an extra zoom or pan translation.
    replacement = SimpleNamespace(
        cacheKey=lambda: 22,
        deviceIndependentSize=lambda: SimpleNamespace(
            width=lambda: 800, height=lambda: 620
        ),
    )
    publish(
        widget,
        replacement,
        QRectF((zoomed.width - 800) / 2, (zoomed.height - 620) / 2, 800, 620),
        (zoomed.center_lat, zoomed.center_lon, zoomed.zoom),
    )
    assert item.values["imageX"] + item.values["imageWidth"] / 2 == pytest.approx(
        zoomed.width / 2, abs=0.001
    )


@pytest.mark.parametrize("role", ("base", "environment"))
def test_covered_pan_only_moves_parent_and_rebase_keeps_image_anchored(role):
    camera = _camera()
    cls, widget = _widget(camera)
    item = getattr(widget, f"_{role}")
    image = SimpleNamespace(cacheKey=lambda: 21)
    target = QRectF(-80, -70, 800, 620)
    publish = getattr(cls, f"set_{role}")
    publish(widget, image, target)
    item.writes.clear()

    moved = replace(camera, center_lon=camera.center_lon + 0.04)
    dx, dy = widget._map_pan_reference.translation_to(moved)
    widget._camera = moved
    assert not cls._update_shared_map_pan(widget, moved)
    publish(widget, image, target.translated(dx, dy))
    assert item.writes == []
    assert widget._map_content.values["x"] == pytest.approx(dx)

    # A padded-window rebase compensates every retained local anchor before
    # resetting the parent, even if no replacement image has arrived.
    far = replace(camera, center_lon=camera.center_lon + 0.5)
    old_dx, old_dy = widget._map_pan_reference.translation_to(far)
    widget._camera = far
    assert cls._update_shared_map_pan(widget, far)
    assert item.values["imageX"] == pytest.approx(target.x() + old_dx)
    assert item.values["imageY"] == pytest.approx(target.y() + old_dy)
    assert widget._map_content.values["x"] == 0
    item.writes.clear()
    publish(widget, image, target.translated(old_dx, old_dy))
    assert item.writes == []


def test_fill_front_and_pending_rebase_without_coverage_updates():
    camera = _camera()
    cls, widget = _widget(camera)
    grid = _field().grid
    rect = _grid_rect(camera.view(), grid.lattice)
    for item in (widget._fill, widget._fill_back):
        widget._place_fill_rect(item, rect)
        item.writes.clear()

    moved = replace(camera, center_lon=camera.center_lon + 0.04)
    widget._camera = moved
    cls._update_shared_map_pan(widget, moved)
    for item in (widget._fill, widget._fill_back):
        widget._place_fill_rect(item, _grid_rect(camera.view(), grid.lattice))
        assert item.writes == []

    far = replace(camera, center_lon=camera.center_lon + 0.5)
    dx, dy = widget._map_pan_reference.translation_to(far)
    widget._camera = far
    cls._update_shared_map_pan(widget, far)
    for item in (widget._fill, widget._fill_back):
        assert item.values["gridX"] == pytest.approx(rect.x() + dx)
        assert item.values["gridY"] == pytest.approx(rect.y() + dy)
        item.writes.clear()
        widget._place_fill_rect(item, _grid_rect(far.view(), grid.lattice))
        assert item.writes == []


def test_dpr_invalidation_keeps_retained_image_and_fill_geography():
    camera = _camera()
    cls, widget = _widget(camera)
    image = SimpleNamespace(cacheKey=lambda: 21)
    target = QRectF(-80, -70, 800, 620)
    cls.set_base(widget, image, target)
    grid = _field().grid
    rect = _grid_rect(camera.view(), grid.lattice)
    widget._place_fill_rect(widget._fill, rect)

    panned = replace(camera, center_lon=camera.center_lon + 0.04)
    widget._camera = panned
    cls._update_shared_map_pan(widget, panned)
    dx, dy = widget._map_pan_reference.translation_to(panned)
    widget.devicePixelRatio = lambda: 2.0
    assert cls._update_shared_map_pan(widget, panned)
    assert widget._map_content.values["x"] == 0
    assert widget._base.values["imageX"] == pytest.approx(target.x() + dx)
    assert widget._base.values["imageY"] == pytest.approx(target.y() + dy)
    assert widget._fill.values["gridX"] == pytest.approx(rect.x() + dx)
    assert widget._fill.values["gridY"] == pytest.approx(rect.y() + dy)
