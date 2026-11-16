"""Sidebar collapse/expand smoothness regressions for PanelStack."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize, Qt, QVariantAnimation
from PySide6.QtWidgets import (
    QLabel,
    QSplitter,
    QStackedWidget,
    QWidget,
)

from sailworks.qt.collapsiblepanel import CollapsiblePanel
from sailworks.qt.panelstack import PanelStack
from sailworks.qt.window import PreviewWindow


def _build_stack():
    stack = PanelStack()
    stack.add_panel(CollapsiblePanel("First", QLabel("first panel content")))
    stack.add_panel(CollapsiblePanel("Second", QLabel("second panel content")))
    return stack


def _build_splitter(qtbot, stack):
    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.setChildrenCollapsible(False)
    splitter.addWidget(stack)
    splitter.addWidget(QWidget())
    splitter.addWidget(QWidget())
    splitter.setSizes([300, 400, 200])
    splitter.resize(900, 600)
    qtbot.addWidget(splitter)
    splitter.show()
    qtbot.wait(0)
    return splitter


def test_panel_stack_minimum_size_hint_is_zero(qtbot):
    stack = _build_stack()
    qtbot.addWidget(stack)

    assert stack.minimumSizeHint() == QSize(0, 0)
    assert stack.minimumWidth() == 0


def test_closing_stack_animates_to_zero(qtbot):
    stack = _build_stack()
    splitter = _build_splitter(qtbot, stack)

    stack.setMinimumWidth(300)
    splitter.setSizes([300, 400, 200])
    qtbot.wait(0)
    assert splitter.sizes()[0] >= 300

    stack.setMinimumWidth(0)
    splitter.setSizes([0, 700, 200])
    qtbot.wait(0)

    # Regression: QScrollArea's 88px minimum hint used to clamp the closing
    # stack, then _finish_panel_stack_toggle snapped it to zero.
    assert splitter.sizes()[0] <= 1


def test_open_stack_still_respects_minimum_width(qtbot):
    stack = _build_stack()
    splitter = _build_splitter(qtbot, stack)

    stack.setMinimumWidth(300)
    splitter.setSizes([0, 700, 200])
    qtbot.wait(0)

    assert splitter.sizes()[0] >= 300


def test_showing_stack_keeps_closed_start_width(qtbot):
    stack = _build_stack()
    splitter = _build_splitter(qtbot, stack)

    stack.setMinimumWidth(300)
    splitter.setSizes([300, 400, 200])
    qtbot.wait(0)
    remembered = splitter.sizes()
    assert remembered[0] >= 300

    stack.hide()
    stack.setMinimumWidth(0)
    start = [0, 700, 200]
    splitter.setSizes(start)
    qtbot.wait(0)
    assert splitter.sizes()[0] == 0

    # Opening must not flash the remembered width before the first tick.
    stack.show()
    stack.setMinimumWidth(0)
    splitter.setSizes(start)
    qtbot.wait(0)

    assert splitter.sizes()[0] == 0


class _AnimationHost(QWidget):
    """Minimal host that borrows the window's freeze/thaw logic."""

    _freeze_panel_animation_view = PreviewWindow._freeze_panel_animation_view
    _thaw_panel_animation_view = PreviewWindow._thaw_panel_animation_view
    _finish_panel_stack_toggle = PreviewWindow._finish_panel_stack_toggle


def _make_animation_host(qtbot):
    host = _AnimationHost()
    host.views = QStackedWidget()
    host.map = QWidget(host)
    host.log_map = QWidget(host)
    host.other = QWidget(host)
    host.views.addWidget(host.map)
    host.views.addWidget(host.log_map)
    host.views.addWidget(host.other)
    host._panel_stacks = {}
    host._panel_stack_animation = None
    host._panel_animation_frozen_view = None
    host._position_drawer_calls = 0

    def _position(*_):
        host._position_drawer_calls += 1

    host._position_drawer_flags = _position
    qtbot.addWidget(host)
    return host


def test_freeze_pauses_only_the_active_map(qtbot):
    host = _make_animation_host(qtbot)

    host.views.setCurrentWidget(host.map)
    host._freeze_panel_animation_view()

    assert host._panel_animation_frozen_view is host.map
    assert not host.map.updatesEnabled()
    assert host.log_map.updatesEnabled()

    # Idempotent: a second freeze must not adopt a newly selected view.
    host.views.setCurrentWidget(host.log_map)
    host._freeze_panel_animation_view()
    assert host._panel_animation_frozen_view is host.map
    assert host.log_map.updatesEnabled()


def test_log_map_is_frozen_when_active(qtbot):
    host = _make_animation_host(qtbot)

    host.views.setCurrentWidget(host.log_map)
    host._freeze_panel_animation_view()

    assert host._panel_animation_frozen_view is host.log_map
    assert not host.log_map.updatesEnabled()
    assert host.map.updatesEnabled()


def test_non_map_tab_is_not_frozen(qtbot):
    host = _make_animation_host(qtbot)

    host.views.setCurrentWidget(host.other)
    host._freeze_panel_animation_view()

    assert host._panel_animation_frozen_view is None
    assert host.other.updatesEnabled()


def test_thaw_reenables_and_clears(qtbot):
    host = _make_animation_host(qtbot)

    host.views.setCurrentWidget(host.map)
    host._freeze_panel_animation_view()
    host._thaw_panel_animation_view()

    assert host._panel_animation_frozen_view is None
    assert host.map.updatesEnabled()

    # Idempotent: a second thaw with nothing frozen is a no-op.
    host._thaw_panel_animation_view()
    assert host._panel_animation_frozen_view is None
    assert host.map.updatesEnabled()


def test_finish_toggle_thaws_when_stack_is_missing(qtbot):
    host = _make_animation_host(qtbot)
    host.views.setCurrentWidget(host.map)
    host._freeze_panel_animation_view()
    animation = QVariantAnimation(host)
    host._panel_stack_animation = animation

    host._finish_panel_stack_toggle("left", False, animation)

    assert host._panel_animation_frozen_view is None
    assert host.map.updatesEnabled()
    assert host._panel_stack_animation is None
