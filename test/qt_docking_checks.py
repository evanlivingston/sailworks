"""Focused panel interaction checks; run in an isolated Qt process."""

import importlib.util
import unittest
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QLabel,
    QMainWindow,
    QTabBar,
    QToolButton,
)

from sailworks.qt.collapsiblepanel import PANEL_ANIMATION_MS, CollapsiblePanel
from sailworks.qt.dockchrome import (
    DockCollapse,
    DockDragController,
    DockTitleBar,
    style_dock,
)
from sailworks.qt.panelstack import GAP_ANIMATION_MS, PanelStack


class DockingChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QMainWindow()
        self.window.setDockNestingEnabled(True)
        self.window.setCentralWidget(QLabel("Map"))
        self.window.resize(1100, 800)
        self.window.move(50, 50)
        self.a = self.make_dock("NMEA")
        self.b = self.make_dock("Routing")
        self.window.tabifyDockWidget(self.a, self.b)
        self.a.raise_()
        self.window.show()
        self.drain()
        self.manager = self.window.findChild(DockDragController)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.drain()

    def drain(self):
        for _ in range(5):
            self.app.processEvents()

    def make_dock(self, title):
        dock = QDockWidget(title, self.window)
        dock.setObjectName(title)
        style_dock(dock)
        dock.setWidget(CollapsiblePanel(title, QLabel(title + " content")))
        self.window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        return dock

    def float_b(self):
        self.b.setFloating(True)
        self.b.setGeometry(420, 250, 320, 240)
        self.drain()

    def test_no_duplicate_title_under_tabs(self):
        self.assertEqual(self.a.titleBarWidget().height(), 0)
        self.assertEqual(self.b.titleBarWidget().height(), 0)

    def test_lone_docked_panel_keeps_a_drag_handle(self):
        self.b.hide()
        self.drain()
        self.assertIsInstance(self.a.titleBarWidget(), DockTitleBar)
        self.b.show()
        self.drain()
        self.assertEqual(self.a.titleBarWidget().height(), 0)

    def test_collapse_arrow_in_lone_panel_header(self):
        self.b.hide()
        self.drain()
        collapse = self.a.findChild(DockCollapse)
        button = self.a.titleBarWidget().findChild(QToolButton, "dock-collapse")
        self.assertIs(button, collapse.title_button)
        panel = self.a.widget()
        self.assertIsInstance(panel, CollapsiblePanel)
        self.assertFalse(panel.header.isVisible())
        expanded_height = self.a.height()

        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.drain()
        self.assertTrue(collapse.collapsed)
        self.assertFalse(panel.body.isVisible())
        self.assertLess(self.a.height(), expanded_height)
        self.assertEqual(button.arrowType(), Qt.ArrowType.RightArrow)

        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.drain()
        self.assertFalse(collapse.collapsed)
        self.assertTrue(panel.body.isVisible())
        self.assertGreaterEqual(self.a.height(), expanded_height - 2)
        self.assertEqual(button.arrowType(), Qt.ArrowType.DownArrow)

    def test_collapse_arrow_in_tabbed_panel_header(self):
        collapse = self.b.findChild(DockCollapse)
        panel = self.b.widget()
        self.assertIsInstance(panel, CollapsiblePanel)
        self.assertTrue(panel.header.isVisible())
        self.assertIs(panel.collapse_button.parentWidget(), panel.header)

        QTest.mouseClick(panel.collapse_button, Qt.MouseButton.LeftButton)
        self.drain()
        self.assertTrue(collapse.collapsed)
        self.assertFalse(panel.body.isVisible())
        self.assertTrue(panel.header.isVisible())
        self.assertEqual(self.b.titleBarWidget().height(), 0)

        QTest.mouseClick(panel.collapse_button, Qt.MouseButton.LeftButton)
        self.drain()
        self.assertFalse(collapse.collapsed)
        self.assertTrue(panel.body.isVisible())

    def test_collapsing_a_tab_group_lifts_the_panel_below(self):
        window = QMainWindow()
        window.setDockNestingEnabled(True)
        window.setCentralWidget(QLabel("Map"))
        window.resize(1000, 800)

        def make(title):
            dock = QDockWidget(title, window)
            dock.setWidget(CollapsiblePanel(title, QLabel(title + " body")))
            style_dock(dock)
            return dock

        weather, nmea, routing = make("Weather"), make("NMEA"), make("Routing")
        window.addDockWidget(
            Qt.DockWidgetArea.RightDockWidgetArea,
            weather,
            Qt.Orientation.Vertical,
        )
        window.splitDockWidget(weather, routing, Qt.Orientation.Vertical)
        window.tabifyDockWidget(weather, nmea)
        weather.raise_()
        window.show()
        self.drain()
        window.resizeDocks([weather, routing], [500, 250], Qt.Orientation.Vertical)
        self.drain()

        weather_collapse = weather.findChild(DockCollapse)
        nmea_collapse = nmea.findChild(DockCollapse)
        routing_height = routing.height()

        weather_collapse.set_collapsed(True)
        self.drain()
        self.assertLess(weather.height(), 100)
        self.assertGreater(routing.height(), routing_height + 100)

        nmea.raise_()
        self.drain()
        self.assertFalse(nmea_collapse.collapsed)
        self.assertGreater(nmea.height(), 400)
        self.assertLess(abs(routing.height() - routing_height), 40)

        weather.raise_()
        self.drain()
        self.assertLess(weather.height(), 100)
        self.assertGreater(routing.height(), routing_height + 100)

        weather_collapse.set_collapsed(False)
        self.drain()
        self.assertGreater(weather.height(), 400)
        self.assertLess(abs(routing.height() - routing_height), 40)

        window.close()
        window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_sidebar_preview_builds_with_collapse_arrows(self):
        path = Path(__file__).parents[1] / "tools" / "preview_sidebar.py"
        spec = importlib.util.spec_from_file_location("preview_sidebar", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        gallery = module.build_gallery()
        gallery.show()
        self.drain()
        docks = gallery.findChildren(QDockWidget)
        self.assertTrue(docks)
        for dock in docks:
            collapse = dock.findChild(DockCollapse)
            self.assertIsNotNone(collapse, dock.windowTitle())
            panel = dock.widget()
            self.assertIsInstance(panel, CollapsiblePanel)
            self.assertIsNotNone(collapse.title_button.parentWidget())
        gallery.close()
        gallery.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_panel_stack_preview_auto_height_and_reorder(self):
        path = Path(__file__).parents[1] / "tools" / "preview_sidebar.py"
        spec = importlib.util.spec_from_file_location("preview_sidebar", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        gallery = module.build_stack_gallery()
        gallery.show()
        self.drain()
        stack = gallery.findChild(PanelStack)
        self.assertIsNotNone(stack)
        self.assertEqual(len(stack._panels), 5)
        first, second = stack._panels[0], stack._panels[1]
        self.assertTrue(first.header.isVisible())
        expanded_height = first.height()
        second_y = second.y()

        first.set_collapsed(True)
        self.assertTrue(first.body.isVisible())
        QTest.qWait(PANEL_ANIMATION_MS + 100)
        self.assertTrue(first.collapsed)
        self.assertFalse(first.body.isVisible())
        self.assertLess(first.height(), expanded_height)
        self.assertLess(second.y(), second_y)

        first.set_collapsed(False)
        QTest.qWait(PANEL_ANIMATION_MS + 100)
        self.assertFalse(first.collapsed)
        self.assertGreater(first.height(), expanded_height - 5)

        press = first.header.mapToGlobal(first.header.rect().center())
        target = stack.container.mapToGlobal(QPoint(10, stack.container.height() - 20))
        self.send_mouse(
            first.header,
            QEvent.Type.MouseButtonPress,
            press,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
        self.send_mouse(
            first.header,
            QEvent.Type.MouseMove,
            target,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
        QTest.qWait(GAP_ANIMATION_MS + 50)
        self.assertTrue(stack._dragging)
        self.assertIsNotNone(stack._placeholder)
        self.assertTrue(first.header.property("dragging"))
        self.assertGreater(first.geometry().y(), 0)

        self.send_mouse(
            first.header,
            QEvent.Type.MouseButtonRelease,
            target,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
        QTest.qWait(PANEL_ANIMATION_MS + 100)
        self.assertFalse(stack._dragging)
        self.assertIsNone(stack._placeholder)
        self.assertIs(stack._panels[-1], first)

        gallery.close()
        gallery.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_floating_is_opaque_frameless_tool_with_contained_content(self):
        content = self.b.widget()
        for _ in range(3):
            self.float_b()
            self.assertTrue(self.b.windowFlags() & Qt.WindowType.FramelessWindowHint)
            self.assertEqual(self.b.windowType(), Qt.WindowType.Tool)
            self.assertFalse(
                self.b.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            )
            self.assertTrue(self.b.mask().isEmpty())
            self.assertIsInstance(self.b.titleBarWidget(), DockTitleBar)
            self.assertTrue(self.b.rect().contains(content.geometry()))
            self.assertGreaterEqual(content.y(), self.b.titleBarWidget().height())
            self.b.setFloating(False)
            self.drain()
            self.assertIs(self.b.widget(), content)
            self.assertEqual(self.b.titleBarWidget().height(), 0)

    def test_tab_identity_survives_reorder_and_duplicate_titles(self):
        self.b.setWindowTitle(self.a.windowTitle())
        self.drain()
        tabs = self.window.findChild(QTabBar)
        original = [self.manager._tab_dock(tabs, i) for i in range(tabs.count())]
        self.assertEqual(set(original), {self.a, self.b})
        tabs.moveTab(0, 1)
        self.assertEqual(self.manager._tab_dock(tabs, 1), original[0])

    def send_mouse(self, widget, kind, point, button, buttons):
        event = QMouseEvent(
            kind,
            widget.mapFromGlobal(point),
            point,
            button,
            buttons,
            Qt.KeyboardModifier.NoModifier,
        )
        self.app.sendEvent(widget, event)

    def test_tab_tear_off_then_drag_back_to_sidebar(self):
        tabs = self.window.findChild(QTabBar)
        index = next(
            i for i in range(tabs.count()) if self.manager._tab_dock(tabs, i) is self.b
        )
        QTest.mousePress(
            tabs, Qt.MouseButton.LeftButton, pos=tabs.tabRect(index).center()
        )
        cursor = self.manager.global_rect(self.window.centralWidget()).center()
        self.send_mouse(
            tabs,
            QEvent.Type.MouseMove,
            cursor,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
        self.drain()
        self.assertTrue(self.b.isFloating())
        self.assertIsNotNone(self.manager._drag_bar)
        cursor = self.manager.global_rect(self.a).center()
        self.send_mouse(
            tabs,
            QEvent.Type.MouseMove,
            cursor,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
        bar = self.b.titleBarWidget()
        self.assertEqual(bar._pending_snap[0].kind, "tab")
        self.assertTrue(bar._preview.isVisible())
        self.send_mouse(
            tabs,
            QEvent.Type.MouseButtonRelease,
            cursor,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
        self.drain()
        self.assertFalse(self.b.isFloating())
        self.assertIn(self.b, self.window.tabifiedDockWidgets(self.a))
        self.assertFalse(bar._preview.isVisible())

    def test_in_strip_drag_keeps_native_reordering(self):
        tabs = self.window.findChild(QTabBar)
        QTest.mousePress(tabs, Qt.MouseButton.LeftButton, pos=tabs.tabRect(0).center())
        cursor = tabs.mapToGlobal(tabs.tabRect(1).center())
        self.send_mouse(
            tabs,
            QEvent.Type.MouseMove,
            cursor,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
        self.assertFalse(self.a.isFloating())
        self.assertFalse(self.b.isFloating())
        QTest.mouseRelease(
            tabs, Qt.MouseButton.LeftButton, pos=tabs.tabRect(1).center()
        )
        self.assertIsNone(self.manager._pressed)

    def test_split_targets_and_tab_target_have_matching_previews(self):
        self.float_b()
        rect = self.manager.global_rect(self.a)
        for kind, cursor in (
            ("top", QPoint(rect.center().x(), rect.top() + 10)),
            ("bottom", QPoint(rect.center().x(), rect.bottom() - 10)),
            ("tab", rect.center()),
        ):
            target = self.manager.target_at(self.b, cursor)
            self.assertEqual(target.kind, kind)
            self.assertEqual(target.rect.width(), rect.width())
            self.assertEqual(
                target.rect.height(),
                rect.height() if kind == "tab" else rect.height() // 2,
            )

    def split(self, kind):
        peer = self.make_dock("Weather")
        hidden = self.make_dock("Hidden")
        self.window.tabifyDockWidget(self.a, peer)
        self.window.tabifyDockWidget(self.a, hidden)
        hidden.hide()
        self.a.raise_()
        self.float_b()
        rect = self.manager.global_rect(self.a)
        cursor = QPoint(
            rect.center().x(), rect.top() + 10 if kind == "top" else rect.bottom() - 10
        )
        target = self.manager.target_at(self.b, cursor)
        self.assertEqual(target.kind, kind)
        self.manager.apply_target(self.b, target)
        self.drain()
        self.assertFalse(self.b.isFloating())
        self.assertNotIn(self.b, self.window.tabifiedDockWidgets(self.a))
        self.assertIn(peer, self.window.tabifiedDockWidgets(self.a))
        self.assertTrue(hidden.isHidden())
        self.assertEqual(self.b.x(), self.a.x())
        if kind == "top":
            self.assertLess(self.b.y(), self.a.y())
        else:
            self.assertGreater(self.b.y(), self.a.y())

    def test_drop_above_tabbed_sidebar(self):
        self.split("top")

    def test_drop_below_tabbed_sidebar(self):
        self.split("bottom")

    def test_floating_panels_snap_vertically_not_to_screen(self):
        self.a.setFloating(True)
        self.a.setGeometry(100, 320, 310, 200)
        self.float_b()
        for y, expected_y in ((320, 80), (519, 520)):
            target = self.manager.target_at(self.b, QPoint(200, y))
            self.assertEqual(target.kind, "float")
            self.manager.apply_target(self.b, target)
            self.assertEqual(self.b.y(), expected_y)
            self.assertEqual(self.b.width(), self.a.width())
        screen = self.app.primaryScreen().availableGeometry()
        for point in (screen.topLeft(), screen.topRight(), screen.center()):
            self.assertIsNone(self.manager.target_at(self.b, point))

    def test_empty_sidebar_target_and_disallowed_area(self):
        self.float_b()
        bounds = self.manager.global_rect(self.window)
        cursor = QPoint(
            bounds.left() + 5,
            self.manager.global_rect(self.window.centralWidget()).center().y(),
        )
        target = self.manager.target_at(self.b, cursor)
        self.assertEqual(target.kind, "area")
        self.assertEqual(target.area, Qt.DockWidgetArea.LeftDockWidgetArea)
        self.b.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        self.assertIsNone(self.manager.target_at(self.b, cursor))
        self.b.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.manager.apply_target(self.b, target)
        self.drain()
        self.assertEqual(
            self.window.dockWidgetArea(self.b), Qt.DockWidgetArea.LeftDockWidgetArea
        )

    def test_split_layout_roundtrips_through_qt_state(self):
        self.split("bottom")
        state = self.window.saveState(1)
        self.b.setFloating(True)
        self.window.restoreState(state, 1)
        self.drain()
        self.assertFalse(self.b.isFloating())
        self.assertGreater(self.b.y(), self.a.y())

    def test_floating_header_drag_redocks_and_clears_preview(self):
        self.float_b()
        bar = self.b.titleBarWidget()
        QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=QPoint(100, 15))
        cursor = self.manager.global_rect(self.a).center()
        self.send_mouse(
            bar,
            QEvent.Type.MouseMove,
            cursor,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
        self.assertEqual(bar._pending_snap[0].kind, "tab")
        self.send_mouse(
            bar,
            QEvent.Type.MouseButtonRelease,
            cursor,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
        self.drain()
        self.assertFalse(self.b.isFloating())
        self.assertFalse(bar._preview.isVisible())

    def test_floating_edge_resize_still_works(self):
        from sailworks.qt.dockchrome import _FloatingFrame

        self.float_b()
        frame = self.b.findChild(_FloatingFrame)
        QTest.mousePress(self.b, Qt.MouseButton.LeftButton, pos=QPoint(1, 100))
        self.assertIsNotNone(frame._resize)
        QTest.mouseRelease(self.b, Qt.MouseButton.LeftButton, pos=QPoint(1, 100))
        self.assertIsNone(frame._resize)


if __name__ == "__main__":
    unittest.main()
