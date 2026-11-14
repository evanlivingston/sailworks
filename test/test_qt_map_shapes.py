"""Compatibility of the viewport overlay's shape payloads.

The canvas overlay builds its payload from any duck-typed host. Hosts written
before the map-interaction mixin existed have no shape collection hooks; they
must publish an empty shape payload rather than break the overlay build.
"""

import pytest

pytest.importorskip("PySide6")

import math  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from sailworks.core.geo.mapshapes import KIND_AVOID, KIND_NOTE  # noqa: E402
from sailworks.qt.mapinteraction import (  # noqa: E402
    MapInteractionMixin,
    MapShapeDrawingMixin,
)
from sailworks.qt.viewportmap import (  # noqa: E402
    _EMPTY_SHAPES_KEY,
    _host_shape_draft_payload,
    _host_shapes_key,
    _host_shapes_overlay_payload,
    _host_supports_shapes,
)


class _LegacyHost:
    """A duck-typed overlay host with no map-interaction mixin."""


class _MixinHost:
    """The hooks a real viewport map inherits from the interaction mixin."""

    def shape_draft_payload(self):
        return {"kind": "route", "points": [], "preview": None, "error": ""}

    def map_shapes_key(self):
        return (2, (("uid-1", 4, True, True),), ("route",))

    def map_shapes_overlay_payload(self):
        return {
            "revision": 2,
            "shapes": [{"id": "uid-1", "label": "Windward line"}],
            "draft": self.shape_draft_payload(),
            "key": self.map_shapes_key(),
        }


def test_legacy_host_has_stable_empty_shape_key():
    host = _LegacyHost()
    assert _host_shapes_key(host) == _EMPTY_SHAPES_KEY
    assert _host_shapes_key(host) == _host_shapes_key(host)


def test_legacy_host_publishes_list_shaped_shape_payload():
    payload = _host_shapes_overlay_payload(_LegacyHost())
    assert payload["shapes"] == []
    assert payload["revision"] == 0
    assert payload["draft"] is None
    assert payload["key"] == _EMPTY_SHAPES_KEY


def test_legacy_host_draft_is_none():
    assert _host_shape_draft_payload(_LegacyHost()) is None


def test_non_callable_shape_hooks_fall_back():
    host = _LegacyHost()
    host.map_shapes_key = ("stale",)
    host.map_shapes_overlay_payload = {"shapes": None}
    host.shape_draft_payload = {"kind": "route"}
    assert _host_shapes_key(host) == _EMPTY_SHAPES_KEY
    assert _host_shapes_overlay_payload(host)["shapes"] == []
    assert _host_shape_draft_payload(host) is None


def test_legacy_host_overlay_keeps_the_original_payload_keys():
    """A shape-less host publishes no shape keys at all.

    Duck-typed hosts compare the overlay payload exactly, so a host without
    the interaction mixin must not gain ``map_shapes`` or ``shape_draft``
    entries.
    """
    assert _host_supports_shapes(_LegacyHost()) is False


def test_non_callable_shape_overlay_hook_is_not_shape_capable():
    host = _LegacyHost()
    host.map_shapes_overlay_payload = {"shapes": None}
    assert _host_supports_shapes(host) is False


def test_shape_capable_host_still_publishes_shape_payloads():
    host = _MixinHost()
    assert _host_supports_shapes(host) is True
    assert _host_shapes_overlay_payload(host) == host.map_shapes_overlay_payload()
    assert _host_shape_draft_payload(host)["kind"] == "route"


def test_mixin_host_payload_and_key_pass_through():
    host = _MixinHost()
    payload = _host_shapes_overlay_payload(host)
    assert payload == host.map_shapes_overlay_payload()
    assert payload["shapes"] == [{"id": "uid-1", "label": "Windward line"}]
    assert _host_shapes_key(host) == (2, (("uid-1", 4, True, True),), ("route",))
    assert _host_shape_draft_payload(host)["kind"] == "route"


class _FakeElement:
    def __init__(self, uid, vertices, kind):
        self.uid = uid
        self.name = ""
        self.note = ""
        self.kind = kind
        self.revision = 1
        self.visible = True
        self.enabled = True
        self.colour = ""
        self.vertices = list(vertices)

    @property
    def id(self):
        # The overlay payload derives both the shape dict "id" and the edit
        # "uid" from the stored element's uid, so the two name the same shape.
        return self.uid

    @property
    def vertices(self):
        return list(self._vertices)

    @vertices.setter
    def vertices(self, value):
        # Mirror Shape's validating setter for avoidance polygons: a corner
        # drag and an edge insert both rely on it refusing geometry that the
        # polygon cannot hold. Notes stay permissive, as stored.
        from sailworks.core.geo.mapshapes import (
            MIN_POLYGON_VERTICES,
            validate_vertices,
        )

        value = tuple(tuple(point) for point in value)
        if self.kind == KIND_AVOID and len(value) >= MIN_POLYGON_VERTICES:
            validate_vertices(self.kind, value)
        self._vertices = list(value)
        self.revision += 1

    def to_map_shape(self):
        return SimpleNamespace(
            id=self.uid,
            name=self.name,
            note=self.note,
            kind=self.kind,
            vertices=[tuple(point) for point in self.vertices],
            colour=self.colour,
        )


class _FakeCollection:
    """Stand-in for the session's ShapeCollection."""

    def __init__(self):
        self.elements = []
        self.revision = 0
        self.saves = 0
        self.fail_save = False

    def save(self):
        if self.fail_save:
            raise OSError("disk is full")
        self.saves += 1

    def create(self, points, kind=KIND_NOTE, save=True):
        element = _FakeElement(f"shape-{len(self.elements) + 1}", points, kind)
        self.elements.append(element)
        self.revision += 1
        return element

    def __iter__(self):
        return iter(self.elements)


class _FakeSignal:
    def __init__(self):
        self.emissions = 0

    def emit(self):
        self.emissions += 1


class _DrawingHost(MapShapeDrawingMixin):
    """A drawing host with no Qt surface, driven through the real mixin."""

    def __init__(self, controller):
        self.controller = controller
        self._shape_draft_kind = None
        self._shape_draft_points = []
        self._shape_draft_preview = None
        self._shape_edit_uid = None
        self._shape_edit_edge = None
        self._shape_edit_point = None
        self._shape_edit_vertex = None
        self._shape_edit_drag_vertex = None
        self._shape_edit_drag_point = None
        self._shape_edit_drag_origin = None
        self._shape_edit_drag_vertices = None
        self._measure_points = ()
        self._measure_mode = False
        self._shape_error = ""
        self._viewport_overlay_cache = None
        self.shapes_changed = _FakeSignal()
        self.publish_count = 0
        self.cancel_moves = 0

    def setCursor(self, cursor):
        pass

    def _cancel_cursor(self):
        pass

    def cancel_move(self):
        self.cancel_moves += 1

    def _clear_measurement(self):
        self._measure_mode = False
        self._measure_points = ()

    # -- projection ----------------------------------------------------
    #
    # Zoom 8 over an 800x600 surface, centred on (0, 0): one degree of
    # longitude is 65536 / 360 px, so the test polygon's edges land on
    # predictable pixel columns and rows.

    def scene_projection(self):
        return (0.0, 0.0, 8.0, 0.0, 0.0)

    # The edge projection in the mixin calls this directly; bind the real
    # implementation rather than duplicating the mercator maths here.
    _mercator_y = staticmethod(MapInteractionMixin._mercator_y)

    def viewport_rect(self):
        return SimpleNamespace(width=lambda: 800.0, height=lambda: 600.0)

    def convert_screen_to_geographic(self, x, y):
        world = 256.0 * (2.0**8.0)
        longitude = (float(x) - 400.0) * 360.0 / world
        y_mercator = world / 2.0 + (float(y) - 300.0)
        latitude = math.degrees(
            math.atan(math.sinh(math.pi * (1.0 - 2.0 * y_mercator / world)))
        )
        return SimpleNamespace(get_degrees=lambda: (latitude, longitude))

    def apply_map_cursor(self, shape=None):
        pass

    def setFocus(self):
        pass

    def update(self):
        pass

    def publish_route_overlay(self, force=False):
        self.publish_count += 1


def _draw_note_polygon(host):
    assert host.begin_shape_drawing(KIND_NOTE)
    host._shape_draft_points = [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]
    return host.finish_shape_drawing()


def test_finish_shape_drawing_uses_session_core_collection():
    collection = _FakeCollection()
    controller = SimpleNamespace(core=SimpleNamespace(shapeManager=collection))
    host = _DrawingHost(controller)

    key_before = host.map_shapes_key()
    shape = _draw_note_polygon(host)

    assert shape is not None
    assert host._shape_error == ""
    assert [element.uid for element in collection.elements] == [shape.uid]
    assert host.shapes_changed.emissions == 1

    payload = host.map_shapes_overlay_payload()
    assert [item["id"] for item in payload["shapes"]] == [shape.uid]
    assert payload["shapes"][0]["kind"] == KIND_NOTE
    assert payload["revision"] == collection.revision
    assert host.map_shapes_key() != key_before


def test_overlay_colour_uses_the_custom_value_or_the_kind_default():
    collection = _FakeCollection()
    controller = SimpleNamespace(core=SimpleNamespace(shapeManager=collection))
    host = _DrawingHost(controller)

    note = collection.create([(1.0, 2.0)], kind=KIND_NOTE)
    avoid = collection.create([(0.0, 0.0), (0.0, 2.0), (2.0, 2.0)], kind=KIND_AVOID)
    avoid.colour = "#102030"

    payload = host.map_shapes_overlay_payload()
    colours = {item["id"]: item["colour"] for item in payload["shapes"]}
    assert colours[note.uid] == [0.16, 0.55, 0.86]
    assert colours[avoid.uid] == [16 / 255, 32 / 255, 48 / 255]


def test_colour_edit_invalidates_the_overlay_cache_key():
    collection = _FakeCollection()
    controller = SimpleNamespace(core=SimpleNamespace(shapeManager=collection))
    host = _DrawingHost(controller)
    shape = collection.create([(1.0, 2.0)], kind=KIND_NOTE)

    key_before = host.map_shapes_key()
    shape.colour = "#AA0000"
    shape.revision += 1

    assert host.map_shapes_key() != key_before
    payload = host.map_shapes_overlay_payload()
    assert payload["shapes"][0]["colour"] == [170 / 255, 0.0, 0.0]


def test_finish_shape_drawing_still_accepts_direct_controller_manager():
    collection = _FakeCollection()
    controller = SimpleNamespace(shapeManager=collection)
    host = _DrawingHost(controller)

    assert _draw_note_polygon(host) is not None
    assert [item["id"] for item in host.map_shapes_overlay_payload()["shapes"]] == [
        "shape-1"
    ]


def test_finish_shape_drawing_without_a_collection_reports_the_error():
    controller = SimpleNamespace(core=SimpleNamespace(shapeManager=None))
    host = _DrawingHost(controller)

    assert _draw_note_polygon(host) is None
    assert host._shape_error == "shape storage is unavailable"
    assert host.map_shapes_overlay_payload()["shapes"] == []


# -- edge edit mode -----------------------------------------------------

_SQUARE = ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0))


def _editing_host():
    collection = _FakeCollection()
    controller = SimpleNamespace(core=SimpleNamespace(shapeManager=collection))
    host = _DrawingHost(controller)
    shape = collection.create(_SQUARE, kind=KIND_AVOID)
    assert host.begin_shape_edit(shape.uid)
    return host, collection, shape


def test_hover_snaps_the_preview_to_the_nearest_edge():
    host, _collection, shape = _editing_host()

    assert host._update_shape_edit_hover(491.0, 300.0)

    edit = host.map_shapes_overlay_payload()["edit"]
    assert edit["uid"] == shape.uid
    assert edit["edge"] == 0
    assert edit["point"][0] == pytest.approx(0.0, abs=1e-9)
    assert edit["point"][1] == pytest.approx(0.5, abs=1e-3)


def test_hover_ignores_a_pointer_far_from_every_edge():
    host, _collection, _shape = _editing_host()

    assert host._update_shape_edit_hover(100.0, 100.0) is False
    assert host.map_shapes_overlay_payload()["edit"]["point"] is None


def test_click_inserts_a_snapped_vertex_and_persists_it():
    host, collection, shape = _editing_host()

    assert host._record_shape_edit_click(491.0, 300.0)

    vertices = shape.vertices
    assert len(vertices) == 5
    assert tuple(vertices[1]) == pytest.approx((0.0, 0.5), abs=1e-3)
    assert collection.saves == 1
    assert host.shapes_changed.emissions == 1
    # Edit mode stays on so more vertices can be added.
    assert host.is_shape_editing()
    assert host.map_shapes_overlay_payload()["edit"]["point"] is None
    assert len(host.map_shapes_overlay_payload()["shapes"][0]["vertices"]) == 5


def test_click_on_the_closing_edge_appends_a_vertex():
    host, _collection, shape = _editing_host()

    # Midpoint of the segment from the last vertex back to the first.
    assert host._update_shape_edit_hover(400.0, 202.0)
    assert host.map_shapes_overlay_payload()["edit"]["edge"] == 3

    assert host._record_shape_edit_click(400.0, 202.0)

    vertices = shape.vertices
    assert len(vertices) == 5
    assert tuple(vertices[-1]) == pytest.approx((0.5384, 0.0), abs=1e-3)


def test_click_away_from_an_edge_does_not_insert():
    host, collection, shape = _editing_host()

    assert host._record_shape_edit_click(100.0, 100.0) is False
    assert len(shape.vertices) == 4
    assert collection.saves == 0
    assert host.shapes_changed.emissions == 0


def test_click_on_an_existing_vertex_is_rejected():
    host, collection, shape = _editing_host()

    assert host._record_shape_edit_click(400.0, 300.0)

    assert len(shape.vertices) == 4
    assert collection.saves == 0
    assert "already exists" in host.map_shapes_overlay_payload()["edit"]["error"]


def test_save_failure_restores_the_geometry_and_surfaces_the_error():
    host, collection, shape = _editing_host()
    collection.fail_save = True

    assert host._record_shape_edit_click(491.0, 300.0)

    assert len(shape.vertices) == 4
    assert collection.saves == 0
    assert "could not save" in host.map_shapes_overlay_payload()["edit"]["error"]


def test_cancel_shape_edit_restores_the_plain_payload():
    host, _collection, _shape = _editing_host()
    host._update_shape_edit_hover(491.0, 300.0)

    assert host.cancel_shape_edit() is True

    assert host.is_shape_editing() is False
    assert host.map_shapes_overlay_payload()["edit"] is None
    assert host.cancel_shape_edit() is False


def test_edit_payload_and_key_track_the_hover_state():
    host, _collection, shape = _editing_host()

    key_idle = host.map_shapes_key()
    assert host.publish_count >= 1

    host._update_shape_edit_hover(491.0, 300.0)
    key_hovered = host.map_shapes_key()
    assert key_hovered != key_idle

    edit = host.map_shapes_overlay_payload()["edit"]
    assert edit["uid"] == shape.uid
    assert edit["edge"] == 0

    host._update_shape_edit_hover(100.0, 100.0)
    assert host.map_shapes_key() != key_hovered
    assert host.map_shapes_overlay_payload()["edit"]["point"] is None


def test_begin_shape_edit_retires_draft_move_and_measurement():
    collection = _FakeCollection()
    controller = SimpleNamespace(core=SimpleNamespace(shapeManager=collection))
    host = _DrawingHost(controller)
    shape = collection.create(_SQUARE, kind=KIND_NOTE)

    assert host.begin_shape_drawing(KIND_NOTE)
    host._measure_mode = True
    host._measure_points = ((0.0, 0.0),)

    assert host.begin_shape_edit(shape.uid) is True
    assert host.is_shape_drawing() is False
    assert host._measure_mode is False
    assert host._measure_points == ()
    assert host.cancel_moves >= 1


def test_begin_shape_edit_refuses_an_unknown_shape():
    collection = _FakeCollection()
    controller = SimpleNamespace(core=SimpleNamespace(shapeManager=collection))
    host = _DrawingHost(controller)

    assert host.begin_shape_edit("missing") is False
    assert host.is_shape_editing() is False
    assert host.map_shapes_overlay_payload()["edit"] is None


# -- corner drag --------------------------------------------------------


def test_hover_over_a_corner_prefers_the_vertex_over_the_edge():
    host, _collection, _shape = _editing_host()
    key_idle = host.map_shapes_key()

    assert host._update_shape_edit_hover(400.0, 300.0)

    edit = host.map_shapes_overlay_payload()["edit"]
    assert edit["vertex"] == 0
    assert edit["edge"] is None
    assert edit["point"] is None
    assert edit["dragging"] is False
    assert host.map_shapes_key() != key_idle

    # Moving back onto the edge keeps the snapped insertion preview.
    assert host._update_shape_edit_hover(491.0, 300.0)
    edit = host.map_shapes_overlay_payload()["edit"]
    assert edit["vertex"] is None
    assert edit["edge"] == 0


def test_corner_drag_moves_the_vertex_and_saves_once():
    host, collection, shape = _editing_host()

    assert host._shape_edit_press(400.0, 300.0) is True
    assert host.is_shape_vertex_dragging() is True
    assert host._shape_edit_move(350.0, 330.0) is True

    edit = host.map_shapes_overlay_payload()["edit"]
    assert edit["dragging"] is True
    assert edit["drag"]["index"] == 0
    assert edit["drag"]["point"][0] == pytest.approx(-0.1648, abs=1e-3)
    assert edit["drag"]["point"][1] == pytest.approx(-0.2747, abs=1e-3)
    # Nothing is written while the pointer is still down.
    assert collection.saves == 0
    assert host.shapes_changed.emissions == 0

    assert host._shape_edit_release(350.0, 330.0) is True

    assert host.is_shape_vertex_dragging() is False
    assert host.is_shape_editing() is True
    assert tuple(shape.vertices[0]) == pytest.approx((-0.1648, -0.2747), abs=1e-3)
    assert collection.saves == 1
    assert host.shapes_changed.emissions == 1
    edit = host.map_shapes_overlay_payload()["edit"]
    assert edit["drag"] is None
    assert edit["vertex"] == 0
    assert edit["error"] == ""


_QML_SCENE = (
    Path(__file__).resolve().parents[1]
    / "sailworks"
    / "qt"
    / "shaders"
    / "ViewportChartScene.qml"
)


def test_drag_payload_is_a_lat_lon_vertex_substitution():
    """The canvas swaps this point in for one committed corner.

    The paint builds a live ring with the dragged corner replaced, so the
    payload point must be stored in the same (lat, lon) order as the shape's
    own vertices and name the corner by index.
    """
    host, _collection, shape = _editing_host()
    committed = [tuple(point) for point in shape.vertices]

    assert host._shape_edit_press(400.0, 300.0) is True
    assert host._shape_edit_move(350.0, 330.0) is True

    edit = host.map_shapes_overlay_payload()["edit"]
    assert edit["uid"] == shape.id
    assert edit["dragging"] is True
    drag = edit["drag"]

    index = drag["index"]
    assert 0 <= index < len(committed)
    point = drag["point"]
    assert len(point) == 2
    assert all(isinstance(value, (int, float)) for value in point)
    assert -90.0 <= point[0] <= 90.0
    assert -180.0 <= point[1] <= 180.0

    live = list(committed)
    live[index] = (point[0], point[1])
    assert live[index] != committed[index]

    # Only the preview moved; the stored ring waits for release.
    assert [tuple(p) for p in shape.vertices] == committed


def test_qml_paints_the_dragged_corner_from_the_drag_payload():
    """QML is not executable here, so pin the painting seam instead.

    Every polygon pass for the edited shape must read one per-frame ring in
    which the dragged corner is substituted; reading ``shape.vertices``
    inside the paint loop is exactly the bug where only the handle moves.
    """
    source = _QML_SCENE.read_text(encoding="utf-8")
    assert "function liveShapeVertices(shape, edit)" in source

    draw_body = source.split("function drawMapShapes(context)", 1)[1]
    draw_body = draw_body.split("onPaint:", 1)[0]
    assert "liveShapeVertices(shape, edit)" in draw_body
    assert "shape.vertices" not in draw_body


def test_corner_drag_press_is_consumed_so_the_host_does_not_pan():
    host, _collection, _shape = _editing_host()

    # The pointer host only arms pan/measurement when the press hook declines.
    assert host._shape_edit_press(400.0, 300.0) is True
    assert host.is_shape_vertex_dragging() is True
    assert host._shape_edit_move(420.0, 300.0) is True
    assert host._shape_edit_release(420.0, 300.0) is True

    # Outside edit mode the same hooks decline, so pan keeps working.
    host.cancel_shape_edit()
    assert host._shape_edit_press(400.0, 300.0) is False
    assert host._shape_edit_move(420.0, 300.0) is False
    assert host._shape_edit_release(420.0, 300.0) is False


def test_invalid_corner_drag_rolls_back_and_reports_the_error():
    host, collection, shape = _editing_host()

    assert host._shape_edit_press(400.0, 300.0) is True
    # Dropping the corner above the opposite edge crosses the ring.
    assert host._shape_edit_move(491.0, 100.0) is True
    assert host._shape_edit_release(491.0, 100.0) is True

    assert tuple(shape.vertices[0]) == pytest.approx((0.0, 0.0), abs=1e-9)
    assert collection.saves == 0
    assert host.shapes_changed.emissions == 0
    assert host.is_shape_vertex_dragging() is False
    assert host.is_shape_editing() is True
    error = host.map_shapes_overlay_payload()["edit"]["error"]
    assert "cross" in error or "touch" in error


def test_noop_corner_drag_does_not_save():
    host, collection, shape = _editing_host()

    assert host._shape_edit_press(400.0, 300.0) is True
    assert host._shape_edit_release(400.0, 300.0) is True

    assert collection.saves == 0
    assert host.shapes_changed.emissions == 0
    assert tuple(shape.vertices[0]) == pytest.approx((0.0, 0.0), abs=1e-9)


def test_escape_cancels_a_corner_drag_without_saving():
    host, collection, shape = _editing_host()

    assert host._shape_edit_press(400.0, 300.0) is True
    assert host._shape_edit_move(350.0, 330.0) is True

    assert host.cancel_shape_edit() is True

    assert host.is_shape_vertex_dragging() is False
    assert host.is_shape_editing() is False
    assert collection.saves == 0
    assert host.shapes_changed.emissions == 0
    assert tuple(shape.vertices[0]) == pytest.approx((0.0, 0.0), abs=1e-9)
    assert host.map_shapes_overlay_payload()["edit"] is None
