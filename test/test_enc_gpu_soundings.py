"""Focused tests for GPU sounding labels in ``sailworks.qt.encgpu``.

These use the real PySide6 scene-graph objects and an injected texture factory:
they assert the glyph quads, node order, per-camera gating and the texture
lifecycle.  They do not render or measure presentation.
"""

from __future__ import annotations

import ctypes
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

# The textured label node is proven across build/present/teardown cycles: the
# child is detached with ``removeChildNode`` and its texture owned by exactly
# one material, so these run on the default item again.

from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQuick import (  # noqa: E402
    QSGMaterial,
    QSGTextureMaterial,
)

from sailworks.qt import encgpu  # noqa: E402
from sailworks.rendering.encview import ViewScene  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QGuiApplication.instance()
    if app is None:
        try:
            app = QGuiApplication([])
        except Exception as exc:  # pragma: no cover - headless without a platform
            pytest.skip(f"Qt platform unavailable: {exc}")
    return app


def _view_scene(soundings) -> ViewScene:
    return ViewScene(
        positions=np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0]], dtype=np.float64),
        parts=(),
        cells=(),
        unsupported=(),
        skipped=(),
        soundings=np.asarray(soundings, dtype=np.float64).reshape(-1, 3),
    )


def _read_text(geometry) -> np.ndarray:
    count = geometry.vertexCount()
    data = geometry.vertexData()
    address = data if isinstance(data, int) else int(data)
    vertices = np.empty(count, dtype=encgpu.TEXT_VERTEX_DTYPE)
    if count:
        ctypes.memmove(vertices.ctypes.data, address, vertices.nbytes)
    return vertices


def _item(factory):
    item = encgpu.EncMeshItem(colored=True, texture_factory=factory)
    item.setWidth(220.0)
    item.setHeight(120.0)
    return item


def test_item_builds_textured_glyph_quads_after_the_triangle_node(qt_app):
    calls = []

    def factory(image):
        calls.append(image.size())
        return None

    item = _item(factory)
    item.set_scale(50.0)
    item.set_view_scene(_view_scene([[10.0, 10.0, 3.5], [150.0, 10.0, 9.9]]))

    node = item.updatePaintNode(None, None)

    assert node.childCount() == 1
    assert item.text_node is not None
    assert [label.text for label in item.labels] == ["3.5", "9.9"]
    assert item.text_upload_count == 1
    assert calls == [item.atlas.image.size()]
    assert isinstance(item._text_material, QSGTextureMaterial)
    assert item._text_material.flags() & QSGMaterial.Flag.Blending

    vertices = _read_text(item._text_geometry)
    assert vertices.shape[0] == 6 * (len("3.5") + len("9.9"))
    assert np.all(vertices["u"] >= 0.0) and np.all(vertices["u"] <= 1.0)
    assert np.all(vertices["v"] >= 0.0) and np.all(vertices["v"] <= 1.0)

    first = item.labels[0]
    assert vertices["x"][0] == pytest.approx(first.x)
    assert vertices["y"][0] == pytest.approx(first.y - item.atlas.ascent)


def test_unchanged_frame_does_not_reupload_or_recreate_the_texture(qt_app):
    calls = []

    def factory(image):
        calls.append(image.size())
        return None

    item = _item(factory)
    item.set_scale(50.0)
    item.set_view_scene(_view_scene([[10.0, 10.0, 3.5]]))
    node = item.updatePaintNode(None, None)

    item.updatePaintNode(node, None)
    item.updatePaintNode(node, None)

    assert item.text_upload_count == 1
    assert len(calls) == 1
    assert item.atlas is not None


def test_dedupe_is_per_camera_not_baked_into_the_scene(qt_app):
    calls = []

    def factory(image):
        calls.append(image.size())
        return None

    item = _item(factory)
    item.set_scale(50.0)
    item.set_view_scene(_view_scene([[10.0, 10.0, 3.5], [40.0, 10.0, 4.5]]))
    node = item.updatePaintNode(None, None)
    assert len(item.labels) == 2

    # The same two source points project into one 32x16 box under this camera.
    item.set_projection(lambda source: source * 0.1)
    assert item.updatePaintNode(node, None) is node

    assert [label.text for label in item.labels] == ["3.5"]
    assert item.text_upload_count == 2
    assert len(calls) == 1  # same atlas, same texture


def test_scale_gate_hides_and_restores_without_a_new_texture(qt_app):
    calls = []

    def factory(image):
        calls.append(image.size())
        return None

    item = _item(factory)
    item.set_view_scene(_view_scene([[10.0, 10.0, 3.5]]))
    node = item.updatePaintNode(None, None)
    assert node.childCount() == 1

    item.set_scale(150.0)
    item.updatePaintNode(node, None)
    assert item.labels == ()
    assert item.text_node is None
    assert node.childCount() == 0

    item.set_scale(50.0)
    item.updatePaintNode(node, None)
    assert [label.text for label in item.labels] == ["3.5"]
    assert node.childCount() == 1
    assert len(calls) == 1


def test_teardown_and_reattach_rebuilds_text_node_and_texture(qt_app):
    calls = []

    def factory(image):
        calls.append(image.size())
        return None

    item = _item(factory)
    item.set_view_scene(_view_scene([[10.0, 10.0, 3.5]]))
    node = item.updatePaintNode(None, None)
    first_geometry = item._text_geometry

    item.releaseResources()
    rebuilt = item.updatePaintNode(None, None)

    assert rebuilt is not node
    assert rebuilt.childCount() == 1
    assert item._text_geometry is not first_geometry
    assert len(calls) == 2
    assert item.text_upload_count == 2


def test_scene_without_soundings_creates_no_text_node_or_texture(qt_app):
    calls = []

    def factory(image):  # pragma: no cover - must not be called
        calls.append(image.size())
        return None

    item = _item(factory)
    item.set_view_scene(_view_scene([]))

    node = item.updatePaintNode(None, None)

    assert node.childCount() == 0
    assert item.text_node is None
    assert calls == []
