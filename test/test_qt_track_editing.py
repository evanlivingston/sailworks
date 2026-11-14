"""Focused regression coverage for selected-track editing behavior."""

from types import SimpleNamespace

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from sailworks.core.geo.track import Track
from sailworks.qt.mapinteraction import MapInteractionMixin
from sailworks.qt.navigationpanel import MovePointCommand, NavigationPanel
from sailworks.qt.weatherpresenter import WeatherPresenter, _track_mark_event


class _Tracks(list):
    def __init__(self, *tracks):
        super().__init__(track for track in tracks if track is not None)
        self.active = self[0] if self else None
        for track in self:
            track.collection = self

    def save(self):
        pass

    def new_element(self, points):
        track = Track("New", points, collection=self)
        self.append(track)
        return track

    def get_active(self):
        return self.active

    def set_active(self, track):
        self.active = track


class _Panel:
    """Minimal panel state for direct behavioral tests of NavigationPanel."""

    def __init__(self, track):
        self.core = SimpleNamespace(trackManager=_Tracks(track))
        self.canvas = SimpleNamespace(
            navigation=SimpleNamespace(edit_track=None, hl_track_item=None),
            invalidate_race_geometry=lambda: None,
            update=lambda: None,
        )
        self.track_buttons = [QPushButton(), QPushButton()]
        self.point_index = 0
        self.track_points = QTableWidget()
        self.tracks = QTableWidget()
        self.changed = SimpleNamespace(emit=lambda: None)

    def refresh(self):
        pass

    def refresh_points(self):
        pass

    _set_track_editing = NavigationPanel._set_track_editing
    _invalidate_race_geometry = NavigationPanel._invalidate_race_geometry
    select_track = NavigationPanel.select_track
    new_track = NavigationPanel.new_track


def test_track_select_edit_and_escape(qtbot):
    app = QApplication.instance()
    assert app is not None
    ordinary = Track("Editable", [(1, 2, None)])
    panel = _Panel(ordinary)

    NavigationPanel.edit_track(panel)
    assert panel.canvas.navigation.edit_track is ordinary
    assert panel.track_buttons[1].isEnabled()

    event = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
    )
    assert NavigationPanel.eventFilter(panel, panel.tracks, event) is True
    assert event.isAccepted()
    assert panel.canvas.navigation.edit_track is None

    NavigationPanel.select_track(panel)
    assert panel.canvas.navigation.edit_track is None
    assert not panel.track_buttons[1].isEnabled()


def test_point_modifications_require_selected_editable_track(qtbot):
    track = Track("Track", [(1, 2, None), (3, 4, None)])
    panel = _Panel(track)
    panel.move_requested = SimpleNamespace(emit=lambda callback: None)
    before = list(track.points)

    NavigationPanel.edit_point(panel, "remove")
    assert track.points == before

    panel.canvas.navigation.edit_track = track
    NavigationPanel.edit_point(panel, "remove")
    assert len(track.points) == 1


def test_edit_point_name_updates_active_track(qtbot):
    track = Track("Track", [(1, 2, None)])
    panel = _Panel(track)
    panel.syncing = False
    panel.track_points.setColumnCount(4)
    panel.track_points.setRowCount(1)
    item = QTableWidgetItem("Start")
    panel.track_points.setItem(0, 1, item)

    NavigationPanel._edit_point_name(panel, item)

    assert track.point_name(0) == "Start"
    assert track[0] == (1, 2, None)


def test_move_point_command_undoes_and_redoes_relocation(qtbot):
    track = Track("Track", [(1, 2, None)])
    _Tracks(track)
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


def test_add_point_requires_edit_mode_for_existing_tracks(qtbot):
    track = Track("Track", [(1, 2, None)])
    panel = _Panel(track)
    panel.status = SimpleNamespace(setText=lambda text: None)
    panel.tabs = SimpleNamespace(setCurrentIndex=lambda index: None)
    panel.canvas._weather_presenter = SimpleNamespace(
        invalidations=0,
        invalidate_race_geometry=lambda: setattr(
            panel.canvas._weather_presenter,
            "invalidations",
            panel.canvas._weather_presenter.invalidations + 1,
        ),
    )
    panel.canvas.invalidations = 0
    panel.canvas.invalidate_race_geometry = lambda: setattr(
        panel.canvas, "invalidations", panel.canvas.invalidations + 1
    )

    NavigationPanel.add_point(panel, (5, 6))
    assert len(track.points) == 1

    panel.canvas.navigation.edit_track = track
    NavigationPanel.add_point(panel, (5, 6))
    assert len(track.points) == 2
    assert panel.canvas.invalidations == 1
    assert panel.canvas._weather_presenter.invalidations == 1


def _new_track_panel():
    panel = _Panel(None)
    manager = panel.core.trackManager
    panel.status = SimpleNamespace(setText=lambda text: None)
    panel.tabs = SimpleNamespace(setCurrentIndex=lambda index: None)
    panel.canvas._weather_presenter = SimpleNamespace(
        invalidate_race_geometry=lambda: None
    )
    panel.canvas.invalidate_race_geometry = lambda: None
    panel._set_track_editing = lambda enabled: setattr(
        panel.canvas.navigation, "edit_track", manager.get_active() if enabled else None
    )
    panel._invalidate_race_geometry = lambda: None
    return panel


def test_new_track_enters_edit_mode_and_accepts_repeated_points(qtbot):
    panel = _new_track_panel()

    track = NavigationPanel.new_track(panel)
    assert panel.canvas.navigation.edit_track is track
    NavigationPanel.add_point(panel, (1, 2))
    NavigationPanel.add_point(panel, (3, 4))
    assert track.points == [(1, 2, None), (3, 4, None)]


def test_add_point_implicitly_creates_editable_track(qtbot):
    panel = _new_track_panel()

    NavigationPanel.add_point(panel, (1, 2))
    track = panel.core.trackManager.get_active()
    assert track is not None
    assert panel.canvas.navigation.edit_track is track
    NavigationPanel.add_point(panel, (3, 4))
    assert track.points == [(1, 2, None), (3, 4, None)]


def test_context_click_preserves_multiselection_and_selects_unselected_row(qtbot):
    from PySide6.QtWidgets import QTableWidgetItem

    table = QTableWidget(2, 1)
    qtbot.addWidget(table)
    table.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setItem(0, 0, QTableWidgetItem("one"))
    table.setItem(1, 0, QTableWidgetItem("two"))
    table.selectRow(0)
    table.selectRow(1)
    NavigationPanel._select_context_item(table, table.item(0, 0))
    assert {item.row() for item in table.selectedItems()} == {0, 1}
    NavigationPanel._select_context_item(table, table.item(1, 0))
    assert {item.row() for item in table.selectedItems()} == {0, 1}
    table.clearSelection()
    NavigationPanel._select_context_item(table, table.item(1, 0))
    assert {item.row() for item in table.selectedItems()} == {1}


def test_track_list_click_and_double_click_are_wired(qtbot):
    table = QTableWidget(2, 1)
    qtbot.addWidget(table)
    ordinary = Track("Editable", [(1, 2, None)])
    race = Track("Race", [(3, 4, None)])
    manager = _Tracks(ordinary, race)
    buttons = [QPushButton(), QPushButton()]
    canvas = SimpleNamespace(
        navigation=SimpleNamespace(edit_track=None, hl_track_item=None),
        invalidate_race_geometry=lambda: None,
        update=lambda: None,
    )
    panel = SimpleNamespace(
        tracks=table,
        core=SimpleNamespace(trackManager=manager),
        canvas=canvas,
        track_buttons=buttons,
        syncing=False,
        point_index=-1,
        refresh_points=lambda: None,
        _invalidate_race_geometry=lambda: None,
        _set_track_editing=lambda enabled: NavigationPanel._set_track_editing(
            panel, enabled
        ),
        _select_track=lambda row: NavigationPanel._select_track(panel, row),
        edit_track=lambda: NavigationPanel.edit_track(panel),
    )
    for row, track in enumerate(manager):
        table.setItem(row, 0, QTableWidgetItem(track.name))
    NavigationPanel._wire_track_selection(panel)

    table.show()
    qtbot.waitExposed(table)
    ordinary_cell = table.visualItemRect(table.item(0, 0)).center()
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=ordinary_cell)
    QApplication.processEvents()
    assert manager.get_active() is ordinary
    assert canvas.navigation.edit_track is None
    assert not buttons[1].isEnabled()

    QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=ordinary_cell)
    QApplication.processEvents()
    assert canvas.navigation.edit_track is ordinary
    assert buttons[1].isEnabled()

    race_cell = table.visualItemRect(table.item(1, 0)).center()
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=race_cell)
    QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=race_cell)
    QApplication.processEvents()
    assert manager.get_active() is race
    assert canvas.navigation.edit_track is race
    assert buttons[1].isEnabled()


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
    track = Track("Editable", [(1, 2, None)])
    panel = _Panel(track)
    calls = []
    panel.canvas.refresh_route_overlay = lambda: calls.append("refresh")
    panel.canvas.invalidate_navigation = lambda: calls.append("invalidate")
    panel.canvas.navigation.hl_track_hover = 4

    NavigationPanel._set_track_editing(panel, True)

    assert panel.canvas.navigation.edit_track is track
    assert panel.canvas.navigation.hl_track_hover is None
    assert calls == ["refresh", "invalidate"]

    NavigationPanel._set_track_editing(panel, False)
    assert panel.canvas.navigation.edit_track is None


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
