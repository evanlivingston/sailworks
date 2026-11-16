"""Sidebar collapse/expand smoothness regressions for PanelStack."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QLabel, QSplitter, QWidget

from sailworks.qt.collapsiblepanel import CollapsiblePanel
from sailworks.qt.panelstack import PanelStack


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
