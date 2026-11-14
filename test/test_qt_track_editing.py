"""Focused regression coverage for selected-track editing behavior.

Track editing lives in the scene tree since the navigation panel was removed;
these tests drive the ScenePanel entry points and the shared undo commands.
"""

from types import SimpleNamespace

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent, QUndoStack
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget

from sailworks.core.geo.track import Track
from sailworks.core.scene import KIND_LOG, KIND_POI, KIND_ROUTING, KIND_TRACK
from sailworks.qt.mapinteraction import MapInteractionMixin
from sailworks.qt.navigationcommands import MovePointCommand
from sailworks.qt.weatherpresenter import WeatherPresenter, _track_mark_event
from test.test_qt_scenepanel import (
    FakePOI,
    FakeRoute,
    FakeTrack,
    application,
    panel_for,
)


def _node(kind, key, index):
    return ("point", kind, key, index)


def test_rename_point_updates_the_active_track(tmp_path):
    application()
    track = FakeTrack("t1", "Track")
    panel = panel_for(tmp_path, tracks=[track])

    panel._rename_point(_node(KIND_TRACK, "t1", 0), "Start")

    assert track.point_name(0) == "Start"
    assert tuple(track.points[0]) == (51.1234567, -1.2345678)


def test_delete_point_is_undoable(tmp_path):
    application()
    track = FakeTrack("t1", "Track")
    stack = QUndoStack()
    panel = panel_for(tmp_path, tracks=[track], undo_stack=stack)

    panel._delete_point(_node(KIND_TRACK, "t1", 0))

    assert len(track) == 1
    assert stack.count() == 1
    stack.undo()
    assert len(track) == 2
    assert tuple(track.points[0]) == (51.1234567, -1.2345678)


def test_add_point_appends_to_the_track_being_edited(tmp_path):
    application()
    track = FakeTrack("t1", "Track")
    panel = panel_for(tmp_path, tracks=[track])

    added = panel.add_point((5, 6))

    assert added is track
    assert tuple(track.points[-1]) == (5.0, 6.0)


def test_add_point_creates_an_active_track_when_none_exists(tmp_path):
    application()
    panel = panel_for(tmp_path)

    added = panel.add_point((1, 2))

    assert added is not None
    assert panel.core.trackManager.get_active() is added
    panel.add_point((3, 4))
    assert [tuple(point) for point in added.points] == [(1.0, 2.0), (3.0, 4.0)]


def test_move_point_request_emits_element_and_index(tmp_path):
    application()
    track = FakeTrack("t1", "Track")
    panel = panel_for(tmp_path, tracks=[track])
    emitted = []
    panel.move_point_requested.connect(
        lambda element, index: emitted.append((element, index))
    )

    panel._move_point(_node(KIND_TRACK, "t1", 1))

    assert emitted == [(track, 1)]


class _Collection(list):
    """Minimal collection so Track.move can persist."""

    def __init__(self, *tracks):
        super().__init__(tracks)
        for track in self:
            track.collection = self

    def save(self):
        pass


def test_move_point_command_undoes_and_redoes_relocation():
    track = Track("Track", [(1, 2, None)])
    _Collection(track)
    refreshed = []
    command = MovePointCommand(
        lambda: refreshed.append(True),
        track,
        0,
        (1, 2),
        (5, 6),
    )

    command.redo()
    assert track[0] == (5, 6, None)
    command.undo()
    assert track[0] == (1, 2, None)
    assert len(refreshed) == 2


def test_only_edited_track_gets_point_handle_circles():
    from sailworks.rendering.navigation import NavigationRenderer

    class TrackManager(list):
        def is_active(self, track):
            return True

    tracks = [
        Track("First", [(1, 2, None)]),
        Track("Second", [(3, 4, None)]),
    ]
    renderer = NavigationRenderer(
        SimpleNamespace(
            trackManager=TrackManager(tracks), poiManager=[], routingManager=[]
        )
    )
    renderer.edit_track = tracks[1]

    class Cr:
        def __init__(self):
            self.arcs = []

        def arc(self, x, y, radius, *_):
            self.arcs.append((x, y, radius))

        def __getattr__(self, _):
            return lambda *args, **kwargs: None

    cr = Cr()

    class GpsMap:
        def convert_geographic_to_screen(self, point):
            return point.get_degrees()

    renderer.draw_navigation(GpsMap(), cr)
    assert cr.arcs == [(3.0, 4.0, 5)]


class _OverlayTrack(list):
    def __init__(self, name, points, *, visible=True, revision=1):
        super().__init__(points)
        self.name = name
        self.visible = visible
        self.revision = revision
        self.color = None


class _OverlayTrackManager(list):
    def __init__(self, tracks):
        super().__init__(tracks)
        self.revision = 1
        self.active = tracks[0] if tracks else None

    def is_active(self, track):
        return track is self.active


def _overlay_host(tracks):
    manager = _OverlayTrackManager(tracks)
    host = SimpleNamespace(
        controller=SimpleNamespace(core=SimpleNamespace(trackManager=manager)),
        navigation=SimpleNamespace(
            edit_track=None, hl_track_item=None, hl_track_hover=None
        ),
    )
    return host, manager[0]


def test_track_payload_marks_editing_and_hover_only_for_edit_track():
    from sailworks.qt.viewportmap import _overlay_tracks, _track_payload

    first = _OverlayTrack("Editing", [(51.0, -1.0), (51.1, -1.1)])
    second = _OverlayTrack("Other", [(52.0, -2.0)])
    host, track = _overlay_host([first, second])

    payload = _track_payload(track, host)
    assert payload["editing"] is False
    assert payload["hover"] == -1
    assert payload["points"] == [[51.0, -1.0], [51.1, -1.1]]

    before = _overlay_tracks(host)[0]
    host.navigation.edit_track = track
    host.navigation.hl_track_hover = 1
    after = _overlay_tracks(host)[0]
    assert before != after

    payload = _track_payload(track, host)
    assert payload["editing"] is True
    assert payload["hover"] == 1

    other = _track_payload(second, host)
    assert other["editing"] is False
    assert other["hover"] == -1


class _HitCanvas(MapInteractionMixin):
    _zoom = 0.0
    _center_lat = 0.0
    _center_lon = 0.0

    def __init__(self, track):
        self.navigation = SimpleNamespace(edit_track=track, hl_track_hover=None)
        self.move_callback = None
        self.cursor = None
        self.cleared = 0
        self.published = 0

    def viewport_rect(self):
        return QRectF(0, 0, 256, 256)

    def _cancel_cursor(self):
        pass

    def _cancel_inertia(self):
        pass

    def _finish_zoom_silent(self):
        pass

    def setCursor(self, cursor):
        self.cursor = cursor

    def setFocus(self):
        pass

    def update(self):
        pass

    def clear_move_preview(self):
        self.cleared += 1

    def publish_route_overlay(self):
        self.published += 1

    def convert_screen_to_geographic(self, x, y):
        return SimpleNamespace(get_degrees=lambda: (float(y), float(x)))


def test_edit_track_mark_hit_test_uses_edit_track_and_hit_radius():
    track = _OverlayTrack("Editing", [(0.0, 0.0), (0.0, 90.0)])
    canvas = _HitCanvas(track)

    # zoom 0 puts the equator at y=128 and 90 degrees east 64 px right.
    assert canvas.edit_track_mark_at(QPointF(128, 128)) == 0
    assert canvas.edit_track_mark_at(QPointF(194, 128)) == 1
    assert canvas.edit_track_mark_at(QPointF(160, 160)) is None

    track.visible = False
    assert canvas.edit_track_mark_at(QPointF(128, 128)) is None

    track.visible = True
    canvas.navigation.edit_track = None
    assert canvas.edit_track_mark_at(QPointF(128, 128)) is None


def test_set_track_mark_hover_only_publishes_on_change():
    canvas = SimpleNamespace(
        navigation=SimpleNamespace(hl_track_hover=None),
        published=0,
    )

    def publish():
        canvas.published += 1

    canvas.publish_route_overlay = publish
    canvas.apply_map_cursor = lambda: None
    canvas.set_track_mark_hover = (
        lambda index: MapInteractionMixin.set_track_mark_hover(canvas, index)
    )

    assert canvas.set_track_mark_hover(2) is True
    assert canvas.navigation.hl_track_hover == 2
    assert canvas.published == 1
    assert canvas.set_track_mark_hover(2) is False
    assert canvas.published == 1
    assert canvas.set_track_mark_hover(None) is True
    assert canvas.navigation.hl_track_hover is None
    assert canvas.published == 2


def test_entering_track_edit_refreshes_overlay_and_clears_hover():
    from sailworks.qt.window import PreviewWindow

    track = Track("Editable", [(1, 2, None)])
    calls = []
    canvas = SimpleNamespace(
        navigation=SimpleNamespace(edit_track=None, hl_track_hover=4),
        refresh_route_overlay=lambda: calls.append("refresh"),
        invalidate_navigation=lambda: calls.append("invalidate"),
        update=lambda: calls.append("update"),
    )
    host = SimpleNamespace(map=canvas)

    PreviewWindow._set_track_editing(host, track)

    assert canvas.navigation.edit_track is track
    assert canvas.navigation.hl_track_hover is None
    assert calls == ["refresh", "invalidate", "update"]

    PreviewWindow._set_track_editing(host, None)
    assert canvas.navigation.edit_track is None


def _mouse_event(
    kind, x, y, *, button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.NoButton
):
    return QMouseEvent(
        kind,
        QPointF(x, y),
        QPointF(x, y),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )


def _key_event(key):
    return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


class _Emitted:
    def __init__(self, on_emit=None):
        self.calls = []
        self.on_emit = on_emit

    def emit(self, *args):
        self.calls.append(args)
        if self.on_emit is not None:
            self.on_emit(*args)


class _MarkCanvas:
    """Canvas stand-in for the viewport mark capture path."""

    def __init__(self, marks, *, measure=False, take_move=True):
        self.marks = marks
        self._measure_mode = measure
        self.move_callback = None
        self.hover = []
        self.drags = []
        self.previews = []
        self.track_point_move_requested = _Emitted(
            (lambda *_: setattr(self, "move_callback", lambda coordinates: None))
            if take_move
            else None
        )

    def edit_track_mark_at(self, position):
        return self.marks.get((round(position.x()), round(position.y())))

    def set_track_mark_hover(self, index):
        self.hover.append(index)
        return True

    def _begin_move_drag(self, position):
        self.drags.append(position)
        return True

    def _move_preview(self, position):
        self.previews.append(position)
        return True


def _mark_host(canvas, *, moving=False):
    return SimpleNamespace(
        canvas=canvas,
        widget=SimpleNamespace(is_moving=lambda: moving),
        _track_press=None,
    )


def test_track_mark_press_and_click_do_not_move(qtbot):
    track = _OverlayTrack("Editing", [(0.0, 0.0)])
    canvas = _MarkCanvas({(20, 20): 0})
    host = _mark_host(canvas)

    press = _mouse_event(
        QEvent.Type.MouseButtonPress, 20, 20, buttons=Qt.MouseButton.LeftButton
    )
    assert _track_mark_event(host, press, track) is True
    assert host._track_press[0] is track
    assert host._track_press[1] == 0

    # A stationary release is a click: no drag, no move request.
    release = _mouse_event(QEvent.Type.MouseButtonRelease, 20, 20)
    assert _track_mark_event(host, release, track) is True
    assert host._track_press is None
    assert canvas.track_point_move_requested.calls == []
    assert canvas.drags == []
    assert canvas.previews == []


def test_track_mark_press_off_mark_reaches_panning(qtbot):
    canvas = _MarkCanvas({})
    host = _mark_host(canvas)
    press = _mouse_event(
        QEvent.Type.MouseButtonPress, 90, 90, buttons=Qt.MouseButton.LeftButton
    )
    assert (
        _track_mark_event(host, press, _OverlayTrack("Editing", [(0.0, 0.0)])) is False
    )
    assert host._track_press is None
    # Any hover from the previous pointer position is stale once the press
    # leaves the mark to the viewport's panning.
    assert canvas.hover == [None]


def test_track_mark_hover_clears_when_the_scene_pans(qtbot):
    canvas = _MarkCanvas({(20, 20): 0})
    host = _mark_host(canvas)

    hover = _mouse_event(QEvent.Type.MouseMove, 20, 20)
    assert _track_mark_event(host, hover, _OverlayTrack("E", [(0.0, 0.0)])) is False
    assert canvas.hover == [0]

    pan = _mouse_event(QEvent.Type.MouseMove, 40, 40, buttons=Qt.MouseButton.LeftButton)
    assert _track_mark_event(host, pan, _OverlayTrack("E", [(0.0, 0.0)])) is False
    assert canvas.hover[-1] is None

    moving = _mark_host(_MarkCanvas({(20, 20): 0}), moving=True)
    assert (
        _track_mark_event(
            moving, _mouse_event(QEvent.Type.MouseMove, 20, 20), _OverlayTrack("E", [])
        )
        is False
    )
    assert moving.canvas.hover[-1] is None


def test_track_mark_drag_starts_once_past_the_threshold(qtbot):
    track = _OverlayTrack("Editing", [(0.0, 0.0)])
    canvas = _MarkCanvas({(20, 20): 0})
    host = _mark_host(canvas)

    assert (
        _track_mark_event(
            host,
            _mouse_event(
                QEvent.Type.MouseButtonPress,
                20,
                20,
                buttons=Qt.MouseButton.LeftButton,
            ),
            track,
        )
        is True
    )
    # Below the drag distance the capture only holds the gesture.
    assert (
        _track_mark_event(
            host,
            _mouse_event(
                QEvent.Type.MouseMove,
                20 + QApplication.startDragDistance() - 1,
                20,
                buttons=Qt.MouseButton.LeftButton,
            ),
            track,
        )
        is True
    )
    assert canvas.track_point_move_requested.calls == []

    start = QPointF(20, 20)
    moved = _mouse_event(
        QEvent.Type.MouseMove,
        20 + QApplication.startDragDistance() + 1,
        20,
        buttons=Qt.MouseButton.LeftButton,
    )
    assert _track_mark_event(host, moved, track) is True
    assert canvas.track_point_move_requested.calls == [(track, 0)]
    assert canvas.drags == [start]
    assert canvas.previews == [moved.position()]
    assert host._track_press is None

    # Later moves belong to the move flow, not the capture path.
    again = _mouse_event(
        QEvent.Type.MouseMove, 60, 20, buttons=Qt.MouseButton.LeftButton
    )
    assert _track_mark_event(host, again, track) is False
    assert len(canvas.track_point_move_requested.calls) == 1


def test_track_mark_drag_without_a_move_receiver_is_swallowed(qtbot):
    track = _OverlayTrack("Editing", [(0.0, 0.0)])
    canvas = _MarkCanvas({(20, 20): 0}, take_move=False)
    host = _mark_host(canvas)
    _track_mark_event(
        host,
        _mouse_event(
            QEvent.Type.MouseButtonPress, 20, 20, buttons=Qt.MouseButton.LeftButton
        ),
        track,
    )
    dragged = _mouse_event(
        QEvent.Type.MouseMove,
        20 + QApplication.startDragDistance() + 4,
        20,
        buttons=Qt.MouseButton.LeftButton,
    )
    # The press was consumed, so there is no pan to fall back to.  The drag is
    # swallowed rather than leaving a captured gesture with no move callback.
    assert _track_mark_event(host, dragged, track) is True
    assert len(canvas.track_point_move_requested.calls) == 1
    assert host._track_press is None
    assert canvas.drags == []
    assert canvas.previews == []


def test_track_mark_capture_does_not_intercept_measurement(qtbot):
    track = _OverlayTrack("Editing", [(0.0, 0.0)])
    canvas = _MarkCanvas({(20, 20): 0}, measure=True)
    host = _mark_host(canvas)
    press = _mouse_event(
        QEvent.Type.MouseButtonPress, 20, 20, buttons=Qt.MouseButton.LeftButton
    )
    assert _track_mark_event(host, press, track) is False
    assert host._track_press is None
    assert canvas.hover == []


def test_track_mark_hit_test_follows_the_scene_pan_offset():
    class _PannedHitCanvas(_HitCanvas):
        def scene_projection(self):
            return (0.0, 0.0, 0.0, 30.0, 0.0)

    track = _OverlayTrack("Editing", [(0.0, 0.0), (0.0, 90.0)])
    canvas = _PannedHitCanvas(track)

    # 90 degrees east at zoom 0 sits 128 + 64 px along, plus the 30 px pan.
    assert canvas.edit_track_mark_at(QPointF(222, 128)) == 1
    assert canvas.edit_track_mark_at(QPointF(192, 128)) is None


def test_track_mark_drag_commits_once_through_the_move_flow(qtbot):
    track = _OverlayTrack("Editing", [(0.0, 0.0)])
    canvas = _HitCanvas(track)
    commits = []
    previews = []
    restores = []
    canvas.track_point_move_requested = _Emitted(
        lambda element, index: canvas.begin_move(
            lambda coordinates: commits.append(coordinates),
            preview=lambda coordinates: previews.append(coordinates),
            restore=lambda: restores.append(True),
        )
    )
    host = _mark_host(canvas)

    press = _mouse_event(
        QEvent.Type.MouseButtonPress, 128, 128, buttons=Qt.MouseButton.LeftButton
    )
    assert _track_mark_event(host, press, track) is True
    drag_distance = QApplication.startDragDistance()
    drag_x = 128 + drag_distance + 2
    drag = _mouse_event(
        QEvent.Type.MouseMove,
        drag_x,
        128,
        buttons=Qt.MouseButton.LeftButton,
    )
    assert _track_mark_event(host, drag, track) is True
    assert canvas.move_callback is not None
    # The preview is the pointer's (latitude, longitude), so y comes first.
    assert previews == [(128.0, float(drag_x))]

    assert canvas._finish_move_drag(QPointF(150, 120)) is True
    assert commits == [(120.0, 150.0)]
    assert restores == []


def test_escape_cancels_a_pending_move_and_edit_mode(qtbot):
    track = _OverlayTrack("Editing", [(0.0, 0.0)])
    canvas = _HitCanvas(track)
    cancelled = []
    canvas.track_edit_cancel_requested = _Emitted(lambda: cancelled.append(True))
    began = []
    canvas.begin_move(lambda coordinates: began.append(coordinates))

    event = _key_event(Qt.Key.Key_Escape)
    canvas.keyPressEvent(event)
    assert event.isAccepted()
    assert began == []
    assert canvas.move_callback is None
    assert canvas.cleared == 1

    canvas.navigation.edit_track = track
    canvas.keyPressEvent(_key_event(Qt.Key.Key_Escape))
    assert cancelled == [True]


class _EscCanvas(QWidget):
    """QWidget canvas stand-in for the presenter key filter path."""

    def __init__(self):
        super().__init__()
        self.navigation = SimpleNamespace(edit_track=None, hl_track_hover=0)
        self.move_callback = lambda coordinates: None
        self._move_press_position = QPointF(1, 1)
        self.hover = []

    def set_track_mark_hover(self, index):
        self.hover.append(index)
        self.navigation.hl_track_hover = index
        return True

    def keyPressEvent(self, event):  # noqa: N802 - Qt virtual method name
        if event.key() == Qt.Key.Key_Escape and self.move_callback is not None:
            self.move_callback = None
            self._move_press_position = None
            event.accept()


def test_escape_during_a_drag_clears_hover_and_still_cancels(qtbot):
    track = _OverlayTrack("Editing", [(0.0, 0.0)])
    canvas = _EscCanvas()
    host = SimpleNamespace(
        canvas=canvas,
        widget=canvas,
        _track_press=(track, 0, QPointF(20, 20)),
        _routing_press_position=None,
    )
    event = _key_event(Qt.Key.Key_Escape)

    assert WeatherPresenter.eventFilter(host, canvas, event) is True
    assert host._track_press is None
    assert canvas.hover == [None]
    # The filter still forwards Escape, so the move flow cancels as before.
    assert canvas.move_callback is None
    assert event.isAccepted()


class _SceneCollection(list):
    """Collection protocol the window-level delete command expects."""

    def __init__(self, elements=()):
        super().__init__(elements)
        self.elements = self
        self._active = None

    def get_active(self):
        return self._active

    def set_active(self, element):
        self._active = element

    def remove_many(self, elements):
        wanted = {id(element) for element in elements}
        self[:] = [element for element in self if id(element) not in wanted]
        self.save()

    def touch(self):
        pass

    def save(self):
        pass


def _scene_window(tracks=(), routes=(), pois=(), edit_track=None, log_path=None):
    """PreviewWindow stand-in driving the scene action handlers."""
    from sailworks.qt.window import PreviewWindow

    collections = {
        "track": _SceneCollection(tracks),
        "routing": _SceneCollection(routes),
        "poi": _SceneCollection(pois),
    }
    core = SimpleNamespace(
        trackManager=collections["track"],
        routingManager=collections["routing"],
        poiManager=collections["poi"],
    )
    elements = {
        item.uid: item for collection in collections.values() for item in collection
    }

    def live_entry(kind, key):
        element = elements.get(key)
        return SimpleNamespace(element=element) if element is not None else None

    canvas = SimpleNamespace(
        navigation=SimpleNamespace(edit_track=edit_track, hl_track_hover=3),
        invalidate_navigation=lambda: None,
        invalidate_race_geometry=lambda: None,
        refresh_route_overlay=lambda: None,
        update=lambda: None,
        setFocus=lambda: None,
        map_pointer_target=lambda: None,
    )
    loaded = []

    class _LogLibrary:
        def path(self, key):
            if log_path is None:
                raise KeyError(key)
            return log_path

    window = SimpleNamespace(
        map=canvas,
        log_map=SimpleNamespace(
            invalidate_navigation=lambda: None, update=lambda: None
        ),
        controller=SimpleNamespace(
            core=core,
            log_library=_LogLibrary(),
            load_paths=lambda paths: loaded.extend(paths),
            time_control=SimpleNamespace(time=None),
        ),
        scene_panel=SimpleNamespace(
            scene=SimpleNamespace(live_entry=live_entry),
            refresh=lambda: None,
        ),
        undo_stack=QUndoStack(),
        views=SimpleNamespace(setCurrentWidget=lambda widget: None),
        race_panel=SimpleNamespace(loaded_race_tracks=lambda: {}),
        _navigation_refresh=lambda: None,
        _select_saved_route=lambda route: None,
        _race_time_changed=lambda when: None,
    )
    # The scene handlers call these as bound window methods, so the stand-in
    # host must expose them.  Without the binding the fake host raises
    # AttributeError inside a Qt slot, which surfaces as an unrelated async
    # exception instead of the assertion under test.
    window._set_track_editing = lambda track: PreviewWindow._set_track_editing(
        window, track
    )
    window._exit_track_edit = lambda: PreviewWindow._exit_track_edit(window)
    window._focus_map_surface = lambda: PreviewWindow._focus_map_surface(window)
    window.loaded_paths = loaded
    return window


def test_deleting_the_edited_track_leaves_edit_mode():
    from sailworks.qt.window import PreviewWindow

    track = FakeTrack("t1", "Editable")
    window = _scene_window(tracks=[track], edit_track=track)

    PreviewWindow._scene_delete(window, [("item", KIND_TRACK, "t1")])

    assert track not in window.controller.core.trackManager
    assert window.map.navigation.edit_track is None
    assert window.map.navigation.hl_track_hover is None
    assert window.undo_stack.count() == 1

    window.undo_stack.undo()
    assert track in window.controller.core.trackManager
    # Restoring the track must not resurrect edit mode on the orphaned marks.
    assert window.map.navigation.edit_track is None


def test_deleting_another_track_keeps_edit_mode():
    from sailworks.qt.window import PreviewWindow

    edited = FakeTrack("t1", "Editable")
    other = FakeTrack("t2", "Other")
    window = _scene_window(tracks=[edited, other], edit_track=edited)

    PreviewWindow._scene_delete(window, [("item", KIND_TRACK, "t2")])

    assert other not in window.controller.core.trackManager
    assert window.map.navigation.edit_track is edited


def test_selecting_a_poi_leaves_track_edit_mode():
    from sailworks.qt.window import PreviewWindow

    track = FakeTrack("t1", "Editable")
    poi = FakePOI("p1", "Harbor")
    window = _scene_window(tracks=[track], pois=[poi], edit_track=track)

    PreviewWindow._scene_selected(window, KIND_POI, "p1")

    assert window.map.navigation.edit_track is None
    assert window.map.navigation.hl_track_hover is None


def test_selecting_a_route_leaves_track_edit_mode():
    from sailworks.qt.window import PreviewWindow

    track = FakeTrack("t1", "Editable")
    route = FakeRoute("r1", "Route")
    window = _scene_window(tracks=[track], routes=[route], edit_track=track)

    PreviewWindow._scene_selected(window, KIND_ROUTING, "r1")

    assert window.controller.core.routingManager.get_active() is route
    assert window.map.navigation.edit_track is None


def test_selecting_a_race_track_leaves_track_edit_mode():
    from sailworks.qt.window import PreviewWindow

    track = FakeTrack("t1", "Editable")
    race_track = SimpleNamespace(boat_id=7)
    window = _scene_window(tracks=[track], edit_track=track)
    window.race_panel = SimpleNamespace(
        loaded_race_tracks=lambda: {"r1": (race_track,)}
    )

    PreviewWindow._scene_selected(window, KIND_TRACK, "race:r1:7")

    assert window.race_panel.active_race_track is race_track
    assert window.map.navigation.edit_track is None


def test_opening_a_log_leaves_track_edit_mode(tmp_path):
    from sailworks.qt.window import PreviewWindow

    track = FakeTrack("t1", "Editable")
    window = _scene_window(
        tracks=[track], edit_track=track, log_path=tmp_path / "race.log"
    )

    PreviewWindow._scene_open(window, KIND_LOG, "log-1")

    assert window.map.navigation.edit_track is None
    assert window.loaded_paths == [tmp_path / "race.log"]


def test_edit_track_action_focuses_the_map(tmp_path):
    from sailworks.qt.window import PreviewWindow

    application()
    track = FakeTrack("t1", "Morning")
    panel = panel_for(tmp_path, tracks=[track])
    window = _scene_window(tracks=[track])
    window.scene_panel = panel
    focused = []
    quick_view = SimpleNamespace(setFocus=lambda: focused.append("quick"))
    window.map.map_pointer_target = lambda: quick_view
    window.map.setFocus = lambda: focused.append("map")
    shown = []
    window.views = SimpleNamespace(setCurrentWidget=shown.append)
    panel.edit_track_requested.connect(
        lambda key: PreviewWindow._scene_edit_track(window, key)
    )

    panel.edit_track_requested.emit("t1")

    assert window.map.navigation.edit_track is track
    assert shown == [window.map]
    assert focused == ["quick", "map"]


def test_escape_with_the_tree_focused_leaves_track_edit():
    from sailworks.qt.window import PreviewWindow

    application()
    track = FakeTrack("t1", "Morning")

    class _Window(QMainWindow):
        keyPressEvent = PreviewWindow.keyPressEvent
        _set_track_editing = PreviewWindow._set_track_editing
        _exit_track_edit = PreviewWindow._exit_track_edit

        def __init__(self, canvas):
            super().__init__()
            self.map = canvas

    canvas = _scene_window(tracks=[track], edit_track=track).map
    window = _Window(canvas)
    event = _key_event(Qt.Key.Key_Escape)

    window.keyPressEvent(event)

    assert canvas.navigation.edit_track is None
    assert canvas.navigation.hl_track_hover is None
    assert event.isAccepted()
