"""Sidebar collapse/expand smoothness regressions for PanelStack and the overlay."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (  # noqa: E402
    QLabel,
    QVBoxLayout,
    QWidget,
)

from sailworks.qt.collapsiblepanel import PANEL_ANIMATION_MS, CollapsiblePanel
from sailworks.qt.paneloverlay import PanelOverlay
from sailworks.qt.panelstack import PanelStack
from sailworks.qt.window import PreviewWindow


def _build_stack():
    stack = PanelStack()
    stack.add_panel(CollapsiblePanel("First", QLabel("first panel content")))
    stack.add_panel(CollapsiblePanel("Second", QLabel("second panel content")))
    return stack


def test_panel_stack_minimum_size_hint_is_zero(qtbot):
    stack = _build_stack()
    qtbot.addWidget(stack)

    assert stack.minimumSizeHint() == QSize(0, 0)
    assert stack.minimumWidth() == 0


class _ToggleHost(QWidget):
    """Minimal host that borrows the window's overlay toggle logic."""

    _toggle_panel_stack = PreviewWindow._toggle_panel_stack
    _animate_panel_stack = PreviewWindow._animate_panel_stack
    _finish_panel_stack_toggle = PreviewWindow._finish_panel_stack_toggle

    def _position_drawer_flags(self, *_):
        self.position_calls += 1


def _make_toggle_host(qtbot, width=1200):
    host = _ToggleHost()
    host.resize(width, 600)
    host.position_calls = 0
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    left = _build_stack()
    right = _build_stack()
    views = QWidget()
    overlay = PanelOverlay(views, left, right)
    overlay.set_side_width("left", 420)
    overlay.set_side_width("right", 380)
    layout.addWidget(overlay)
    host._panel_overlay = overlay
    host.views = views
    host._panel_stacks = {"left": left, "right": right}
    host._panel_stack_width = {"left": 420, "right": 380}
    host._panel_stack_open = {"left": True, "right": True}
    host._panel_stack_animation = None
    qtbot.addWidget(host)
    host.show()
    qtbot.wait(10)
    return host


def test_closing_stack_animates_to_zero_and_hides(qtbot):
    host = _make_toggle_host(qtbot)
    stack = host._panel_overlay.stack("left")
    assert stack.width() == 420

    host._toggle_panel_stack("left")
    qtbot.wait(PANEL_ANIMATION_MS + 100)

    assert not host._panel_stack_open["left"]
    assert stack.width() == 0
    assert stack.isHidden()


def test_toggling_a_stack_never_resizes_the_views(qtbot):
    host = _make_toggle_host(qtbot)
    views_geometry = host.views.geometry()

    host._toggle_panel_stack("left")
    qtbot.wait(PANEL_ANIMATION_MS + 100)
    assert host.views.geometry() == views_geometry

    host._toggle_panel_stack("left")
    qtbot.wait(PANEL_ANIMATION_MS + 100)

    assert host._panel_stack_open["left"]
    assert host._panel_overlay.stack("left").width() == 420
    assert host.views.geometry() == views_geometry


def test_animation_frames_only_change_the_side_width(qtbot):
    host = _make_toggle_host(qtbot)
    views_geometry = host.views.geometry()

    host._animate_panel_stack("left", 0, 420, 0.5)

    width = host._panel_overlay.side_width("left")
    assert 0 < width < 420
    assert host.views.geometry() == views_geometry
    assert host.position_calls >= 1


def test_closing_keeps_the_saved_width_for_reopening(qtbot):
    host = _make_toggle_host(qtbot)

    host._toggle_panel_stack("right")
    qtbot.wait(PANEL_ANIMATION_MS + 100)
    assert host._panel_stack_width["right"] == 380

    host._toggle_panel_stack("right")
    qtbot.wait(PANEL_ANIMATION_MS + 100)
    assert host._panel_overlay.side_width("right") == 380
    assert host._panel_overlay.stack("right").width() == 380
