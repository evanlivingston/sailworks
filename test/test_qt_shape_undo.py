# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

For detail about GNU see <http://www.gnu.org/licenses/>.
"""

"""Undoable shape vertex edits and the keys that leave edge edit mode."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent, QUndoStack
from PySide6.QtWidgets import QApplication

from sailworks.qt.mapinteraction import MapInteractionMixin
from sailworks.qt.window import PreviewWindow
from test.test_qt_map_shapes import _DrawingHost, _editing_host


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _HistoryHost(_DrawingHost, MapInteractionMixin):
    """The real mixins with pointer and key handling, minus a Qt surface.

    ``_DrawingHost`` comes first so its Qt-free cursor and paint doubles win;
    the key handler still resolves to ``MapInteractionMixin.keyPressEvent``.
    """


@pytest.fixture
def history_host(app):
    host = _editing_host()
    # ``_editing_host`` returns the drawing host and its collection/shape.
    drawing, collection, shape = host
    surface = _HistoryHost(drawing.controller)
    surface.__dict__.update(drawing.__dict__)
    stack = QUndoStack()
    surface.set_undo_stack(stack)
    return surface, collection, shape, stack


def _vertex_ring(shape):
    return tuple(tuple(vertex) for vertex in shape.vertices)


def _press_key(host, key):
    event = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
    host.keyPressEvent(event)
    return event


# -- add and move through the history -----------------------------------


def test_add_vertex_is_one_command_and_undo_restores_the_ring(history_host):
    host, collection, shape, stack = history_host
    original = _vertex_ring(shape)

    assert host._record_shape_edit_click(491.0, 300.0)

    assert stack.count() == 1
    assert stack.undoText() == "Add shape vertex"
    inserted = _vertex_ring(shape)
    assert len(inserted) == 5
    assert len(original) == 4
    assert tuple(inserted[1]) == pytest.approx((0.0, 0.5), abs=1e-3)
    # The untouched corners keep their exact order around the insert.
    assert (inserted[0], *inserted[2:]) == original
    assert collection.saves == 1

    stack.undo()

    assert _vertex_ring(shape) == original
    assert collection.saves == 2

    stack.redo()

    assert _vertex_ring(shape) == inserted
    assert collection.saves == 3


def test_move_vertex_is_one_command_and_undo_restores_the_ring(history_host):
    host, collection, shape, stack = history_host
    original = _vertex_ring(shape)

    assert host._shape_edit_press(400.0, 300.0)
    assert host._shape_edit_move(350.0, 330.0)
    # The live drag is still a preview, not a command.
    assert stack.count() == 0
    assert collection.saves == 0
    assert _vertex_ring(shape) == original

    assert host._shape_edit_release(350.0, 330.0)

    assert stack.count() == 1
    assert stack.undoText() == "Move shape vertex"
    moved = _vertex_ring(shape)
    assert moved != original
    assert moved[1:] == original[1:]
    assert collection.saves == 1

    stack.undo()

    assert _vertex_ring(shape) == original
    assert collection.saves == 2

    stack.redo()

    assert _vertex_ring(shape) == moved
    assert collection.saves == 3


def test_history_refresh_republishes_and_announces_each_state(history_host):
    host, _collection, _shape, stack = history_host
    published = host.publish_count

    assert host._record_shape_edit_click(491.0, 300.0)
    assert host.shapes_changed.emissions == 1
    assert host.publish_count > published

    stack.undo()
    assert host.shapes_changed.emissions == 2

    stack.redo()
    assert host.shapes_changed.emissions == 3


def test_noop_drop_pushes_nothing(history_host):
    host, collection, shape, stack = history_host
    original = _vertex_ring(shape)

    assert host._shape_edit_press(400.0, 300.0)
    assert host._shape_edit_release(400.0, 300.0)

    assert stack.count() == 0
    assert collection.saves == 0
    assert _vertex_ring(shape) == original


def test_invalid_drop_is_rejected_without_a_command(history_host):
    host, collection, shape, stack = history_host
    original = _vertex_ring(shape)

    assert host._shape_edit_press(400.0, 300.0)
    assert host._shape_edit_move(491.0, 100.0)
    assert host._shape_edit_release(491.0, 100.0)

    assert stack.count() == 0
    assert collection.saves == 0
    assert _vertex_ring(shape) == original
    error = host.map_shapes_overlay_payload()["edit"]["error"]
    assert "cross" in error or "touch" in error


def test_save_failure_rolls_back_and_leaves_no_undoable_edit(history_host):
    host, collection, shape, stack = history_host
    original = _vertex_ring(shape)
    collection.fail_save = True

    assert host._record_shape_edit_click(491.0, 300.0)

    assert _vertex_ring(shape) == original
    assert collection.saves == 0
    assert stack.canUndo() is False
    assert "could not save" in host.map_shapes_overlay_payload()["edit"]["error"]
    assert host.shapes_changed.emissions == 0


# -- leaving edit mode --------------------------------------------------


def test_finish_shape_edit_commits_the_drag_then_leaves(history_host):
    host, collection, shape, stack = history_host

    assert host._shape_edit_press(400.0, 300.0)
    assert host._shape_edit_move(350.0, 330.0)
    assert host.is_shape_vertex_dragging()

    assert host.finish_shape_edit() is True

    assert host.is_shape_editing() is False
    assert host.is_shape_vertex_dragging() is False
    assert stack.count() == 1
    assert collection.saves == 1


def test_finish_shape_edit_without_a_drag_only_leaves(history_host):
    host, collection, shape, stack = history_host
    original = _vertex_ring(shape)

    assert host.finish_shape_edit() is True

    assert host.is_shape_editing() is False
    assert stack.count() == 0
    assert collection.saves == 0
    assert _vertex_ring(shape) == original


def test_escape_key_leaves_edit_mode_without_a_command(history_host):
    host, collection, shape, stack = history_host
    host._update_shape_edit_hover(491.0, 300.0)

    event = _press_key(host, Qt.Key.Key_Escape)

    assert event.isAccepted()
    assert host.is_shape_editing() is False
    assert stack.count() == 0
    assert collection.saves == 0


def test_return_key_commits_the_drag_then_leaves_edit_mode(history_host):
    host, collection, shape, stack = history_host
    assert host._shape_edit_press(400.0, 300.0)
    assert host._shape_edit_move(350.0, 330.0)

    event = _press_key(host, Qt.Key.Key_Return)

    assert event.isAccepted()
    assert host.is_shape_editing() is False
    assert stack.count() == 1
    assert collection.saves == 1


def test_return_key_without_a_drag_only_leaves_edit_mode(history_host):
    host, collection, shape, stack = history_host

    event = _press_key(host, Qt.Key.Key_Enter)

    assert event.isAccepted()
    assert host.is_shape_editing() is False
    assert stack.count() == 0
    assert collection.saves == 0


# -- window fallback when the Scene tree owns focus ---------------------


class _KeyWindow:
    """Only the window surface the key handler consults."""

    def __init__(self, canvas):
        self.canvas = canvas

    def _active_map(self):
        return self.canvas


def test_window_escape_ends_shape_edit_when_the_tree_has_focus(history_host):
    host, collection, shape, stack = history_host
    window = _KeyWindow(host)
    event = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
    )

    PreviewWindow.keyPressEvent(window, event)

    assert host.is_shape_editing() is False
    assert stack.count() == 0
    assert collection.saves == 0


def test_window_enter_commits_the_drag_when_the_tree_has_focus(history_host):
    host, collection, shape, stack = history_host
    window = _KeyWindow(host)
    assert host._shape_edit_press(400.0, 300.0)
    assert host._shape_edit_move(350.0, 330.0)
    event = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier
    )

    PreviewWindow.keyPressEvent(window, event)

    assert host.is_shape_editing() is False
    assert stack.count() == 1
    assert collection.saves == 1
