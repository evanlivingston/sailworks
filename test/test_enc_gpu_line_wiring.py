"""Wiring of the flag-gated GPU ENC line trial.

The trial has two failure modes the default process must never meet: an
unconditional QML import of a C++ module that may not be built, and a write to
an item that does not exist.  These tests pin both: the scene QML carries no
``SailworksRouting`` import, creation is a no-op when the module is absent, and
the payload/camera/pan writes are gated by the flag and match the packed
``LineSegments`` bytes exactly.

The layer itself lives in :mod:`sailworks.qt.encgpu_lines`; the viewport only
owns one instance of it.
"""

from types import SimpleNamespace

import numpy as np
import pytest

from sailworks.qt import encgpu_lines
from sailworks.rendering.vectordrawer import encmesh

FLAG = encgpu_lines.ENV_ENC_GPU_LINES


@pytest.fixture
def gpu_lines_available():
    """Report a successfully created native item for one test only."""
    encmesh.set_gpu_lines_available(True)
    try:
        yield
    finally:
        encmesh.set_gpu_lines_available(False)


class _Signal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


class FakeItem:
    """The QQuickItem surface the trial writes to."""

    def __init__(self):
        self.writes = []
        self.visible = False
        self.x = 0.0
        self.y = 0.0
        self.width = 0.0
        self.height = 0.0
        self.parent_item = None
        self._properties = {}

    def setProperty(self, name, value):
        self.writes.append((name, value))
        self._properties[name] = value

    def property(self, name):
        return self._properties.get(name)

    def setVisible(self, value):
        self.visible = bool(value)

    def isVisible(self):
        return self.visible

    def setX(self, value):
        self.x = float(value)

    def setY(self, value):
        self.y = float(value)

    def setWidth(self, value):
        self.width = float(value)

    def setHeight(self, value):
        self.height = float(value)

    def setParentItem(self, parent):
        self.parent_item = parent

    def parentItem(self):
        return self.parent_item


class FakeMapContent:
    """``mapContent``: a pan translation the scene item inherits."""

    def __init__(self):
        self._x = 0.0
        self._y = 0.0
        self.xChanged = _Signal()
        self.yChanged = _Signal()

    def x(self):
        return self._x

    def y(self):
        return self._y

    def move(self, x, y):
        self._x = x
        self._y = y
        self.xChanged.emit()
        self.yChanged.emit()


class FakeContainer(FakeItem):
    def __init__(self, parent=None):
        super().__init__()
        self.parent_item = parent
        self.widthChanged = _Signal()
        self.heightChanged = _Signal()


def _segments():
    return encmesh.LineSegments(
        endpoints=np.array([[0.0, 0.0, 1.0, 1.0]], dtype=np.float32),
        widths=np.array([2.5], dtype=np.float32),
        colours=np.array([[255, 0, 0, 180]], dtype=np.uint8),
        dash_on=np.array([3.0], dtype=np.float32),
        dash_off=np.array([3.0], dtype=np.float32),
        phase=np.array([0.0], dtype=np.float32),
    )


def _layer(item=None):
    """A layer around an already-built item, as the viewport would hold it."""
    return encgpu_lines.EncGpuLineLayer(item)


def _camera():
    return SimpleNamespace(center_lat=37.5, center_lon=-122.25, zoom=9.75)


def test_scene_qml_has_no_unconditional_routing_import():
    from pathlib import Path
    import re

    qml = (
        Path(__file__).resolve().parents[1]
        / "sailworks"
        / "qt"
        / "shaders"
        / "ViewportChartScene.qml"
    ).read_text()
    # A comment may name the module; only an import or an inline item would
    # make the scene depend on the built C++ plugin at startup.
    assert re.search(r"^\s*import\s+SailworksRouting", qml, re.MULTILINE) is None
    assert "GpuLineItem {" not in qml
    assert "encGpuLineLayer" not in qml
    assert 'objectName: "encSceneLayer"' in qml


def test_flag_off_is_a_no_op(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    encmesh.set_gpu_lines_available(False)
    item = FakeItem()

    drawn = _layer(item).update(SimpleNamespace(line_segments=_segments()), _camera())

    assert drawn is False
    assert item.writes == []
    assert item.isVisible() is False


def test_env_force_on_cannot_upload_without_a_native_item(monkeypatch):
    """``=1`` is legacy: it cannot make a layer that was never built draw."""
    monkeypatch.setenv(FLAG, "1")
    encmesh.set_gpu_lines_available(False)
    item = FakeItem()

    drawn = _layer(item).update(SimpleNamespace(line_segments=_segments()), _camera())

    assert encmesh.gpu_lines_enabled() is False
    assert drawn is False
    assert item.writes == []


def test_missing_native_item_keeps_the_cpu_path(monkeypatch):
    """Creation failure records no availability, so the CPU stroke stays."""
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.setattr(encgpu_lines, "qml_dir", lambda: None)
    encmesh.set_gpu_lines_available(False)

    layer = encgpu_lines.EncGpuLineLayer()
    item = layer.create(SimpleNamespace(engine=lambda: None), object())

    assert item is None
    assert layer.available is False
    assert encmesh.gpu_lines_available() is False
    assert encmesh.gpu_lines_enabled() is False
    # Even the historical force-on value cannot skip the CPU stroke now.
    monkeypatch.setenv(FLAG, "1")
    assert encmesh.gpu_lines_enabled() is False


def test_clear_always_cleans_a_built_item():
    item = FakeItem()
    item.setProperty("segmentData", b"stale")
    item.setVisible(True)

    assert _layer(item).clear() is True
    assert bytes(item.property("segmentData")) == b""
    assert item.isVisible() is False
    assert _layer().clear() is False


def test_flag_on_uploads_the_packed_payload(monkeypatch, gpu_lines_available):
    monkeypatch.setenv(FLAG, "1")
    item = FakeItem()
    segments = _segments()

    drawn = _layer(item).update(SimpleNamespace(line_segments=segments), _camera())

    assert drawn is True
    payload = bytes(item.property("segmentData"))
    assert payload == segments.to_bytes()
    assert payload == segments.packed().tobytes(order="C")
    # Twelve little-endian float32 fields per record, no header or padding.
    assert len(payload) == segments.segment_count * 12 * 4
    assert len(payload) == segments.segment_count * 48
    assert item.isVisible() is True


def test_camera_is_written_as_floats(gpu_lines_available):
    item = FakeItem()
    camera = _camera()

    assert _layer(item).set_camera(camera) is True

    assert item.property("centerLatitude") == 37.5
    assert item.property("centerLongitude") == -122.25
    assert item.property("zoom") == 9.75


def test_pan_compensation_mirrors_and_follows_map_content():
    item = FakeItem()
    map_content = FakeMapContent()
    map_content.move(120.0, -40.0)

    assert encgpu_lines.compensate_pan(item, map_content) is True
    assert (item.x, item.y) == (-120.0, 40.0)

    map_content.move(-15.5, 7.25)
    assert (item.x, item.y) == (15.5, -7.25)

    assert encgpu_lines.compensate_pan(None, map_content) is False
    assert encgpu_lines.compensate_pan(item, None) is False


def test_empty_payload_clears_a_presented_layer(monkeypatch, gpu_lines_available):
    monkeypatch.setenv(FLAG, "1")
    item = FakeItem()
    layer = _layer(item)
    assert layer.update(SimpleNamespace(line_segments=_segments()), _camera())

    drawn = layer.update(
        SimpleNamespace(line_segments=encmesh.EMPTY_LINE_SEGMENTS),
        _camera(),
    )

    assert drawn is False
    assert bytes(item.property("segmentData")) == b""
    assert item.isVisible() is False


def test_missing_module_directory_is_a_no_op(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setattr(encgpu_lines, "qml_dir", lambda: None)
    container = FakeContainer()

    assert encgpu_lines.EncGpuLineLayer().create(object(), container) is None
    assert encgpu_lines.EncGpuLineLayer().create(object(), None) is None


@pytest.mark.parametrize("value", sorted(encmesh.GPU_LINES_OFF_VALUES))
def test_flag_off_never_looks_for_the_module(monkeypatch, value):
    """An explicit opt-out returns before the QML module is probed at all."""
    monkeypatch.setenv(FLAG, value)

    def fail():
        raise AssertionError("the module must not be probed with the flag off")

    monkeypatch.setattr(encgpu_lines, "qml_dir", fail)
    assert encgpu_lines.requested() is False
    assert encgpu_lines.forced_off() is True
    assert encgpu_lines.EncGpuLineLayer().create(object(), FakeContainer()) is None


def test_unset_flag_still_probes_for_the_module(monkeypatch, tmp_path):
    """The default process attempts creation when the module is on disk."""
    monkeypatch.delenv(FLAG, raising=False)
    assert encgpu_lines.requested() is True
    assert encgpu_lines.forced_off() is False

    monkeypatch.setattr(encgpu_lines, "qml_dir", lambda: tmp_path)
    assert encgpu_lines.EncGpuLineLayer().create(object(), FakeContainer()) is None


def test_host_without_an_engine_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setattr(encgpu_lines, "qml_dir", lambda: tmp_path)

    # No ``engine()`` on the host: creation cannot even be attempted.
    assert encgpu_lines.EncGpuLineLayer().create(object(), FakeContainer()) is None


def test_qml_dir_requires_the_module_qmldir(monkeypatch, tmp_path):
    monkeypatch.setenv("SAILWORKS_ROUTING_QML_PATH", str(tmp_path))
    assert encgpu_lines.qml_dir() is None

    (tmp_path / "SailworksRouting").mkdir()
    assert encgpu_lines.qml_dir() is None

    (tmp_path / "SailworksRouting" / "qmldir").write_text("module SailworksRouting\n")
    assert encgpu_lines.qml_dir() == tmp_path
