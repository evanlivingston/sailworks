"""Focused panel stack interaction checks; run in an isolated Qt process."""

import importlib.util
import unittest
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from sailworks.qt.collapsiblepanel import PANEL_ANIMATION_MS, CollapsiblePanel
from sailworks.qt.panelstack import GAP_ANIMATION_MS, PanelSplitHandle, PanelStack


class DockingChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def drain(self):
        for _ in range(5):
            self.app.processEvents()

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

    def build_gallery(self):
        path = Path(__file__).parents[1] / "tools" / "preview_sidebar.py"
        spec = importlib.util.spec_from_file_location("preview_sidebar", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        gallery = module.build_gallery()
        gallery.show()
        self.drain()
        self.addCleanup(self._close, gallery)
        return gallery

    def _close(self, gallery):
        gallery.close()
        gallery.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_sidebar_preview_builds_with_collapse_arrows(self):
        gallery = self.build_gallery()
        stack = gallery.findChild(PanelStack)
        self.assertIsNotNone(stack)
        self.assertEqual(len(stack.panels()), 4)
        for panel in stack.panels():
            self.assertTrue(panel.header.isVisible())
            self.assertIsNotNone(panel.collapse_button.parentWidget())
            self.assertIsNotNone(panel.body)

    def test_panel_stack_preview_auto_height_and_reorder(self):
        gallery = self.build_gallery()
        stack = gallery.findChild(PanelStack)
        self.assertIsNotNone(stack)
        first, second = stack.panels()[0], stack.panels()[1]
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
        self.assertIs(stack.panels()[-1], first)

        stack.remove_panel(first)
        self.assertFalse(first.isVisible())
        stack.add_panel(first)
        self.drain()
        self.assertTrue(first.isVisible())

    def test_panel_split_handles_resize_and_restore(self):
        gallery = self.build_gallery()
        stack = gallery.findChild(PanelStack)
        self.assertIsNotNone(stack)
        panels = stack.panels()
        layout = stack.stack
        widgets = [layout.itemAt(index).widget() for index in range(layout.count())]
        widgets = [widget for widget in widgets if widget is not None]
        handles = [widget for widget in widgets if isinstance(widget, PanelSplitHandle)]
        # One divider per adjacent pair, plus the trailing grip that lets the
        # last panel grow into the stretch below it.
        self.assertEqual(len(handles), len(panels))
        for lower in panels[1:]:
            self.assertIsInstance(widgets[widgets.index(lower) - 1], PanelSplitHandle)
        self.assertIsInstance(widgets[widgets.index(panels[-1]) + 1], PanelSplitHandle)

        upper, lower = panels[0], panels[1]
        handle = widgets[widgets.index(lower) - 1]
        # The lower panel starts at its content minimum, so give it room to
        # shrink before trading height with the panel above.
        lower.set_user_height(lower.height() + 80)
        self.drain()
        start_upper, start_lower = upper.height(), lower.height()
        press = handle.mapToGlobal(handle.rect().center())
        moved = press + QPoint(0, 40)
        self.send_mouse(
            handle,
            QEvent.Type.MouseButtonPress,
            press,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
        self.send_mouse(
            handle,
            QEvent.Type.MouseMove,
            moved,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
        self.send_mouse(
            handle,
            QEvent.Type.MouseButtonRelease,
            moved,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
        self.drain()
        self.assertGreater(upper.height(), start_upper)
        self.assertLess(lower.height(), start_lower)
        self.assertAlmostEqual(
            upper.height() + lower.height(), start_upper + start_lower, delta=4
        )
        self.assertIsNotNone(upper.user_height())
        self.assertIsNotNone(lower.user_height())

        self.send_mouse(
            handle,
            QEvent.Type.MouseButtonDblClick,
            press,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
        self.drain()
        self.assertIsNone(upper.user_height())
        self.assertIsNone(lower.user_height())
        self.assertAlmostEqual(upper.height(), start_upper, delta=2)

        chosen = start_upper + 30
        upper.set_user_height(chosen)
        self.drain()
        upper.set_collapsed(True)
        QTest.qWait(PANEL_ANIMATION_MS + 100)
        self.assertEqual(upper.user_height(), chosen)
        self.assertLess(upper.height(), chosen)
        upper.set_collapsed(False)
        QTest.qWait(PANEL_ANIMATION_MS + 100)
        self.assertEqual(upper.user_height(), chosen)
        self.assertGreaterEqual(upper.height(), chosen - 2)

    def test_trailing_grip_grows_last_panel_only(self):
        stack = PanelStack()
        stack.resize(320, 420)
        stack.show()
        self.addCleanup(self._close, stack)
        for title in ("One", "Two", "Three"):
            body = QWidget()
            body.setMinimumHeight(60)
            stack.add_panel(CollapsiblePanel(title, body))
        self.drain()

        panels = stack.panels()
        self.assertEqual(len(panels), 3)
        self.assertEqual(len(stack._handles), len(panels))
        grip = stack._handles[-1]
        self.assertIsInstance(grip, PanelSplitHandle)

        others = [panel.height() for panel in panels[:-1]]
        last = panels[-1]
        start_last = last.height()
        press = grip.mapToGlobal(grip.rect().center())
        moved = press + QPoint(0, 40)
        self.send_mouse(
            grip,
            QEvent.Type.MouseButtonPress,
            press,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
        self.send_mouse(
            grip,
            QEvent.Type.MouseMove,
            moved,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
        self.send_mouse(
            grip,
            QEvent.Type.MouseButtonRelease,
            moved,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
        self.drain()
        self.assertGreaterEqual(last.height(), start_last + 30)
        self.assertEqual([panel.height() for panel in panels[:-1]], others)
        self.assertEqual(last.user_height(), last.height())

        self.send_mouse(
            grip,
            QEvent.Type.MouseButtonDblClick,
            press,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
        self.drain()
        self.assertIsNone(last.user_height())

    def test_content_minimum_height_clamps_user_height(self):
        body = QWidget()
        body.setMinimumHeight(120)
        panel = CollapsiblePanel("Fixed", body)
        panel.show()
        self.addCleanup(self._close, panel)
        self.drain()

        minimum = panel.minimum_panel_height()
        self.assertGreaterEqual(minimum, panel._header_height() + 120)
        panel.set_user_height(10)
        self.drain()
        self.assertGreaterEqual(panel.minimumHeight(), minimum)
        self.assertGreaterEqual(panel.height(), minimum)
