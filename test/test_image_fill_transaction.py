"""Headless contract for the image-fill scene publication transaction (#119).

The prospective entry points are _stage_image_fill(field, camera), returning
an opaque completion token, and _try_commit_image_fill(token).  The normal
_publish_fill path must use the same transaction.  Layer objects model QML
properties and synchronous signals; provider and publication manager are real.
"""

from dataclasses import replace
from types import SimpleNamespace

from PySide6.QtGui import QImage

from sailworks.qt import gribviewport
from sailworks.qt.gpuweather import WeatherImageProvider
from sailworks.qt.viewportpublication import PublicationManager
from test.test_gribviewport import _camera, _field

SOURCES = ("u0Source", "v0Source", "u1Source", "v1Source")
UNIFORMS = ("blend", "gridX", "gridY", "gridWidth", "gridHeight")


class Layer:
    def __init__(self, visible=False):
        self.visible = visible
        self.properties = {"prepared": False, "shaderError": ""}
        self.on_write = None

    def setProperty(self, name, value):
        self.properties[name] = value
        if self.on_write is not None:
            self.on_write(name)
        return True

    def property(self, name):
        return self.properties.get(name)

    def setVisible(self, visible):
        self.visible = visible

    def setOpacity(self, opacity):
        self.properties["opacity"] = opacity


class Scene:
    """Bind real viewport methods without constructing a QQuickWidget."""

    def __init__(self, provider=None):
        self._fill = Layer(True)
        self._fill_back = Layer()
        self._barbs = Layer(True)
        self._weather = Layer(True)
        self._provider = provider or WeatherImageProvider()
        self._publication_manager = PublicationManager()
        self._publication_manager.set_reference_camera(_camera())
        self._native_weather_fields = False
        # The viewport owns a world underlay layer; None until the QML world
        # layer is bound, which is what a headless scene always sees.
        self._world = None
        self._world_provider = None
        self._chart_layer = True
        self._wind_gradient_enabled = True
        self._wind_gradient_opacity = 1.0
        self._published_fill = None
        self._state = "loading"
        self._disposed = False
        self._camera = _camera()
        # Motion state the real widget initialises in __init__.  The camera
        # pass calls is_moving() while a selection is staged, so these must
        # exist and report an idle gesture rather than fall through the
        # class-descriptor lookup above.
        self._drag = None
        self._drag_velocity = gribviewport.DragVelocity()
        self._pan_inertia = None
        self._pan_inertia_tick = SimpleNamespace(isActive=lambda: False)
        self._wheel_zoom_target = None
        self._wheel_zoom_tick = SimpleNamespace(isActive=lambda: False)
        self._wheel_trackpad_panning = False
        self._native_gesture_active = False
        # Wave layers stay unbound in this headless scene; the camera pass
        # reads their visibility and the retained items on a rebase.
        self._wave_visible = False
        self._wave_arrows_visible = False
        self._wave_fill = None
        self._wave_fill_back = None
        self._wave_barbs = None
        self._wave_barbs_back = None
        self._wave_token = None
        self._wave_barbs_token = None
        self.counters = dict(fill_publications=0, fill_uploads=0)

    def __getattr__(self, name):
        return getattr(gribviewport._viewport_widget_class(), name).__get__(self)

    def _show_state(self, state, *args):
        self._state = state


def field(generation):
    result = _field()
    result.generation = generation
    result.identity = generation
    images = []
    for index in range(4):
        image = QImage(4, 4, QImage.Format.Format_RGBA8888)
        image.fill(0xFF000000 + generation * 16 + index)
        images.append(image)
    result.grid.images = tuple(images)
    return result


def stage(scene, generation):
    return scene._stage_image_fill(field(generation), _camera())


def finish(scene, token):
    scene._fill_back.properties["prepared"] = True
    scene._try_commit_image_fill(token)


def visible_identity(scene):
    return scene._publication_manager.layers["fill"].visible


def test_publish_stages_only_back_and_synchronous_callback_cannot_commit_partial():
    scene = Scene()
    old_front, back = scene._fill, scene._fill_back
    writes = []

    def synchronous_callback(name):
        if name in SOURCES:
            # QML can report a retained prior source ready while assigning the
            # first new source.  The transaction must still be sealed first.
            back.properties["prepared"] = True
            token = scene._publication_manager.layers["fill"].completion_token
            scene._try_commit_image_fill(token)
            writes.append((name, scene._fill, old_front.visible, back.visible))
            back.properties["prepared"] = False

    back.on_write = synchronous_callback
    scene._publish_fill(field(1), (), _camera())

    assert [entry[0] for entry in writes] == list(SOURCES)
    assert all(
        front is old_front and visible and not hidden
        for _, front, visible, hidden in writes
    )
    assert old_front.properties.keys().isdisjoint(SOURCES + UNIFORMS)
    assert all(name in back.properties for name in SOURCES + UNIFORMS)
    assert visible_identity(scene) is None
    assert scene._publication_manager.layers["fill"].pending is not None


def test_prepared_matching_token_promotes_all_and_stale_or_failed_cannot_promote():
    scene = Scene()
    first = stage(scene, 1)
    scene._try_commit_image_fill(first)  # four sources alone are not shader readiness
    assert visible_identity(scene) is None
    finish(scene, first)
    front = scene._fill
    identity = visible_identity(scene)
    assert identity.generation == 1
    assert scene._published_fill == "1/grid"
    assert front.visible and not scene._fill_back.visible
    assert ("1/grid", None) in scene._provider._reservations.values()

    stale = stage(scene, 2)
    current = stage(scene, 3)
    scene._fill_back.properties["prepared"] = True
    scene._try_commit_image_fill(stale)
    assert scene._fill is front and visible_identity(scene) == identity
    assert scene._publication_manager.layers["fill"].pending.generation == 3
    scene._fill_back.properties["shaderError"] = "compile failed"
    scene._try_commit_image_fill(current)
    assert scene._fill is front and visible_identity(scene) == identity
    assert scene._published_fill == "1/grid"
    assert ("1/grid", None) in scene._provider._reservations.values()
    scene._fill_back.properties["shaderError"] = ""
    scene._try_commit_image_fill(current)  # delayed success after failure
    assert scene._fill is front and visible_identity(scene) == identity


def test_failed_pending_image_cancels_without_dropping_visible_front():
    scene = Scene()
    finish(scene, stage(scene, 1))
    front = scene._fill
    pending = stage(scene, 2)
    scene._fill_back.properties["sourceError"] = "weather image unavailable"
    scene._try_commit_image_fill(pending)
    assert scene._fill is front and not front.visible
    assert scene._publication_manager.layers["fill"].pending is None
    assert ("1/grid", None) in scene._provider._reservations.values()
    scene._fill_back.properties["sourceError"] = ""
    scene._fill_back.properties["prepared"] = True
    scene._try_commit_image_fill(pending)
    assert scene._fill is front and scene._published_fill == "1/grid"


def test_rebase_and_hide_show_do_not_restore_old_reference_front():
    scene = Scene()
    finish(scene, stage(scene, 1))
    old_front = scene._fill
    pending = stage(scene, 2)
    scene._set_weather_visible(False)

    # Exercise the actual camera path, with non-weather collaborators stubbed.
    scene._update_shared_map_pan = lambda camera: False
    scene._marine_layer = SimpleNamespace(set_camera=lambda camera: None)
    scene._icoads_layer = SimpleNamespace(set_camera=lambda camera: None)
    scene._chart_model = None
    scene._place_chart = lambda: None
    scene._place_route_overlay = lambda *args: None
    scene._publish_streamlines = lambda camera: None
    scene._wind_style = "barbs"
    scene._model = SimpleNamespace(
        update=lambda camera, ref=None, reuse_steps=0: ("loading", None, None)
    )
    scene._weather_display_camera = _camera()
    scene.cameraChanged = SimpleNamespace(emit=lambda camera: None)
    scene._presentation_spans = []
    scene.devicePixelRatio = lambda: 1.0
    scene._retire_barb_anchor_frame = lambda *args, **kwargs: None
    scene._maybe_arm_barb_zoom_preview = lambda *args, **kwargs: None
    scene._barb_zoom_preview = None
    scene._camera_dpr = 1.0
    scene._barb_zoom_dpr_matches = lambda prev, curr: True
    scene._barb_zoom_preview_holds = lambda cam, dpr: False
    scene._apply_barb_zoom_preview = lambda: False
    scene._clear_barb_zoom_preview = lambda *args: None
    scene._barb_anchor_frame_places = lambda *args: True
    scene._set_barb_anchor_frame_valid = lambda *args: None
    scene.set_camera(replace(_camera(), zoom=_camera().zoom + 1))
    scene._set_weather_visible(True)
    scene._fill_back.properties["prepared"] = True
    scene._try_commit_image_fill(pending)
    assert not old_front.visible
    assert not scene._fill.visible
    assert scene._publication_manager.layers["fill"].pending is None
    assert scene._state in ("loading", "unavailable")


def test_budget_refusal_preserves_front_reservation_and_reports_unavailable():
    # Four distinct 4x4 RGBA images fit exactly; a second field cannot coexist.
    provider = WeatherImageProvider(byte_limit=4 * 4 * 4 * 4)
    scene = Scene(provider)
    finish(scene, stage(scene, 1))
    front, identity = scene._fill, visible_identity(scene)
    front_urls = tuple(front.property(name).toString() for name in SOURCES)
    stage(scene, 2)
    assert scene._state == "unavailable"
    assert scene._fill is front and visible_identity(scene) == identity
    assert not front.visible  # Previous forecast is retained, not shown as current.
    assert ("1/grid", None) in provider._reservations.values()
    assert all(url in provider._field_urls["1/grid"] for url in front_urls)
    assert provider._field_bytes <= provider.byte_limit


def test_hide_show_retains_only_a_valid_committed_front():
    scene = Scene()
    finish(scene, stage(scene, 1))
    front = scene._fill
    pending = stage(scene, 2)
    scene._set_weather_visible(False)
    assert not front.visible and not scene._fill_back.visible
    assert scene._publication_manager.layers["fill"].pending is None
    scene._fill_back.properties["prepared"] = True
    scene._try_commit_image_fill(pending)
    scene._set_weather_visible(True)
    assert scene._fill is front and not front.visible
    assert scene._published_fill == "1/grid"


def test_hide_show_restores_a_matching_committed_field():
    scene = Scene()
    finish(scene, stage(scene, 1))
    front = scene._fill
    scene._set_weather_visible(False)
    assert not front.visible
    scene._set_weather_visible(True)
    assert front.visible
    assert visible_identity(scene).generation == 1


def test_uncompiled_hidden_replacement_times_out_explicitly():
    scene = Scene()
    finish(scene, stage(scene, 1))
    front = scene._fill
    pending = stage(scene, 2)
    assert not front.visible
    scene._image_fill_timeout(pending)
    assert scene._state == "unavailable"
    assert scene._publication_manager.layers["fill"].pending is None
    assert visible_identity(scene).generation == 1
    assert not front.visible
    scene._fill_back.properties["prepared"] = True
    scene._try_commit_image_fill(pending)
    assert scene._fill is front and visible_identity(scene).generation == 1
    assert stage(scene, 2) is False  # No endless restaging of a failed field.
    assert scene._state == "unavailable"
    assert scene._publication_manager.layers["fill"].pending is None
    assert stage(scene, 3)  # A new field may still prepare.


def test_repeated_camera_request_reuses_pending_completion_token():
    scene = Scene()
    first = stage(scene, 1)
    repeated = stage(scene, 1)
    assert repeated is first
    assert scene._publication_manager.layers["fill"].completion_token is first
    scene._image_fill_timeout(first)
    assert scene._state == "unavailable"
    assert scene._publication_manager.layers["fill"].pending is None


def test_teardown_releases_resources_and_rejects_late_completion():
    scene = Scene()
    finish(scene, stage(scene, 1))
    pending = stage(scene, 2)
    old_front = scene._fill
    scene._cancel_image_fill(withdraw=True)
    assert not old_front.visible and not scene._fill_back.visible
    assert scene._publication_manager.layers["fill"].visible is None
    assert scene._publication_manager.layers["fill"].pending is None
    assert scene._publication_manager.budget.total_used() == 0
    assert scene._provider.field_bytes() <= scene._provider.byte_limit
    assert scene._provider._reservations == {}
    scene._fill_back.properties["prepared"] = True
    scene._try_commit_image_fill(pending)
    assert scene._publication_manager.layers["fill"].visible is None
    assert not old_front.visible


def test_two_viewports_sharing_provider_have_independent_transactions():
    provider = WeatherImageProvider()
    first, second = Scene(provider), Scene(provider)
    token1, token2 = stage(first, 1), stage(second, 2)
    finish(first, token1)
    assert visible_identity(first).generation == 1
    assert visible_identity(second) is None
    assert second._publication_manager.layers["fill"].pending.generation == 2
    finish(second, token2)
    assert visible_identity(second).generation == 2
    assert ("1/grid", None) in provider._reservations.values()
    assert ("2/grid", None) in provider._reservations.values()
