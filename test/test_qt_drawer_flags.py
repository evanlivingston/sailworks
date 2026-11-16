"""Drawer tabs track the panel edges while the splitter is dragged."""

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sailworks.qt.window import PreviewWindow  # noqa: E402


class _FlagHost(QWidget):
    """Minimal host that borrows the window's drawer-flag logic."""

    _position_drawer_flags = PreviewWindow._position_drawer_flags
    _watch_drawer_flags = PreviewWindow._watch_drawer_flags


def _make_host(qtbot, width=1100):
    host = _FlagHost()
    host.resize(width, 600)
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.setChildrenCollapsible(False)
    left_stack = QWidget()
    views = QWidget()
    right_stack = QWidget()
    splitter.addWidget(left_stack)
    splitter.addWidget(views)
    splitter.addWidget(right_stack)
    splitter.setStretchFactor(0, 0)
    splitter.setStretchFactor(1, 1)
    splitter.setStretchFactor(2, 0)
    layout.addWidget(splitter)
    host.splitter = splitter
    host.views = views
    host.left_stack = left_stack
    host.right_stack = right_stack
    host._panel_stacks = {"left": left_stack, "right": right_stack}
    host._panel_stack_open = {"left": True, "right": True}
    host.map = None
    host.log_map = None
    host.drawer_flags = {}
    for side in ("left", "right"):
        flag = QToolButton(host)
        flag.setFixedSize(18, 64)
        host.drawer_flags[side] = flag
    qtbot.addWidget(host)
    host.show()
    qtbot.wait(20)
    splitter.setSizes([300, 500, 300])
    host._watch_drawer_flags(splitter)
    host._position_drawer_flags()
    qtbot.wait(10)
    return host


def _expected_x(host):
    origin = host.views.mapTo(host, host.views.rect().topLeft())
    return {
        "left": host.left_stack.mapTo(host, host.left_stack.rect().topLeft()).x()
        + host.left_stack.width(),
        "right": host.right_stack.mapTo(host, host.right_stack.rect().topLeft()).x()
        - 18,
        "top": origin.y() + max(0, (host.views.height() - 64) // 2),
    }


def test_flags_sit_on_initial_panel_edges(qtbot):
    host = _make_host(qtbot)
    expected = _expected_x(host)
    assert host.drawer_flags["left"].x() == expected["left"]
    assert host.drawer_flags["right"].x() == expected["right"]
    assert host.drawer_flags["left"].y() == expected["top"]
    assert host.drawer_flags["right"].y() == expected["top"]


def test_dragging_splitter_moves_flags_without_manual_call(qtbot):
    host = _make_host(qtbot)
    before = host.drawer_flags["left"].x()

    host.splitter.moveSplitter(420, 1)
    qtbot.wait(10)

    expected = _expected_x(host)
    assert host.drawer_flags["left"].x() == expected["left"]
    assert host.drawer_flags["left"].x() != before

    host.splitter.moveSplitter(host.splitter.width() - 260, 2)
    qtbot.wait(10)

    expected = _expected_x(host)
    assert host.drawer_flags["right"].x() == expected["right"]
    for side in ("left", "right"):
        assert host.drawer_flags[side].y() == expected["top"]


def test_collapsed_stack_uses_views_edge(qtbot):
    host = _make_host(qtbot)
    host._panel_stack_open["left"] = False
    host.left_stack.hide()
    host._position_drawer_flags()

    origin = host.views.mapTo(host, host.views.rect().topLeft())
    assert host.drawer_flags["left"].x() == origin.x()
    assert host.drawer_flags["left"].text() == "›"


def test_narrow_host_clamps_flags_inside_window(qtbot):
    host = _make_host(qtbot)
    host.resize(120, 600)
    qtbot.wait(10)
    host._position_drawer_flags()

    for side in ("left", "right"):
        flag = host.drawer_flags[side]
        assert 0 <= flag.x() <= host.width() - 18


def test_splitter_moved_arguments_are_accepted(qtbot):
    host = _make_host(qtbot)
    host._position_drawer_flags(200, 1)
    host.splitter.splitterMoved.emit(200, 1)
    qtbot.wait(10)
    assert host.drawer_flags["left"].isVisible()
