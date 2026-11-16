"""Drawer tabs track the overlay panel edges as their widths change."""

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import (  # noqa: E402
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sailworks.qt.paneloverlay import PanelOverlay  # noqa: E402
from sailworks.qt.window import PreviewWindow  # noqa: E402


class _FlagHost(QWidget):
    """Minimal host that borrows the window's drawer-flag logic."""

    _position_drawer_flags = PreviewWindow._position_drawer_flags
    _watch_drawer_flags = PreviewWindow._watch_drawer_flags
    _panel_width_dragged = PreviewWindow._panel_width_dragged

    def _panel_layout_changed(self):
        self.layout_changes += 1


def _make_host(qtbot, width=1100):
    host = _FlagHost()
    host.resize(width, 600)
    host.layout_changes = 0
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    overlay = PanelOverlay(QWidget(), QWidget(), QWidget())
    layout.addWidget(overlay)
    overlay.set_side_width("left", 300)
    overlay.set_side_width("right", 300)
    host.overlay = overlay
    host.views = overlay.views
    host._panel_overlay = overlay
    host._panel_stacks = {}
    host._panel_stack_width = {"left": 300, "right": 300}
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
    host._watch_drawer_flags(overlay)
    host._position_drawer_flags()
    qtbot.wait(10)
    return host


def _expected_x(host):
    origin = host.views.mapTo(host, host.views.rect().topLeft())
    return {
        "left": origin.x() + host.overlay.side_width("left"),
        "right": origin.x()
        + host.views.width()
        - 18
        - host.overlay.side_width("right"),
        "top": origin.y() + max(0, (host.views.height() - 64) // 2),
    }


def test_flags_sit_on_initial_panel_edges(qtbot):
    host = _make_host(qtbot)
    expected = _expected_x(host)
    assert host.drawer_flags["left"].x() == expected["left"]
    assert host.drawer_flags["right"].x() == expected["right"]
    assert host.drawer_flags["left"].y() == expected["top"]
    assert host.drawer_flags["right"].y() == expected["top"]


def test_dragging_the_handle_moves_flags_without_manual_call(qtbot):
    host = _make_host(qtbot)
    before = host.drawer_flags["left"].x()

    handle = host.overlay.handles["left"]
    handle.start_drag(0)
    handle.drag_to(120)
    handle.end_drag()
    qtbot.wait(10)

    expected = _expected_x(host)
    assert host.overlay.side_width("left") == 420
    assert host.drawer_flags["left"].x() == expected["left"]
    assert host.drawer_flags["left"].x() != before

    right = host.overlay.handles["right"]
    right.start_drag(0)
    right.drag_to(-90)
    right.end_drag()
    qtbot.wait(10)

    expected = _expected_x(host)
    assert host.overlay.side_width("right") == 390
    assert host.drawer_flags["right"].x() == expected["right"]
    for side in ("left", "right"):
        assert host.drawer_flags[side].y() == expected["top"]


def test_width_release_triggers_layout_change(qtbot):
    host = _make_host(qtbot)
    before = host.layout_changes

    handle = host.overlay.handles["left"]
    handle.start_drag(0)
    handle.drag_to(80)
    handle.end_drag()
    qtbot.wait(10)

    assert host.layout_changes > before
    assert host._panel_stack_width["left"] == 380


def test_collapsed_stack_uses_views_edge(qtbot):
    host = _make_host(qtbot)
    host._panel_stack_open["left"] = False
    host.overlay.set_side_open("left", False)
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


def test_position_accepts_stray_arguments(qtbot):
    host = _make_host(qtbot)
    host._position_drawer_flags(200, 1)
    qtbot.wait(10)
    assert host.drawer_flags["left"].isVisible()
