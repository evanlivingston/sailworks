"""Focused regression coverage for selected-track editing behavior.

Track editing lives in the scene tree since the navigation panel was removed;
these tests drive the ScenePanel entry points and the shared undo commands.
"""

from types import SimpleNamespace

from PySide6.QtGui import QUndoStack

from sailworks.core.geo.track import Track
from sailworks.core.scene import KIND_TRACK
from sailworks.qt.navigationcommands import MovePointCommand
from test.test_qt_scenepanel import FakeTrack, application, panel_for


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
