"""Focused panel interaction checks; run in an isolated Qt process."""

import unittest

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDockWidget, QLabel, QMainWindow, QTabBar

from gweatherrouting.qt.dockchrome import DockDragController, DockTitleBar, style_dock


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
        dock.setWidget(QLabel(title + " content"))
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
        from gweatherrouting.qt.dockchrome import _FloatingFrame

        self.float_b()
        frame = self.b.findChild(_FloatingFrame)
        QTest.mousePress(self.b, Qt.MouseButton.LeftButton, pos=QPoint(1, 100))
        self.assertIsNotNone(frame._resize)
        QTest.mouseRelease(self.b, Qt.MouseButton.LeftButton, pos=QPoint(1, 100))
        self.assertIsNone(frame._resize)


if __name__ == "__main__":
    unittest.main()
