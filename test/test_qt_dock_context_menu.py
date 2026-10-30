from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QDockWidget, QLabel, QMainWindow, QTabBar

import gweatherrouting.qt.dockchrome as dockchrome
from gweatherrouting.qt.dockchrome import DockDragController, style_dock


def test_close_panel_context_action_hides_dock(qtbot):
    window = QMainWindow()
    dock = QDockWidget("Routing", window)
    style_dock(dock)
    dock.setWidget(QLabel("Routing panel"))
    window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(dock.isVisible)

    menu = dockchrome._dock_context_menu(dock)
    assert menu.actions()[0].text() == "Close panel"
    menu.actions()[0].trigger()

    assert dock.isHidden()


def test_right_click_tab_title_opens_menu_for_that_panel(qtbot, monkeypatch):
    window = QMainWindow()
    a, b = QDockWidget("NMEA", window), QDockWidget("Routing", window)
    for dock in (a, b):
        style_dock(dock)
        dock.setWidget(QLabel(dock.windowTitle()))
        window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    window.tabifyDockWidget(a, b)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.findChild(QTabBar) is not None)
    tabs = window.findChild(QTabBar)
    manager = window.findChild(DockDragController)
    index = next(i for i in range(tabs.count()) if manager._tab_dock(tabs, i) is b)
    position = tabs.tabRect(index).center()
    global_position = tabs.mapToGlobal(position)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position, global_position)
    shown = []
    monkeypatch.setattr(
        dockchrome,
        "_show_dock_context_menu",
        lambda dock, _position: shown.append(dock),
    )

    handled = manager.eventFilter(tabs, event)

    assert handled is True
    assert shown == [b]
