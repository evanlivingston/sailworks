"""The wave fill's visibility contract, against the real scene QML.

The wave layer was reviewed and merged without a test that loads QML, so a
wave fill that stayed ``visible: false`` -- and therefore never compiled --
could not be caught.  These tests load the scene the viewport actually uses
and assert the conditions the shader needs, on real QML objects.

The wind fill's equivalent mechanism is asserted here too: the scene binds the
front slot to a scene property (``root.fillEnabled`` for wind,
``root.waveFillEnabled`` for waves), and the viewport writes that property.
"""

import os
import time
from pathlib import Path
from types import MethodType

import pytest
from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickView
from PySide6.QtTest import QTest

from sailworks.qt.gribviewport import _viewport_widget_class

SHADERS = Path(__file__).resolve().parents[1] / "sailworks/qt/shaders"
SCENES = ("ViewportChartScene.qml", "WeatherScene.qml")

SHADER_COMPILED = 0
SHADER_UNCOMPILED = 1
SHADER_ERROR = 2


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    yield QGuiApplication.instance() or QGuiApplication([])


class _WaveSlots:
    """The viewport's own wave-slot methods, without the rest of the viewport.

    Only the two helpers under test are bound; they read ``_weather`` and the
    two slots, which is their whole contract.
    """

    def __init__(self, root, front, back, opacity=0.75):
        self._weather = root
        self._wave_fill = front
        self._wave_fill_back = back
        self._wave_opacity = opacity
        self._wave_visible = True
        self._disposed = False


def _slots(root, front, back):
    widget = _viewport_widget_class()
    harness = _WaveSlots(root, front, back)
    harness._show_wave_fill = MethodType(widget._show_wave_fill, harness)
    harness._show_wave_stage = MethodType(widget._show_wave_stage, harness)
    return harness


def _load(app, name):
    view = QQuickView()
    view.setSource(QUrl.fromLocalFile(str(SHADERS / name)))
    assert not view.errors(), f"{name}: {view.errors()}"
    root = view.rootObject()
    assert root is not None, name
    root.setProperty("width", 96)
    root.setProperty("height", 96)
    view.resize(96, 96)
    view.show()
    QTest.qWait(20)
    front = next(iter(root.findChildren(QObject, "waveFill")), None)
    back = next(iter(root.findChildren(QObject, "waveFillBack")), None)
    assert front is not None, f"{name} has no waveFill item"
    assert back is not None, f"{name} has no waveFillBack item"
    return view, root, front, back


@pytest.fixture(params=SCENES)
def loaded(request, app):
    view, root, front, back = _load(app, request.param)
    try:
        yield view, root, front, back
    finally:
        view.close()
        view.releaseResources()
        app.processEvents()


def test_scene_owns_one_wave_fill_pair(loaded):
    """No duplicate slot: a duplicate is how the hidden band got picked."""
    _view, root, _front, _back = loaded
    assert len(root.findChildren(QObject, "waveFill")) == 1
    assert len(root.findChildren(QObject, "waveFillBack")) == 1


def test_wave_front_follows_the_scene_property(loaded, app):
    """The wind mechanism: write the scene property, the binding follows."""
    _view, root, front, back = loaded
    harness = _slots(root, front, back)

    harness._show_wave_fill(True)
    app.processEvents()
    assert root.property("waveFillEnabled") is True
    assert front.property("visible") is True
    assert front.isVisible() is True

    front.setOpacity(0.75)
    assert front.opacity() == pytest.approx(0.75)

    harness._show_wave_fill(False)
    app.processEvents()
    assert root.property("waveFillEnabled") is False
    assert front.property("visible") is False
    assert front.isVisible() is False


def test_wave_front_has_no_hidden_ancestor(loaded, app):
    """A hidden container is what made ``setVisible(True)`` a no-op.

    ``isVisible()`` is the effective visibility: an item whose own flag is
    true is still not rendered when a parent is hidden, and an unrendered
    ShaderEffect never compiles.
    """
    _view, root, front, back = loaded
    harness = _slots(root, front, back)
    harness._show_wave_fill(True)
    app.processEvents()

    node = front.parentItem()
    while node is not None:
        assert node.isVisible(), (
            "waveFill sits inside hidden container "
            f"{node.objectName() or type(node).__name__!r}"
        )
        node = node.parentItem()


def test_wave_front_sits_in_the_wind_fill_band(loaded):
    """The wave fill shares the wind fill's always-drawn band."""
    _view, root, front, _back = loaded
    band = front.parentItem()
    wind = next(
        item for item in root.findChildren(QObject, "weatherFill") if item is not front
    )
    assert band is wind.parentItem()


def test_wave_stage_is_rendered(app):
    """The staging slot must be effectively visible, not merely opacity 0.01.

    Its shader compiles only while it is rendered, and ``prepared`` waits for
    that compile.  Without a render backend the compile cannot be observed, so
    the test states the skip instead of asserting nothing.
    """
    view, root, front, back = _load(app, "ViewportChartScene.qml")
    try:
        harness = _slots(root, front, back)
        back.setOpacity(0.01)
        harness._show_wave_stage(back)
        app.processEvents()

        assert back.property("visible") is True
        assert back.isVisible() is True, (
            "the staging slot is not effectively visible, so its shader can "
            "never compile and every frame times out"
        )

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            status = back.property("shaderStatus")
            if status in (SHADER_COMPILED, SHADER_ERROR):
                break
            QTest.qWait(20)
        status = back.property("shaderStatus")
        if status == SHADER_ERROR:
            pytest.fail(f"wave stage shader failed: {back.property('shaderError')!r}")
        if status != SHADER_COMPILED:
            pytest.skip(
                "no render backend compiled the shader (offscreen software); "
                "the stage slot is visible, which is the part this test can "
                "prove without a GPU"
            )
        if not back.property("imagesReady"):
            pytest.skip("shader compiled; no image sources are staged here")
        assert back.property("prepared"), (
            "prepared is false although the shader compiled and images are "
            f"ready (shaderStatus={status!r}, "
            f"shaderFailed={back.property('shaderFailed')!r})"
        )
    finally:
        view.close()
        view.releaseResources()
        app.processEvents()


class _StandInScene:
    """The scene-root property surface ``_show_wave_fill`` writes."""

    def __init__(self):
        self.waveFillEnabled = False

    def property(self, name):
        return getattr(self, name, None)

    def setProperty(self, name, value):
        setattr(self, name, value)

    def isVisible(self):
        return True

    def setVisible(self, visible):
        self.visible = visible


class _StandInSlot:
    """A ShaderEffect-shaped slot that compiles only while rendered.

    Mirrors the QML contract the real item obeys: ``isVisible`` is effective
    visibility, and ``shaderStatus`` stays Uncompiled until the slot renders.
    """

    def __init__(self, parent):
        self._parent = parent
        self._visible = False

    def setVisible(self, visible):
        self._visible = bool(visible)

    @property
    def shaderStatus(self):
        """Compiled only while rendered, as a ShaderEffect is."""
        if self._visible and self._parent.isVisible():
            return SHADER_COMPILED
        return SHADER_UNCOMPILED

    def property(self, name):
        return getattr(self, name, None)

    def parentItem(self):
        return self._parent

    def isVisible(self):
        return self._visible and self._parent.isVisible()


def test_stage_unhides_a_hidden_container(app):
    """The state machine without a GPU: hidden container, no compile, timeout."""
    scene = _StandInScene()
    band = _StandInSlot(scene)
    band.setVisible(False)
    slot = _StandInSlot(band)
    assert band.isVisible() is False

    harness = _WaveSlots(scene, None, slot)
    harness._show_wave_stage = MethodType(
        _viewport_widget_class()._show_wave_stage, harness
    )

    slot.setVisible(True)
    assert slot.isVisible() is False, "a hidden container must block the slot"
    assert slot.shaderStatus == SHADER_UNCOMPILED, "an unrendered slot cannot compile"

    slot.setVisible(False)
    harness._show_wave_stage(slot)
    assert band.isVisible() is True, "the hidden container was not unhidden"
    assert slot.isVisible() is True
    assert slot.shaderStatus == SHADER_COMPILED
