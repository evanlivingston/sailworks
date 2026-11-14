"""Focused panel stack interaction checks; run in an isolated Qt process."""

import importlib.util
import unittest
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sailworks.qt.collapsiblepanel import PANEL_ANIMATION_MS
from sailworks.qt.panelstack import GAP_ANIMATION_MS, PanelStack


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
