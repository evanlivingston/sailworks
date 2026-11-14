"""Direct track-point drags must project with the camera the scene draws.

The retained scene pans by translating its content item and keeps publishing
the overlay camera from before that pan.  A drag that converts pointer
positions with the widget camera therefore previews and commits a point that
is offset by the pan, so the mark snaps on release.  These tests pin the
inverse projection, the preview/commit agreement and the per-move work the
direct drag is allowed to do.
"""

import math
from types import MethodType, SimpleNamespace

import pytest
from PySide6.QtCore import QPointF, QRectF

from sailworks.qt.mapinteraction import MapInteractionMixin

MAX_LATITUDE = 85.05112878


def _mercator_y(latitude, world):
    radians = math.radians(max(-MAX_LATITUDE, min(MAX_LATITUDE, latitude)))
    return world * (1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0


def _screen_point(
    latitude,
    longitude,
    *,
    center_lat,
    center_lon,
    zoom,
    offset_x,
    offset_y,
    width=256.0,
    height=256.0,
):
    """Forward projection with the scene camera, as the QML layer draws."""
    world = 256.0 * (2.0**zoom)
    dx = (longitude - center_lon) * world / 360.0
    dx -= round(dx / world) * world
    x = width / 2.0 + dx + offset_x
    y = (
        height / 2.0
        + _mercator_y(latitude, world)
        - _mercator_y(center_lat, world)
        + offset_y
    )
    return QPointF(x, y)


class _Track(list):
    def __init__(self, points, *, visible=True, name="Track"):
        super().__init__(points)
        self.visible = visible
        self.name = name
        self.revision = 1
        self.color = None


class _DirectCanvas(MapInteractionMixin):
    _zoom = 0.0
    _center_lat = 0.0
    _center_lon = 0.0

    def __init__(self, offset_x=0.0, offset_y=0.0):
        self.offset = (offset_x, offset_y)
        self.navigation = SimpleNamespace(edit_track=None, hl_track_hover=None)
        self.move_callback = None
        self._move_preview_callback = None
        self._move_restore_callback = None
        self._move_direct_point = False
        self._move_press_position = None
        self.updates = 0
        self.conversions = 0

    def viewport_rect(self):
        return QRectF(0, 0, 256, 256)

    def scene_projection(self):
        return (
            self._center_lat,
            self._center_lon,
            self._zoom,
            self.offset[0],
            self.offset[1],
        )

    def update(self):
        self.updates += 1

    def clear_move_preview(self):
        self._move_preview_callback = None

    def _cancel_cursor(self):
        pass

    def _cancel_inertia(self):
        pass

    def _finish_zoom_silent(self):
        pass

    def setCursor(self, cursor):
        pass

    def setFocus(self):
        pass

    def convert_screen_to_geographic(self, x, y):
        self.conversions += 1
        return SimpleNamespace(get_degrees=lambda: (float(y), float(x)))


def test_scene_inverse_round_trips_mark_hit_test_after_pan():
    track = _Track([(12.5, -3.25), (48.0, 11.0)])
    canvas = _DirectCanvas(offset_x=37.0, offset_y=-21.0)
    canvas._zoom = 4.0
    canvas._center_lat = 10.0
    canvas._center_lon = 4.0
    canvas.navigation.edit_track = track

    for latitude, longitude in track:
        screen = _screen_point(
            latitude,
            longitude,
            center_lat=canvas._center_lat,
            center_lon=canvas._center_lon,
            zoom=canvas._zoom,
            offset_x=canvas.offset[0],
            offset_y=canvas.offset[1],
        )
        assert canvas.edit_track_mark_at(screen) == track.index((latitude, longitude))
        point_latitude, point_longitude = canvas.scene_geographic_at(screen)
        assert point_latitude == pytest.approx(latitude, abs=1e-9)
        assert point_longitude == pytest.approx(longitude, abs=1e-9)


def test_scene_inverse_wraps_longitude_across_the_dateline():
    canvas = _DirectCanvas(offset_x=8.0)
    canvas._zoom = 3.0
    canvas._center_lat = 0.0
    canvas._center_lon = 179.5

    screen = _screen_point(
        -16.0,
        -179.5,
        center_lat=canvas._center_lat,
        center_lon=canvas._center_lon,
        zoom=canvas._zoom,
        offset_x=canvas.offset[0],
        offset_y=canvas.offset[1],
    )
    latitude, longitude = canvas.scene_geographic_at(screen)
    assert latitude == pytest.approx(-16.0, abs=1e-9)
    assert longitude == pytest.approx(-179.5, abs=1e-9)


def test_scene_inverse_clamps_mercator_latitude():
    canvas = _DirectCanvas()
    canvas._zoom = 2.0
    latitude, _ = canvas.scene_geographic_at(QPointF(128.0, -10000.0))
    assert latitude == pytest.approx(MAX_LATITUDE)
    latitude, _ = canvas.scene_geographic_at(QPointF(128.0, 10000.0))
    assert latitude == pytest.approx(-MAX_LATITUDE)


def test_direct_track_drag_previews_with_scene_projection_without_repaint():
    canvas = _DirectCanvas(offset_x=-64.0, offset_y=32.0)
    canvas._zoom = 5.0
    previews = []
    canvas.begin_move(
        lambda coordinates: None, preview=previews.append, direct_point=True
    )
    assert canvas._begin_move_drag(QPointF(140.0, 120.0)) is True

    position = QPointF(203.0, 97.0)
    updates = canvas.updates
    conversions = canvas.conversions
    assert canvas._move_preview(position) is True

    assert canvas.conversions == conversions
    assert canvas.updates == updates
    assert previews[-1] == pytest.approx(canvas.scene_geographic_at(position))


def test_direct_track_drag_commit_matches_preview():
    canvas = _DirectCanvas(offset_x=25.0, offset_y=-12.0)
    canvas._zoom = 3.0
    previews = []
    commits = []
    canvas.begin_move(commits.append, preview=previews.append, direct_point=True)
    canvas._begin_move_drag(QPointF(100.0, 100.0))

    position = QPointF(161.0, 84.0)
    canvas._move_preview(position)
    assert canvas._finish_move_drag(position) is True

    assert commits == [previews[-1]]
    assert commits[0] == pytest.approx(canvas.scene_geographic_at(position))


def test_generic_move_keeps_widget_camera_and_repaint():
    canvas = _DirectCanvas(offset_x=50.0)
    previews = []
    commits = []
    canvas.begin_move(commits.append, preview=previews.append)
    canvas._begin_move_drag(QPointF(10.0, 10.0))

    position = QPointF(30.0, 40.0)
    updates = canvas.updates
    conversions = canvas.conversions
    assert canvas._move_preview(position) is True
    assert previews == [(40.0, 30.0)]
    assert canvas.conversions == conversions + 1
    assert canvas.updates == updates + 1

    assert canvas._finish_move_drag(position) is True
    assert commits == [(40.0, 30.0)]


class _TrackManager(list):
    def __init__(self, tracks):
        super().__init__(tracks)
        self.revision = 1
        self.active = tracks[0] if tracks else None

    def is_active(self, track):
        return track is self.active


def _preview_host(tracks):
    from sailworks.qt import viewportmap

    manager = _TrackManager(tracks)
    previews = []
    host = SimpleNamespace(
        controller=SimpleNamespace(core=SimpleNamespace(trackManager=manager)),
        navigation=SimpleNamespace(
            edit_track=None, hl_track_item=None, hl_track_hover=None
        ),
        previews=previews,
        set_move_preview=previews.append,
    )
    # ``preview_move_point`` is a real ViewportMap method; give the stand-in
    # the track-index helper it calls on ``self`` without a widget instance.
    host._move_preview_track_index = MethodType(
        viewportmap.ViewportMap._move_preview_track_index, host
    )
    return host, manager


def test_preview_move_point_reuses_the_track_index_between_moves(monkeypatch):
    from sailworks.qt import viewportmap

    calls = []
    real = viewportmap._overlay_tracks

    def counting(host):
        calls.append(host)
        return real(host)

    monkeypatch.setattr(viewportmap, "_overlay_tracks", counting)

    tracks = [_Track([(0.0, 0.0)], name="A"), _Track([(1.0, 1.0)], name="B")]
    host, manager = _preview_host(tracks)
    element = tracks[1]

    for offset in (0.0, 0.1, 0.2, 0.3):
        viewportmap.ViewportMap.preview_move_point(host, element, 0, 1.0 + offset, 1.0)

    assert len(calls) == 1
    assert host.previews[-1][0] == 1

    # Hiding the preceding track shifts the visible position.
    manager[0].visible = False
    viewportmap.ViewportMap.preview_move_point(host, element, 0, 2.0, 2.0)
    assert len(calls) == 2
    assert host.previews[-1][0] == 0

    # Showing it again puts the element back at the second visible position.
    manager[0].visible = True
    viewportmap.ViewportMap.preview_move_point(host, element, 0, 3.0, 3.0)
    assert len(calls) == 3
    assert host.previews[-1][0] == 1

    # Reordering the manager shifts the visible position back to the front.
    manager.remove(element)
    manager.insert(0, element)
    viewportmap.ViewportMap.preview_move_point(host, element, 0, 4.0, 4.0)
    assert len(calls) == 4
    assert host.previews[-1][0] == 0

    # Hiding a later track leaves the cached position valid.
    manager[1].visible = False
    viewportmap.ViewportMap.preview_move_point(host, element, 0, 5.0, 5.0)
    assert len(calls) == 4
    assert host.previews[-1][0] == 0


def test_clear_move_preview_drops_the_cached_track_index(monkeypatch):
    from sailworks.qt import viewportmap

    calls = []
    real = viewportmap._overlay_tracks

    def counting(host):
        calls.append(host)
        return real(host)

    monkeypatch.setattr(viewportmap, "_overlay_tracks", counting)

    tracks = [_Track([(0.0, 0.0)], name="A")]
    host, _ = _preview_host(tracks)

    viewportmap.ViewportMap.preview_move_point(host, tracks[0], 0, 1.0, 1.0)
    viewportmap.ViewportMap.clear_move_preview(host)
    assert host.previews == [[0, 0, 1.0, 1.0], []]
    viewportmap.ViewportMap.preview_move_point(host, tracks[0], 0, 2.0, 2.0)
    assert len(calls) == 2


class _PointElement:
    """The scene element contract ``Window._move_scene_point`` reads."""

    def __init__(self, points):
        self.points = [tuple(point) for point in points]

    def __len__(self):
        return len(self.points)

    def __getitem__(self, index):
        return self.points[index]


class _MoveMap:
    def __init__(self):
        self.begins = []
        self.preview = None
        self.move = None

    def begin_move(self, callback, preview=None, restore=None, direct_point=False):
        self.move = callback
        self.preview = preview
        self.restore = restore
        self.begins.append(direct_point)


class _Views:
    def __init__(self):
        self.current = None

    def setCurrentWidget(self, widget):
        self.current = widget


def _window_move_host():
    from sailworks.qt.window import PreviewWindow

    host = SimpleNamespace(
        map=_MoveMap(),
        views=_Views(),
        undo_stack=None,
        _refresh_scene_point=lambda: None,
    )
    # Bind the real handlers without constructing a full main window.
    host._move_scene_point = MethodType(PreviewWindow._move_scene_point, host)
    host._move_scene_mark = MethodType(PreviewWindow._move_scene_mark, host)
    return host


def test_direct_mark_drag_uses_the_scene_camera():
    host = _window_move_host()
    element = _PointElement([(1.0, 2.0)])

    host._move_scene_mark(element, 0)

    assert host.map.begins == [True]
    assert host.views.current is host.map


def test_explicit_move_keeps_the_generic_camera():
    host = _window_move_host()
    element = _PointElement([(1.0, 2.0), (3.0, 4.0)])

    host._move_scene_point(element, 1)

    assert host.map.begins == [False]
    assert host.views.current is host.map
