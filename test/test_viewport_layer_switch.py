"""One active background; ENC is an independent overlay over it.

The scene's layer switcher and the chart layer's identity must agree: a
background switch never leaves two backgrounds on (or none), the ENC cells
are an overlay whose retained composition is withdrawn when the setting
changes, and only a ready-to-ready swap fades.
"""

import os
import time
import unittest
from importlib.util import find_spec
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import patch

from sailworks.qt.gribviewport import (
    CHART_SWAP_FADE_MS,
    chart_swap_fade_ms,
    exclusive_background,
)
from sailworks.qt import gribviewport as viewport_module
from sailworks.qt.gribviewportchart import ViewportChartLayer

ROOT = Path(__file__).resolve().parents[1]
SCENE_QML = ROOT / "sailworks/qt/shaders/ViewportChartScene.qml"
CHART_LAYER_QML = ROOT / "sailworks/qt/shaders/ChartLayer.qml"
VIEWPORT = ROOT / "sailworks/qt/gribviewport.py"

if find_spec("PySide6") is not None:
    from PySide6.QtCore import QObject, QUrl
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtQuick import QQuickImageProvider, QQuickView
    from PySide6.QtTest import QTest


def _application():
    """One Qt application for the real-QML tests in this module."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout=5.0):
    """Pump the Qt event loop until a scene reaches the expected state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(10)
    return bool(predicate())


def viewport_block(start_marker, end_marker):
    """The source of one viewport method, from its def to the next def."""
    source = VIEWPORT.read_text()
    start = source.index(start_marker)
    return source[start : source.index(end_marker, start)]


class TestExclusiveBackground(unittest.TestCase):
    def test_showing_a_background_hides_the_other(self):
        self.assertEqual(
            exclusive_background("satellite", True, False, True), (True, False)
        )
        self.assertEqual(
            exclusive_background("basemap", True, True, False), (False, True)
        )

    def test_hiding_a_background_leaves_the_other_on(self):
        self.assertEqual(
            exclusive_background("satellite", False, True, True), (False, True)
        )
        self.assertEqual(
            exclusive_background("basemap", False, True, False), (True, False)
        )
        self.assertEqual(
            exclusive_background("basemap", False, False, True), (True, False)
        )

    def test_every_switch_leaves_exactly_one_background_on(self):
        states = [(False, False), (True, False), (False, True), (True, True)]
        for layer in ("satellite", "basemap"):
            for visible in (True, False):
                for satellite, basemap in states:
                    result = exclusive_background(layer, visible, satellite, basemap)
                    self.assertTrue(any(result), (layer, visible, satellite, basemap))
                    self.assertFalse(all(result), (layer, visible, satellite, basemap))

    def test_unknown_layer_changes_nothing(self):
        self.assertEqual(exclusive_background("enc", True, False, True), (False, True))


class TestSceneSwitcher(unittest.TestCase):
    def setUp(self):
        self.scene = SCENE_QML.read_text()

    def test_the_backgrounds_are_a_radio_group_in_the_scene(self):
        self.assertIn('property bool background: modelData.key !== "enc"', self.scene)
        # A background click always asks for the clicked background, never
        # toggles it off; exactly one background stays selected.
        self.assertIn("background ? true : !parent.active", self.scene)

    def test_enc_remains_an_independent_toggle(self):
        # The delegate builds its identity from the layer key, so the scene
        # names the ENC toggle without hardcoding a literal objectName.
        self.assertIn('objectName: "layerToggle-" + modelData.key', self.scene)
        self.assertIn(": root.encVisible", self.scene)

    def test_the_enc_overlay_is_above_the_background(self):
        satellite = self.scene.index('objectName: "satelliteLayer"')
        chart = self.scene.index('objectName: "chartLayer"')
        self.assertLess(satellite, chart)


class TestChartLayerIdentity(unittest.TestCase):
    def setUp(self):
        self.layer = ViewportChartLayer((), ROOT / "sailworks" / "data")

    def tearDown(self):
        self.layer.close()

    def test_enc_is_off_by_default(self):
        self.assertFalse(self.layer._enc_enabled)
        self.assertEqual(self.layer.enc_state, "disabled")

    def test_enabling_enc_withdraws_the_previous_composition(self):
        self.layer._published = (1, object(), {"preview": False})
        self.assertTrue(self.layer.set_enc_enabled(True))
        self.assertTrue(self.layer._enc_enabled)
        self.assertEqual(self.layer.enc_state, "loading")
        self.assertIsNone(self.layer._published)
        # The worker clears its retained pixels before composing again.
        self.assertTrue(self.layer._reset_sources)

    def test_disabling_enc_withdraws_the_previous_composition(self):
        self.layer.set_enc_enabled(True)
        self.layer._published = (2, object(), {"preview": False})
        self.assertTrue(self.layer.set_enc_enabled(False))
        self.assertFalse(self.layer._enc_enabled)
        self.assertEqual(self.layer.enc_state, "disabled")
        self.assertIsNone(self.layer._published)

    def test_setting_the_same_identity_is_a_no_op(self):
        self.assertFalse(self.layer.set_enc_enabled(False))
        self.layer.set_enc_enabled(True)
        self.assertFalse(self.layer.set_enc_enabled(True))

    def test_the_enc_composition_is_a_transparent_overlay(self):
        # With the cells in, no world coastline or opaque sea is painted into
        # the same surface: the background layer shows through the gaps.
        self.layer.set_enc_enabled(True)
        self.assertEqual(self.layer._base_sources(), ())
        self.layer.set_enc_enabled(False)
        self.assertNotEqual(self.layer._base_sources(), ())

    def test_an_imagery_background_composes_transparent(self):
        # The chart layer now sits above the satellite layer: an opaque base
        # chart would hide imagery, so imagery selects an empty base
        # composition and the raster background stays visible.
        self.assertTrue(self.layer.set_satellite_background(True))
        self.assertEqual(self.layer._base_sources(), ())
        self.assertTrue(self.layer._reset_sources)
        self.assertTrue(self.layer.set_satellite_background(False))
        self.assertNotEqual(self.layer._base_sources(), ())

    def test_switching_the_background_withdraws_the_composition(self):
        self.layer._published = (1, object(), {"preview": False})
        self.assertTrue(self.layer.set_satellite_background(True))
        self.assertIsNone(self.layer._published)
        # The same identity is a no-op and never discards a current frame.
        self.assertFalse(self.layer.set_satellite_background(True))


class TestViewportBackgroundSafety(unittest.TestCase):
    def test_a_satellite_request_that_cannot_start_falls_back(self):
        block = viewport_block(
            "def set_satellite_visible", "def set_drawer_tabs_visible"
        )
        self.assertIn("if not self._start_satellite():", block)
        self.assertIn("self._show_basemap()", block)
        self.assertIn("return False", block)

    def test_an_unavailable_background_is_not_left_empty_at_startup(self):
        source = VIEWPORT.read_text()
        start = source.index("if self._satellite_visible:")
        end = source.index(
            "world_factor = max(1, round(self.devicePixelRatio()))", start
        )
        block = source[start:end]
        self.assertIn("if not self._start_satellite():", block)
        self.assertIn("self._show_basemap()", block)

    def test_hiding_enc_recomposes_instead_of_blanking_the_layer(self):
        block = viewport_block("def _set_enc_visible", "def _layer_toggled")
        # Both identities need a composition, including removing the cells.
        self.assertIn("self._restart_chart_settle()", block)
        # The chart layer stays visible: the overlay is transparent outside
        # the cells, so the background shows through.
        self.assertNotIn('self._chart.setProperty("visible"', block)

    def test_the_chart_model_is_retained_and_the_toggle_reaches_it(self):
        source = VIEWPORT.read_text()
        self.assertIn("self._chart_model = ViewportChartLayer(", source)
        block = viewport_block("def _set_enc_visible", "def _layer_toggled")
        self.assertIn("model.set_enc_enabled(visible)", block)
        self.assertIn("self._clear_chart()", block)

    def test_a_background_switch_recomposes_for_the_new_identity(self):
        block = viewport_block("def set_layer_visible", "def _set_enc_visible")
        self.assertIn("self._sync_satellite_background()", block)
        sync = viewport_block("def _sync_satellite_background", "def _show_basemap")
        self.assertIn("setter(self._satellite_visible)", sync)
        self.assertIn("self._restart_chart_settle()", sync)
        self.assertIn("self._chart_force_replace = True", sync)

    def test_the_basemap_fallback_restores_the_composition_identity(self):
        block = viewport_block("def _show_basemap", "def set_satellite_visible")
        self.assertIn("self._sync_satellite_background()", block)

    def test_the_scene_owns_the_world_layer_visibility(self):
        # No Python write may replace the binding: the imperative write was
        # what left the world up over imagery or blanked the base while it
        # loaded.
        self.assertNotIn("_base_layer_item", VIEWPORT.read_text())
        scene = SCENE_QML.read_text()
        self.assertIn("visible: root.basemapVisible || root.satelliteVisible", scene)
        self.assertIn(
            "readonly property bool satellitePresented: root.satelliteVisible",
            scene,
        )
        self.assertIn("opacity: root.satellitePresented ? 0.9 : 0", scene)

    def test_a_retained_raster_is_withdrawn_before_imagery_is_shown(self):
        withdraw = viewport_block(
            "def _withdraw_satellite_frame", "def set_satellite_visible"
        )
        self.assertIn(
            'self._satellite.setProperty(f"slot{slot}Source", QUrl(""))',
            withdraw,
        )
        shown = viewport_block(
            "def set_satellite_visible", "def set_drawer_tabs_visible"
        )
        # The withdrawn raster is retired before the layer may be presented,
        # and presentation waits for the sharp composition plus a raster
        # frame instead of flipping on the request.
        self.assertLess(
            shown.index("self._withdraw_satellite_frame()"),
            shown.index("self._sync_satellite_background()"),
        )
        self.assertIn('self._present_background("satellite")', shown)
        self.assertNotIn('self._weather.setProperty("satelliteVisible", True)', shown)

    def test_a_layer_toggle_arms_the_fade_of_the_composition_it_requests(self):
        sync = viewport_block("def _sync_satellite_background", "def _show_basemap")
        self.assertIn("self._chart_swap_fade_armed = True", sync)
        self.assertLess(
            sync.index("self._chart_swap_fade_armed = True"),
            sync.index("self._restart_chart_settle()"),
        )
        enc = viewport_block("def _set_enc_visible", "def _layer_toggled")
        self.assertIn("self._chart_swap_fade_armed = True", enc)
        self.assertLess(
            enc.index("self._chart_swap_fade_armed = True"),
            enc.index("self._restart_chart_settle()"),
        )
        # The old composition is withdrawn before the fade is armed, so no
        # stale cells are dissolved into the new state.
        self.assertLess(
            enc.index("self._clear_chart()"),
            enc.index("self._chart_swap_fade_armed = True"),
        )

    def test_a_withdrawn_composition_does_not_leave_a_fade_armed(self):
        block = viewport_block("def _reject_chart_pending", "def _clear_chart")
        self.assertIn('self._chart.setProperty("fadeMs", 0)', block)
        clear = viewport_block("def _clear_chart", "def _chart_slot_result")
        self.assertIn('self._chart.setProperty("fadeMs", 0)', clear)


class _BackgroundItem:
    """Records the presentation properties the viewport writes."""

    def __init__(self):
        self.properties = {}

    def setProperty(self, name, value):  # noqa: N802
        self.properties[name] = value


class _BackgroundChartModel:
    """The chart model's identity, with the request a switch would make."""

    def __init__(self, current):
        self.satellite = current
        self.identities = []
        self.requests = []

    def set_satellite_background(self, enabled):
        self.identities.append(enabled)
        if enabled == self.satellite:
            return False
        self.satellite = enabled
        return True

    def request(self, camera, replace=False, preview=False):
        self.requests.append((camera, replace, preview))
        return 99

    def busy(self):
        return False


def _background_host(camera, *, satellite_visible, identity, chart_visible=True):
    viewport_class = _viewport_class()
    host = SimpleNamespace(
        _camera=camera,
        _satellite_visible=satellite_visible,
        _layer_basemap_visible=not satellite_visible,
        _chart_model=_BackgroundChartModel(current=bool(identity)),
        _chart_timer=object(),
        _chart_visible_generation=7 if chart_visible else None,
        _chart_background_identity=identity,
        _chart_force_replace=False,
        _chart_swap_fade_armed=False,
        _background_pending=None,
        _background_pending_camera=None,
        # No raster is ready yet, so the vector base is what the scene shows.
        _background_presented="basemap",
        _satellite_visible_generation=None,
        _weather=_BackgroundItem(),
        _weather_ready_at=0.0,
        _restart_calls=0,
        cleared=False,
    )
    for name in (
        "_sync_satellite_background",
        "_present_background",
        "_settle_background_presentation",
        "_cancel_background_switch",
        "_request_interactive_chart",
    ):
        setattr(host, name, MethodType(getattr(viewport_class, name), host))
    host._restart_chart_settle = lambda: setattr(
        host, "_restart_calls", host._restart_calls + 1
    )
    host._clear_chart = lambda: setattr(host, "cleared", True)
    return host


class TestReadyToReadyBackgroundSwitch(unittest.TestCase):
    """A background switch waits for content, never for the click."""

    def test_a_same_camera_switch_retains_the_sharp_frame(self):
        camera = _camera()
        # The opaque basemap chart is current; the user selects imagery.
        host = _background_host(
            camera, satellite_visible=True, identity=False, chart_visible=True
        )
        self.assertTrue(host._sync_satellite_background())
        # The sharp frame is neither withdrawn nor blanked: it stays current
        # until the transparent composition is ready.
        self.assertFalse(host.cleared)
        self.assertEqual(host._chart_visible_generation, 7)
        self.assertEqual(host._background_pending, "satellite")
        self.assertIs(host._background_pending_camera, camera)
        # The scene has not been told to swap yet.
        self.assertNotIn("satelliteVisible", host._weather.properties)

    def test_the_switch_requests_a_full_resolution_composition(self):
        host = _background_host(
            _camera(), satellite_visible=True, identity=False, chart_visible=True
        )
        host._sync_satellite_background()
        # A forced replacement composes at native scale, not as a preview.
        self.assertTrue(host._chart_force_replace)
        self.assertEqual(host._restart_calls, 1)
        host._request_interactive_chart()
        # The half-resolution interactive preview cannot fire while the
        # switch waits for its sharp composition.
        self.assertEqual(host._chart_model.requests, [])

    def test_imagery_waits_for_the_composition_and_the_raster(self):
        host = _background_host(
            _camera(), satellite_visible=True, identity=False, chart_visible=True
        )
        host._sync_satellite_background()
        # Still the old opaque composition.
        self.assertFalse(host._settle_background_presentation())
        self.assertEqual(host._background_presented, "basemap")
        # The transparent composition is current, but no raster is ready: the
        # vector base would be presented as if it were imagery.
        host._chart_background_identity = True
        self.assertFalse(host._settle_background_presentation())
        self.assertNotIn("satelliteVisible", host._weather.properties)
        host._satellite_visible_generation = 5
        self.assertTrue(host._settle_background_presentation())
        self.assertIsNone(host._background_pending)
        self.assertEqual(host._background_presented, "satellite")
        self.assertTrue(host._weather.properties["satelliteVisible"])
        self.assertFalse(host._weather.properties["basemapVisible"])

    def test_the_basemap_presents_on_the_opaque_composition(self):
        host = _background_host(
            _camera(), satellite_visible=False, identity=True, chart_visible=True
        )
        self.assertTrue(host._sync_satellite_background())
        self.assertEqual(host._background_pending, "basemap")
        # The transparent imagery composition is still what is on screen.
        self.assertFalse(host._settle_background_presentation())
        host._chart_background_identity = False
        self.assertTrue(host._settle_background_presentation())
        self.assertFalse(host._weather.properties["satelliteVisible"])
        self.assertTrue(host._weather.properties["basemapVisible"])

    def test_a_camera_change_withdraws_the_retained_switch(self):
        host = _background_host(
            _camera(), satellite_visible=True, identity=False, chart_visible=True
        )
        host._sync_satellite_background()
        host._camera = _camera(center_lon=12.0)
        # The retained frame belonged to the old camera: it is not held.  The
        # desired target is still imagery, but no raster is ready, so the
        # vector base stands in rather than an empty satellite band.
        self.assertFalse(host._settle_background_presentation())
        self.assertIsNone(host._background_pending)
        self.assertEqual(host._background_presented, "basemap")
        self.assertFalse(host._weather.properties["satelliteVisible"])
        self.assertTrue(host._weather.properties["basemapVisible"])
        # The desired target is not forgotten: a raster makes it presentable.
        host._chart_background_identity = True
        host._satellite_visible_generation = 4
        self.assertTrue(host._settle_background_presentation())
        self.assertEqual(host._background_presented, "satellite")
        self.assertTrue(host._weather.properties["satelliteVisible"])

    @unittest.skipIf(find_spec("PySide6") is None, "PySide6 is not installed")
    def test_a_superseded_publication_keeps_the_pending_switch(self):
        camera = _camera()
        rejected = []

        class _Manager:
            def reject_pending(self, role, token):
                rejected.append((role, token))

        class _Provider:
            def retain(self, generation=None):
                pass

        host = SimpleNamespace(
            _chart_pending=(3, {}, 1, "token"),
            _publication_manager=_Manager(),
            _chart=_BackgroundItem(),
            _chart_provider=_Provider(),
            _chart_visible_generation=7,
            _satellite_visible=True,
            _satellite_visible_generation=None,
            _background_pending="satellite",
            _background_pending_camera=camera,
            _background_presented="basemap",
            _chart_swap_fade_armed=False,
            _weather=_BackgroundItem(),
        )
        viewport_class = _viewport_class()
        for name in (
            "_reject_chart_pending",
            "_cancel_background_switch",
            "_present_background",
        ):
            setattr(host, name, MethodType(getattr(viewport_class, name), host))
        # A newer composition supersedes the pending one: it may be the frame
        # the switch is waiting for, so the switch must not be withdrawn.
        host._reject_chart_pending(preserve_background_switch=True)
        self.assertEqual(host._background_pending, "satellite")
        self.assertIs(host._background_pending_camera, camera)
        self.assertNotIn("satelliteVisible", host._weather.properties)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0][1], "token")
        # An actual failure has no replacement coming: withdraw the switch and
        # present the explicit fallback.
        host._chart_pending = (4, {}, 0, "next")
        host._reject_chart_pending()
        self.assertIsNone(host._background_pending)
        self.assertFalse(host._weather.properties["satelliteVisible"])

    def test_the_publication_path_preserves_the_switch_on_supersede(self):
        publish = viewport_block("def _publish_chart", "def _reject_chart_pending")
        self.assertIn("preserve_background_switch=True", publish)

    def test_only_a_retained_frame_defers_the_switch(self):
        # Nothing sharp to retain: the target is presented at once, with one
        # fade armed for its first ready frame.
        host = _background_host(
            _camera(), satellite_visible=True, identity=None, chart_visible=False
        )
        self.assertTrue(host._sync_satellite_background())
        self.assertIsNone(host._background_pending)
        self.assertTrue(host._chart_swap_fade_armed)
        self.assertTrue(host._weather.properties["satelliteVisible"])
        self.assertEqual(host._restart_calls, 1)

    def test_a_withdrawn_composition_settles_nothing(self):
        clear = viewport_block("def _clear_chart", "def _chart_slot_result")
        self.assertIn("self._chart_background_identity = None", clear)
        self.assertIn("self._cancel_background_switch()", clear)
        reject = viewport_block("def _reject_chart_pending", "def _clear_chart")
        self.assertIn("self._cancel_background_switch()", reject)
        camera = viewport_block("def set_camera", "def _retire_barb_anchor_frame")
        self.assertIn("self._cancel_background_switch()", camera)

    def test_the_fade_is_the_scene_duration_and_waits_for_the_raster(self):
        scene = SCENE_QML.read_text()
        self.assertIn("property int backgroundFadeMs: 500", scene)
        self.assertIn(
            "readonly property bool satellitePresented: root.satelliteVisible",
            scene,
        )
        self.assertIn("&& satelliteLayer.ready", scene)


class TestChartSwapFade(unittest.TestCase):
    def test_a_ready_swap_always_fades(self):
        self.assertEqual(CHART_SWAP_FADE_MS, 500)
        self.assertEqual(chart_swap_fade_ms(True, False), CHART_SWAP_FADE_MS)

    def test_a_layer_toggle_fades_the_composition_it_requested(self):
        # The toggle withdrew the previous composition, so this is not a
        # ready-to-ready swap, but the replacement must not cut in.
        self.assertEqual(chart_swap_fade_ms(False, True), CHART_SWAP_FADE_MS)

    def test_an_unrequested_composition_appears_without_a_fade(self):
        self.assertEqual(chart_swap_fade_ms(False, False), 0)


class TestReadyTransitionFade(unittest.TestCase):
    def test_the_scene_fades_only_on_a_viewport_set_duration(self):
        qml = CHART_LAYER_QML.read_text()
        self.assertIn("property int fadeMs: 0", qml)
        self.assertEqual(qml.count("duration: root.fadeMs"), 2)
        # The outgoing slot stays visible until its fade completes, so the
        # background never flashes between the two ready images.
        self.assertIn("visible: root.frontSlot === 0 || opacity > 0", qml)
        self.assertIn("visible: root.frontSlot === 1 || opacity > 0", qml)
        # The duration is one swap's, not a sticky default.
        self.assertIn("root.fadeMs = 0", qml)
        self.assertIn("fadeReset.restart()", qml)
        # Clearing an outgoing source does not delete the frame mid-fade, but
        # clearing the front slot still withdraws it immediately.
        self.assertIn("root.frontSlot !== 0", qml)
        self.assertIn("root.frontSlot !== 1", qml)

    def test_the_viewport_fades_the_composition_of_a_layer_toggle(self):
        block = viewport_block(
            'read_property = getattr(self._chart, "property", None)',
            "self._chart_pending = None",
        )
        self.assertIn(
            "fade_ms = chart_swap_fade_ms(previously_ready, fade_armed)",
            block,
        )
        # The arm is consumed by the composition that adopts it.
        self.assertIn("self._chart_swap_fade_armed = False", block)

    def test_the_duration_is_set_before_the_slot_is_promoted(self):
        block = viewport_block(
            'read_property = getattr(self._chart, "property", None)',
            "self._chart_front_slot = slot",
        )
        self.assertLess(
            block.index('self._chart.setProperty("fadeMs", fade_ms)'),
            block.index("self._place_chart()"),
        )


def _frame_provider():
    """A tile for a layer that has to load before it can be presented."""

    class _FrameProvider(QQuickImageProvider):
        def __init__(self):
            super().__init__(QQuickImageProvider.Image)

        def requestImage(self, identifier, size, requestedSize):
            # PySide6 expects the QImage itself; a (image, size) tuple is the
            # C++/PyQt contract and is rejected with a QML provider warning.
            image = QImage(8, 8, QImage.Format_ARGB32)
            image.fill(QColor("#3b7d4f"))
            return image

    return _FrameProvider()


class _SceneQml:
    """The real scene with both backgrounds as live QML items."""

    def __init__(self):
        self.view = QQuickView()
        self.view.engine().addImageProvider("testframe", _frame_provider())
        self.view.setSource(QUrl.fromLocalFile(str(SCENE_QML)))
        if self.view.errors():
            raise AssertionError(str(self.view.errors()))
        self.root = self.view.rootObject()
        if self.root is None:
            raise AssertionError("the scene did not load")
        self.world = self.root.findChild(QObject, "worldLayer")
        self.satellite = self.root.findChild(QObject, "satelliteLayer")
        if self.world is None or self.satellite is None:
            raise AssertionError("the scene has no background layers")
        self.results = []
        self.satellite.slotResult.connect(self._record)
        self.view.resize(64, 64)
        self.view.show()

    def _record(self, slot, token, success):
        self.results.append((slot, token, success))

    def show_frame(self, slot=0, name="frame-a"):
        self.satellite.setProperty(f"slot{slot}Token", name)
        self.satellite.setProperty(
            f"slot{slot}Source", QUrl(f"image://testframe/{name}")
        )

    def wait_frame(self, slot, name, timeout=5.0):
        return _wait_until(lambda: (slot, name, True) in self.results, timeout=timeout)

    def opacity(self, layer):
        return float(layer.property("opacity") or 0.0)

    def close(self):
        self.view.close()
        self.view.releaseResources()


@unittest.skipUnless(find_spec("PySide6"), "Requires PySide6")
class TestBackgroundTransition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _application()

    def test_the_base_stays_visible_while_imagery_loads(self):
        scene = _SceneQml()
        try:
            scene.root.setProperty("basemapVisible", True)
            scene.root.setProperty("satelliteVisible", False)
            self.assertTrue(scene.world.property("visible"))
            self.assertEqual(scene.opacity(scene.satellite), 0.0)

            # Picking imagery starts a worker; no frame is ready yet.
            scene.root.setProperty("basemapVisible", False)
            scene.root.setProperty("satelliteVisible", True)
            self.assertFalse(scene.satellite.property("ready"))
            self.assertEqual(scene.opacity(scene.satellite), 0.0)
            # The layer is on, and the vector base keeps the band filled.
            self.assertTrue(scene.satellite.property("visible"))
            self.assertTrue(scene.world.property("visible"))
        finally:
            scene.close()

    def test_a_ready_frame_fades_in_over_the_vector_base(self):
        scene = _SceneQml()
        try:
            scene.root.setProperty("basemapVisible", False)
            scene.root.setProperty("satelliteVisible", True)
            scene.show_frame()
            self.assertTrue(_wait_until(lambda: scene.satellite.property("ready")))
            # The ready frame starts from transparent rather than appearing
            # whole.
            self.assertLess(scene.opacity(scene.satellite), 0.9)
            self.assertTrue(_wait_until(lambda: scene.opacity(scene.satellite) > 0.05))
            middle = scene.opacity(scene.satellite)
            self.assertGreater(middle, 0.0)
            self.assertLess(middle, 0.9)
            self.assertTrue(
                _wait_until(lambda: abs(scene.opacity(scene.satellite) - 0.9) < 0.005)
            )
            self.assertAlmostEqual(scene.opacity(scene.satellite), 0.9, places=2)
            # The base remains beneath the raster: it is the coastline
            # underlay the satellite opacity shows through.
            self.assertTrue(scene.world.property("visible"))
        finally:
            scene.close()

    def test_leaving_imagery_fades_it_out_onto_the_basemap(self):
        scene = _SceneQml()
        try:
            scene.root.setProperty("basemapVisible", False)
            scene.root.setProperty("satelliteVisible", True)
            scene.show_frame()
            self.assertTrue(_wait_until(lambda: scene.opacity(scene.satellite) > 0.88))

            scene.root.setProperty("satelliteVisible", False)
            scene.root.setProperty("basemapVisible", True)
            # The raster is still on screen at the start of its fade: the
            # switch is not a cut.
            self.assertGreater(scene.opacity(scene.satellite), 0.5)
            self.assertTrue(_wait_until(lambda: scene.opacity(scene.satellite) < 0.05))
            self.assertTrue(
                _wait_until(lambda: not scene.satellite.property("visible"))
            )
            self.assertTrue(scene.world.property("visible"))
        finally:
            scene.close()

    def test_a_streamed_frame_replaces_the_presented_one_without_fading(self):
        scene = _SceneQml()
        try:
            scene.root.setProperty("basemapVisible", False)
            scene.root.setProperty("satelliteVisible", True)
            scene.show_frame(0)
            self.assertTrue(
                _wait_until(lambda: abs(scene.opacity(scene.satellite) - 0.9) < 0.005)
            )

            scene.show_frame(1, "frame-b")
            self.assertTrue(scene.wait_frame(1, "frame-b"))
            scene.satellite.setProperty("frontSlot", 1)
            # A streamed tile is a replacement, not a background switch: the
            # layer never dips.
            self.assertAlmostEqual(scene.opacity(scene.satellite), 0.9, places=2)
            QTest.qWait(150)
            self.assertAlmostEqual(scene.opacity(scene.satellite), 0.9, places=2)
            self.assertEqual(scene.satellite.property("fadeMs"), 0)
        finally:
            scene.close()


@unittest.skipUnless(find_spec("PySide6"), "Requires PySide6")
class TestCompositionFade(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _application()

    def _layer(self):
        view = QQuickView()
        view.engine().addImageProvider("testframe", _frame_provider())
        view.setSource(QUrl.fromLocalFile(str(CHART_LAYER_QML)))
        self.assertFalse(view.errors(), str(view.errors()))
        layer = view.rootObject()
        self.assertIsNotNone(layer)
        view.resize(64, 64)
        view.show()
        return view, layer

    def test_a_composition_with_a_duration_fades_in(self):
        view, layer = self._layer()
        try:
            results = []
            layer.slotResult.connect(
                lambda slot, token, success: results.append((slot, token, success))
            )
            layer.setProperty("imagePixelWidth", 8)
            layer.setProperty("imagePixelHeight", 8)
            layer.setProperty("fadeMs", 500)
            layer.setProperty("slot1Token", "chart-a")
            layer.setProperty("slot1Source", QUrl("image://testframe/chart-a"))
            self.assertTrue(_wait_until(lambda: (1, "chart-a", True) in results))
            slot = layer.findChild(QObject, "chartSlot1")
            self.assertIsNotNone(slot)
            self.assertEqual(float(slot.property("opacity")), 0.0)
            layer.setProperty("frontSlot", 1)
            # A real fade starts below the target: sample the first frame the
            # animation shows rather than reading the property in the same
            # call that started it.
            first = []
            deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline and not first:
                value = float(slot.property("opacity"))
                if value > 0.0:
                    first.append(value)
                QTest.qWait(5)
            self.assertTrue(first, "the promoted composition never started fading")
            self.assertLess(first[0], 0.9)
            self.assertTrue(_wait_until(lambda: float(slot.property("opacity")) > 0.95))
        finally:
            view.close()
            view.releaseResources()

    def test_a_new_token_for_the_same_source_reports_the_loaded_frame(self):
        view, layer = self._layer()
        try:
            results = []
            layer.slotResult.connect(
                lambda slot, token, success: results.append((slot, token, success))
            )
            layer.setProperty("slot1Token", "chart-c")
            layer.setProperty("slot1Source", QUrl("image://testframe/chart-c"))
            self.assertTrue(_wait_until(lambda: (1, "chart-c", True) in results))
            # A redelivery of the same generation reuses the source URL, so
            # the image is not reloaded: the new token must still be answered
            # from the frame already on the slot.
            layer.setProperty("slot1Token", "chart-c-2")
            self.assertTrue(_wait_until(lambda: (1, "chart-c-2", True) in results))
        finally:
            view.close()
            view.releaseResources()

    def test_without_a_duration_the_composition_cuts_in(self):
        view, layer = self._layer()
        try:
            results = []
            layer.slotResult.connect(
                lambda slot, token, success: results.append((slot, token, success))
            )
            layer.setProperty("slot1Token", "chart-b")
            layer.setProperty("slot1Source", QUrl("image://testframe/chart-b"))
            self.assertTrue(_wait_until(lambda: (1, "chart-b", True) in results))
            slot = layer.findChild(QObject, "chartSlot1")
            self.assertEqual(layer.property("fadeMs"), 0)
            layer.setProperty("frontSlot", 1)
            # Without a duration the promoted frame cuts in: it must settle on
            # full opacity within a few event-loop turns, not after a fade.
            self.assertTrue(
                _wait_until(
                    lambda: float(slot.property("opacity")) == 1.0, timeout=0.2
                ),
                "a composition without a duration did not cut in",
            )
        finally:
            view.close()
            view.releaseResources()


@unittest.skipUnless(find_spec("PySide6"), "Requires PySide6")
class TestChartLayersPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def panel(self, values=None, satellite_ready=True):
        from sailworks.qt.chartlayerspanel import ChartLayersPanel

        class Settings(dict):
            def __init__(self, initial):
                super().__init__(initial)
                self.callbacks = {}

            def get(self, key, default=None):
                return dict.get(self, key, default)

            def register_on_change(self, key, callback):
                self.callbacks.setdefault(key, []).append(callback)

            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                for callback in self.callbacks.get(key, ()):
                    callback(value)

        if values is None:
            values = {"chartWorldVisible": True, "chartSatelliteVisible": False}
        self.settings = Settings(dict(values))
        controller = SimpleNamespace(settings=self.settings)
        with patch(
            "sailworks.qt.chartlayerspanel.satellite_available",
            return_value=satellite_ready,
        ):
            return ChartLayersPanel(controller)

    def test_the_background_is_a_radio_group(self):
        panel = self.panel()
        self.assertEqual(
            set(panel.radios), {"chartWorldVisible", "chartSatelliteVisible"}
        )
        self.assertTrue(panel.radios["chartWorldVisible"].isChecked())

    def test_selecting_satellite_clears_the_basemap_setting(self):
        panel = self.panel(satellite_ready=True)
        satellite = panel.radios["chartSatelliteVisible"]
        self.assertTrue(satellite.isEnabled())
        satellite.setChecked(True)
        self.assertFalse(self.settings["chartWorldVisible"])
        self.assertTrue(self.settings["chartSatelliteVisible"])
        self.assertFalse(panel.radios["chartWorldVisible"].isChecked())

    def test_selecting_the_basemap_clears_the_satellite_setting(self):
        panel = self.panel(
            {"chartWorldVisible": False, "chartSatelliteVisible": True},
            satellite_ready=True,
        )
        panel.radios["chartWorldVisible"].setChecked(True)
        self.assertTrue(self.settings["chartWorldVisible"])
        self.assertFalse(self.settings["chartSatelliteVisible"])
        self.assertTrue(panel.radios["chartWorldVisible"].isChecked())

    def test_an_unavailable_satellite_is_disabled_and_normalised(self):
        panel = self.panel(
            {"chartWorldVisible": False, "chartSatelliteVisible": True},
            satellite_ready=False,
        )
        self.assertFalse(panel.radios["chartSatelliteVisible"].isEnabled())
        self.assertTrue(panel.radios["chartWorldVisible"].isChecked())
        self.assertFalse(panel.radios["chartSatelliteVisible"].isChecked())
        self.assertTrue(self.settings["chartWorldVisible"])
        self.assertFalse(self.settings["chartSatelliteVisible"])

    def test_a_stored_pair_with_neither_background_selects_the_basemap(self):
        panel = self.panel(
            {"chartWorldVisible": False, "chartSatelliteVisible": False},
            satellite_ready=True,
        )
        self.assertTrue(panel.radios["chartWorldVisible"].isChecked())
        self.assertTrue(self.settings["chartWorldVisible"])
        self.assertFalse(self.settings["chartSatelliteVisible"])

    def test_a_stored_pair_with_both_backgrounds_selects_one(self):
        panel = self.panel(
            {"chartWorldVisible": True, "chartSatelliteVisible": True},
            satellite_ready=True,
        )
        self.assertTrue(panel.radios["chartSatelliteVisible"].isChecked())
        self.assertFalse(panel.radios["chartWorldVisible"].isChecked())
        self.assertFalse(self.settings["chartWorldVisible"])
        self.assertTrue(self.settings["chartSatelliteVisible"])

    def test_enc_is_an_overlay_checkbox_not_a_background(self):
        panel = self.panel()
        self.assertIn("chartENCVisible", panel.checkboxes)
        self.assertNotIn("chartWorldVisible", panel.checkboxes)
        self.assertNotIn("chartENCVisible", panel.radios)


class _NotifyingSettings(dict):
    """The application settings' contract: every assignment notifies.

    ``Storage.__setitem__`` saves and calls ``notify_change`` for the key on
    every write, including a write that repeats the value already stored.  A
    layer switch therefore has to survive its own echo.
    """

    def __init__(self, values):
        super().__init__(values)
        self.handlers = {}
        self.writes = []

    def register_on_change(self, key, handler):
        self.handlers.setdefault(key, []).append(handler)
        handler(self[key])

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.writes.append((key, bool(value)))
        for handler in list(self.handlers.get(key, ())):
            handler(value)


def _presenter_host(settings):
    """The smallest canvas the presenter's settings callback reads."""

    return SimpleNamespace(
        canvas=SimpleNamespace(controller=SimpleNamespace(settings=settings))
    )


def _layer_changed(settings, layer, visible):
    """Run the real presenter callback against a stand-in canvas."""

    from sailworks.qt.weatherpresenter import WeatherPresenter

    return WeatherPresenter._layer_visibility_changed(
        _presenter_host(settings), layer, visible
    )


@unittest.skipUnless(find_spec("PySide6"), "Requires PySide6")
class TestLayerSettingsFeedback(unittest.TestCase):
    """A layer switch settles instead of echoing through the settings."""

    KEYS = {
        "satellite": "chartSatelliteVisible",
        "basemap": "chartWorldVisible",
        "enc": "chartENCVisible",
    }

    def _settings(self):
        return _NotifyingSettings(
            {
                "chartSatelliteVisible": False,
                "chartWorldVisible": True,
                "chartENCVisible": True,
            }
        )

    def _echo_to_presenter(self, settings):
        """The widget's own signal: a stored change is applied again."""

        for layer, key in self.KEYS.items():
            settings.register_on_change(
                key,
                lambda value, layer=layer: _layer_changed(settings, layer, bool(value)),
            )

    def test_a_switch_that_changes_nothing_writes_nothing(self):
        settings = self._settings()
        self._echo_to_presenter(settings)

        _layer_changed(settings, "satellite", False)
        _layer_changed(settings, "basemap", True)
        _layer_changed(settings, "enc", True)

        self.assertEqual(settings.writes, [])

    def test_a_switch_that_changes_the_setting_is_written_once(self):
        settings = self._settings()
        self._echo_to_presenter(settings)

        _layer_changed(settings, "enc", False)

        self.assertEqual(settings.writes, [("chartENCVisible", False)])
        self.assertIs(settings["chartENCVisible"], False)

    def test_a_background_click_settles_without_recursing(self):
        settings = self._settings()
        self._echo_to_presenter(settings)

        # The panel's radio writes the key; the widget applies it and reports
        # the state it reached, and the stored value comes back to it.
        settings["chartSatelliteVisible"] = True

        self.assertEqual(settings.writes, [("chartSatelliteVisible", True)])
        self.assertIs(settings["chartSatelliteVisible"], True)

    def test_the_backgrounds_stay_exclusive_through_the_feedback(self):
        settings = self._settings()
        self._echo_to_presenter(settings)

        # The scene turns imagery on and reports the vector base off.
        _layer_changed(settings, "satellite", True)
        _layer_changed(settings, "basemap", False)

        self.assertEqual(
            settings.writes,
            [("chartSatelliteVisible", True), ("chartWorldVisible", False)],
        )


class _SatelliteItem:
    """Records the properties the viewport writes on the scene item."""

    def __init__(self):
        self.properties = {}

    def setProperty(self, name, value):  # noqa: N802
        self.properties[name] = value


class _SatelliteTimer:
    def __init__(self):
        self.starts = 0
        self.intervals = []
        self.active = False

    def start(self, *_args):
        self.starts += 1
        self.active = True

    def stop(self):
        self.active = False

    def isActive(self):  # noqa: N802
        return self.active

    def setInterval(self, milliseconds):  # noqa: N802
        self.intervals.append(milliseconds)


class _SatelliteProvider:
    def __init__(self):
        self.retained = []

    def retain(self, generation=None):
        self.retained.append(generation)


def _viewport_class():
    """The viewport class that owns the satellite lifecycle."""
    return viewport_module._viewport_widget_class()


def _camera(center_lon=0.0, zoom=5.0, width=1000, height=800):
    return SimpleNamespace(
        center_lon=center_lon,
        zoom=zoom,
        width=width,
        height=height,
        view=lambda: SimpleNamespace(),
    )


def _satellite_host(camera, *, visible):
    """A viewport-shaped object whose satellite lifecycle methods are real."""
    viewport_class = _viewport_class()
    host = SimpleNamespace(
        _satellite_visible=visible,
        _camera=camera,
        _satellite_camera=None,
        _satellite_provider=_SatelliteProvider(),
        _satellite_timer=_SatelliteTimer(),
        _satellite_retry_timer=None,
        _satellite_retry_at=0.0,
        _satellite_source=None,
        _satellite_source_camera=None,
        _satellite_visible_generation=None,
        _satellite_generation=0,
        _satellite_superseded_generation=0,
        _satellite_job_cameras={},
        _satellite_pending_tokens={},
        _satellite_front_slot=0,
        _satellite_failures=0,
        _satellite_error="",
        _satellite_motion=False,
        _wheel_zoom_target=None,
        _native_gesture_active=False,
        _weather=None,
        _chart_model=None,
        _disposed=False,
        _background_pending=None,
        _background_pending_camera=None,
        _background_presented="basemap",
        _chart_background_identity=None,
        _chart_visible_generation=None,
        failures=[],
    )
    host._satellite = _SatelliteItem()
    for name in (
        "set_satellite_visible",
        "_withdraw_satellite_frame",
        "_satellite_camera_changed",
        "_satellite_covers",
        "_satellite_continuous_source",
        "_satellite_zooming",
        "_satellite_set_motion",
        "_satellite_slot_result",
        "_sync_satellite_background",
        "_present_background",
        "_settle_background_presentation",
        "_cancel_background_switch",
    ):
        setattr(host, name, MethodType(getattr(viewport_class, name), host))
    host._start_satellite = lambda: True
    host._satellite_sync_source = lambda: None
    host._set_satellite_status = lambda text: None
    host._satellite_failed = lambda generation, reason: host.failures.append(
        (generation, reason)
    )
    host._place_satellite = lambda camera: None
    return host


@unittest.skipIf(find_spec("PySide6") is None, "PySide6 is not installed")
class TestSatelliteLifecycle(unittest.TestCase):
    def test_hiding_and_showing_at_the_same_camera_requests_a_frame(self):
        camera = _camera()
        host = _satellite_host(camera, visible=True)
        host._satellite_source_camera = _camera()
        host._satellite_visible_generation = 3
        host._satellite_pending_tokens = {1: ("9", 4)}
        host.set_satellite_visible(False)
        host.set_satellite_visible(True)
        # Hiding withdraws the retained frame, so re-showing at the same camera
        # must request a fresh one instead of accepting the withdrawn frame.
        self.assertEqual(host._satellite_timer.starts, 1)
        self.assertFalse(host._satellite_covers(camera))
        self.assertIsNone(host._satellite_source_camera)
        self.assertIsNone(host._satellite_visible_generation)
        self.assertEqual(host._satellite_pending_tokens, {})
        for slot in (0, 1):
            self.assertEqual(host._satellite.properties[f"slot{slot}Token"], "")
            self.assertEqual(host._satellite.properties[f"slot{slot}Source"], QUrl(""))

    def test_a_withdrawn_back_slot_frame_is_never_adopted(self):
        camera = _camera()
        host = _satellite_host(camera, visible=True)
        host._satellite_generation = 4
        host._satellite_visible_generation = 3
        host._satellite_source_camera = camera
        host._satellite_job_cameras = {4: camera}
        host._satellite_pending_tokens = {1: ("11", 4)}
        host._satellite.setProperty("slot1Token", "11")
        host._satellite.setProperty("slot1Source", QUrl("image://satellite/4"))

        host.set_satellite_visible(False)
        host.set_satellite_visible(True)
        # The hidden session's load completes late. It belongs to a withdrawn
        # generation and must not become the current frame.
        host._satellite_slot_result(1, "11", True)
        self.assertIsNone(host._satellite_visible_generation)
        self.assertIsNone(host._satellite_source_camera)
        self.assertEqual(host._satellite.properties["slot1Token"], "")
        self.assertEqual(host._satellite.properties["slot1Source"], QUrl(""))

    def test_a_failed_load_is_attributed_to_its_own_generation(self):
        host = _satellite_host(_camera(), visible=True)
        host._satellite_generation = 9
        host._satellite_pending_tokens = {1: ("4", 7)}
        host._satellite_slot_result(1, "4", False)
        self.assertEqual(host.failures, [(7, "image load failed")])

    def test_an_older_frame_does_not_replace_a_newer_one(self):
        host = _satellite_host(_camera(), visible=True)
        host._satellite_visible_generation = 8
        host._satellite_pending_tokens = {1: ("5", 6)}
        host._satellite_slot_result(1, "5", True)
        self.assertEqual(host._satellite_visible_generation, 8)
        self.assertEqual(host._satellite_pending_tokens, {})

    def test_a_raster_adopted_last_presents_the_withheld_imagery(self):
        camera = _camera()
        host = _satellite_host(camera, visible=True)
        # The switch was withdrawn while the raster was missing, so the
        # vector base is presented but imagery is still desired and its
        # composition is already current.
        host._chart_background_identity = True
        host._chart_visible_generation = 7
        host._background_presented = "basemap"
        host._satellite_generation = 6
        host._satellite_job_cameras = {6: camera}
        host._satellite_pending_tokens = {1: ("5", 6)}
        # The fake camera/host has no _zoom, which real chart_placement reads
        # via _satellite_covers; this test is about raster-last presentation.
        host._satellite_covers = lambda camera: True
        host._satellite_slot_result(1, "5", True)
        # The raster arrives last: the imagery is presented now instead of
        # staying desynchronised from the desired background.
        self.assertEqual(host._satellite_visible_generation, 6)
        self.assertEqual(host._background_presented, "satellite")


class TestLayerReport(unittest.TestCase):
    """The viewport reports a layer switch only when it changed something."""

    def _emit_block(self):
        source = VIEWPORT.read_text()
        start = source.index("if self._satellite_visible != was_satellite or (")
        end = source.index("):", start)
        return " ".join(source[start:end].split())

    def test_a_satisfied_imagery_request_is_not_reported_again(self):
        # Reporting a request that changed nothing would write back the value
        # the setting already holds, and the assignment notifies again.
        block = self._emit_block()
        self.assertIn('layer == "satellite"', block)
        self.assertIn("not self._satellite_visible", block)

    def test_a_refused_imagery_request_is_still_reported(self):
        block = self._emit_block()
        self.assertIn("self._satellite_visible != was_satellite", block)

    def test_the_scene_owns_the_background_visibility(self):
        source = VIEWPORT.read_text()
        # The scene's bindings derive visibility; no imperative write may
        # replace them, or the fade and the stored state disagree.
        self.assertNotIn('self._weather.setProperty("visible"', source)


class TestSceneFadeBindings(unittest.TestCase):
    """The two scene fades are one transition, not competing animations."""

    def test_the_background_fade_is_the_scene_duration(self):
        scene = SCENE_QML.read_text()
        self.assertIn("duration: root.backgroundFadeMs", scene)
        self.assertIn("opacity: root.satellitePresented ? 0.9 : 0", scene)

    def test_the_basemap_binding_covers_both_backgrounds(self):
        scene = SCENE_QML.read_text()
        self.assertIn("visible: root.basemapVisible || root.satelliteVisible", scene)

    def test_the_chart_layer_fade_is_reset_once_it_finishes(self):
        layer = CHART_LAYER_QML.read_text()
        self.assertIn("interval: root.fadeMs", layer)
        self.assertIn("root.fadeMs = 0", layer)
