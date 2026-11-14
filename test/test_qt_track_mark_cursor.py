"""Pointer cursor cues over the editing track's marks.

The desktop viewport is a QQuickView (a QWindow) laid out inside the map
widget, so these tests assert which surface receives the cursor: the presenter
window when one is hosted, and the container widget otherwise.
"""

from types import SimpleNamespace

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QWidget

from sailworks.qt.mapinteraction import MapInteractionMixin
from sailworks.qt.weatherpresenter import _track_mark_event


class _Track(list):
    def __init__(self, points, visible=True):
        super().__init__(points)
        self.visible = visible


class _CursorWindow:
    """Stand-in for the QQuickView window that receives the mouse events."""

    def __init__(self):
        self.calls = []
        self._cursor = None

    def setCursor(self, cursor):  # noqa: N802 - Qt method name
        self.calls.append(cursor)
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def shape(self):
        return None if self._cursor is None else self._cursor.shape()


class _Emitted:
    def __init__(self, on_emit=None):
        self.calls = []
        self.on_emit = on_emit

    def emit(self, *args):
        self.calls.append(args)
        if self.on_emit is not None:
            self.on_emit(*args)


class _CursorCanvas(MapInteractionMixin):
    _center_lat = 0.0
    _center_lon = 0.0
    _zoom = 0.0

    def __init__(self, track=None, window=None, container=None):
        self.navigation = SimpleNamespace(
            edit_track=track if track is not None else _Track([(0.0, 0.0)]),
            hl_track_hover=None,
        )
        self.move_callback = None
        self._measure_mode = False
        self._measure_points = ()
        self.widget_calls = []
        self.commits = []
        self.published = 0
        self.hosted_window = window
        self.container = container
        if window is not None:
            self._weather_presenter = SimpleNamespace(widget=window)
        self.track_point_move_requested = _Emitted(
            lambda *_: self.begin_move(
                self.commits.append, preview=lambda _: None, direct_point=True
            )
        )
        self.track_edit_cancel_requested = _Emitted(
            lambda: setattr(self.navigation, "edit_track", None)
        )

    def setCursor(self, cursor):  # noqa: N802 - Qt method name
        self.widget_calls.append(cursor)
        if self.container is not None:
            self.container.setCursor(cursor)

    def setFocus(self):  # noqa: N802 - Qt method name
        pass

    def update(self):
        pass

    def viewport_rect(self):
        return QRectF(0, 0, 256, 256)

    def _cancel_cursor(self):
        pass

    def _cancel_inertia(self):
        pass

    def _finish_zoom_silent(self):
        pass

    def clear_move_preview(self):
        pass

    def publish_route_overlay(self):
        self.published += 1

    def convert_screen_to_geographic(self, x, y):
        return SimpleNamespace(get_degrees=lambda: (float(y), float(x)))


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


def _host(canvas, *, moving=False):
    return SimpleNamespace(
        canvas=canvas,
        widget=SimpleNamespace(is_moving=lambda: moving),
        _track_press=None,
    )


def test_hover_cursor_targets_the_presenter_window(qtbot):
    window = _CursorWindow()
    canvas = _CursorCanvas(window=window)

    canvas.set_track_mark_hover(0)

    assert window.shape() == Qt.CursorShape.PointingHandCursor
    # The container widget must not be used for the QuickView's pointer.
    assert canvas.widget_calls == []


def test_blank_and_leave_restore_the_open_hand(qtbot):
    window = _CursorWindow()
    canvas = _CursorCanvas(window=window)

    canvas.set_track_mark_hover(0)
    canvas.set_track_mark_hover(None)

    assert window.shape() == Qt.CursorShape.OpenHandCursor


def test_hover_drag_and_release_cursor_sequence(qtbot):
    window = _CursorWindow()
    canvas = _CursorCanvas(window=window)
    host = _host(canvas)
    track = canvas.navigation.edit_track

    press = _mouse_event(
        QEvent.Type.MouseButtonPress, 128, 128, buttons=Qt.MouseButton.LeftButton
    )
    assert _track_mark_event(host, press, track) is True
    assert window.shape() == Qt.CursorShape.PointingHandCursor

    move = _mouse_event(
        QEvent.Type.MouseMove,
        160,
        140,
        button=Qt.MouseButton.NoButton,
        buttons=Qt.MouseButton.LeftButton,
    )
    assert _track_mark_event(host, move, track) is True
    assert canvas.move_callback is not None
    assert window.shape() == Qt.CursorShape.ClosedHandCursor

    # Release commits the drag; the mark is still under the pointer, and the
    # presenter restores its actionable hover.
    assert canvas._finish_move_drag(QPointF(160, 140)) is True
    canvas.set_track_mark_hover(canvas.edit_track_mark_at(QPointF(128, 128)))

    assert canvas.commits, "the drag should commit the move"
    assert window.shape() == Qt.CursorShape.PointingHandCursor


def test_off_mark_press_keeps_the_pan_cursor(qtbot):
    window = _CursorWindow()
    canvas = _CursorCanvas(window=window)
    host = _host(canvas)

    press = _mouse_event(
        QEvent.Type.MouseButtonPress, 10, 10, buttons=Qt.MouseButton.LeftButton
    )

    assert _track_mark_event(host, press, canvas.navigation.edit_track) is False
    assert host._track_press is None
    assert window.shape() == Qt.CursorShape.OpenHandCursor


def test_dragging_pan_does_not_show_the_mark_cue(qtbot):
    window = _CursorWindow()
    canvas = _CursorCanvas(window=window)
    host = _host(canvas)

    canvas.set_track_mark_hover(0)
    move = _mouse_event(
        QEvent.Type.MouseMove,
        40,
        40,
        button=Qt.MouseButton.NoButton,
        buttons=Qt.MouseButton.LeftButton,
    )

    assert _track_mark_event(host, move, canvas.navigation.edit_track) is False
    assert window.shape() == Qt.CursorShape.OpenHandCursor


def test_measure_mode_keeps_the_crosshair_and_is_not_downgraded_by_hover(qtbot):
    window = _CursorWindow()
    canvas = _CursorCanvas(window=window)

    MapInteractionMixin.keyPressEvent(canvas, _key_event(Qt.Key.Key_I))
    assert window.shape() == Qt.CursorShape.CrossCursor

    canvas.set_track_mark_hover(0)
    assert window.shape() == Qt.CursorShape.CrossCursor

    MapInteractionMixin.keyPressEvent(canvas, _key_event(Qt.Key.Key_Escape))
    assert canvas._measure_mode is False
    assert window.shape() == Qt.CursorShape.OpenHandCursor


def test_explicit_move_crosshair_is_not_downgraded_by_hover(qtbot):
    window = _CursorWindow()
    canvas = _CursorCanvas(window=window)

    canvas.begin_move(lambda coordinates: None)
    assert window.shape() == Qt.CursorShape.CrossCursor

    canvas.set_track_mark_hover(0)
    assert window.shape() == Qt.CursorShape.CrossCursor

    # The presenter clears the hover when a drag starts; leaving the move then
    # restores the pan cursor.
    canvas.set_track_mark_hover(None)
    canvas.cancel_move()
    assert window.shape() == Qt.CursorShape.OpenHandCursor


def test_escape_during_mark_drag_restores_the_base_cursor(qtbot):
    window = _CursorWindow()
    canvas = _CursorCanvas(window=window)

    canvas.set_track_mark_hover(0)
    canvas.begin_move(lambda coordinates: None, direct_point=True)
    assert window.shape() == Qt.CursorShape.ClosedHandCursor

    # The presenter clears the stale hover before the canvas cancels the move.
    canvas.set_track_mark_hover(None)
    canvas.cancel_move()

    assert window.shape() == Qt.CursorShape.OpenHandCursor


def test_edit_mode_exit_clears_a_stale_mark_hover(qtbot):
    window = _CursorWindow()
    canvas = _CursorCanvas(window=window)

    canvas.set_track_mark_hover(0)
    assert window.shape() == Qt.CursorShape.PointingHandCursor

    canvas.navigation.edit_track = None
    canvas.apply_map_cursor()

    assert window.shape() == Qt.CursorShape.OpenHandCursor


def test_escape_leaving_edit_mode_restores_the_pan_cursor(qtbot):
    window = _CursorWindow()
    canvas = _CursorCanvas(window=window)

    canvas.set_track_mark_hover(0)
    assert window.shape() == Qt.CursorShape.PointingHandCursor

    MapInteractionMixin.keyPressEvent(canvas, _key_event(Qt.Key.Key_Escape))

    assert canvas.navigation.edit_track is None
    assert window.shape() == Qt.CursorShape.OpenHandCursor


def test_repeated_hover_moves_do_not_reissue_set_cursor(qtbot):
    window = _CursorWindow()
    canvas = _CursorCanvas(window=window)

    canvas.set_track_mark_hover(0)
    assert len(window.calls) == 1

    for _ in range(5):
        assert canvas.set_track_mark_hover(0) is False

    assert len(window.calls) == 1


def test_cursor_falls_back_to_the_container_widget(qtbot):
    container = QWidget()
    qtbot.addWidget(container)
    canvas = _CursorCanvas(container=container)

    canvas.set_track_mark_hover(0)

    assert container.cursor().shape() == Qt.CursorShape.PointingHandCursor
