"""Permanent overlay sidebars floating above the central view area.

``PanelOverlay`` owns the central views widget and the two panel stacks. The
views always fill the whole widget, and the stacks are positioned on top of
them, so toggling a sidebar never re-lays out or resizes the map.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget

PANEL_MIN_WIDTH = 300
PANEL_MAX_WIDTH = 560
PANEL_MAP_MIN_WIDTH = 300
RESIZE_HANDLE_WIDTH = 5


class PanelResizeHandle(QWidget):
    """Vertical grip on a panel stack's inner edge that resizes the stack."""

    resized = Signal(int)
    released = Signal()

    def __init__(self, side, parent=None):
        super().__init__(parent)
        self._side = side
        self._dragging = False
        self._start_x = 0.0
        self._start_width = 0
        self.setObjectName("panel-resize-handle-" + side)
        self.setCursor(Qt.CursorShape.SplitHCursor)
        self.setFixedWidth(RESIZE_HANDLE_WIDTH)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def side(self):
        return self._side

    def start_drag(self, global_x):
        overlay = self.parentWidget()
        if overlay is None:
            return
        self._dragging = True
        self._start_x = float(global_x)
        self._start_width = overlay.side_width(self._side)

    def drag_to(self, global_x):
        overlay = self.parentWidget()
        if not self._dragging or overlay is None:
            return
        delta = float(global_x) - self._start_x
        if self._side == "right":
            delta = -delta
        width = max(0, round(self._start_width + delta))
        applied = overlay.set_side_width(self._side, width)
        self.resized.emit(applied)

    def end_drag(self):
        if not self._dragging:
            return
        self._dragging = False
        self.released.emit()

    def mousePressEvent(self, event):  # noqa: N802 - Qt virtual method name
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self.start_drag(event.globalPosition().x())
        event.accept()

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt virtual method name
        if self._dragging:
            self.drag_to(event.globalPosition().x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt virtual method name
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self.end_drag()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class PanelOverlay(QWidget):
    """Central widget whose side stacks float above a full-width views area."""

    widths_changed = Signal()

    def __init__(self, views, left_stack, right_stack, parent=None):
        super().__init__(parent)
        self.setObjectName("panel-overlay")
        self.views = views
        views.setParent(self)
        self._stacks = {"left": left_stack, "right": right_stack}
        for stack in self._stacks.values():
            stack.setParent(self)
            stack.setMinimumWidth(0)
            stack.setMaximumWidth(PANEL_MAX_WIDTH)
        self._widths = {"left": PANEL_MIN_WIDTH, "right": PANEL_MIN_WIDTH}
        self._open = {"left": True, "right": True}
        self.handles = {}
        for side in ("left", "right"):
            handle = PanelResizeHandle(side, self)
            handle.resized.connect(
                lambda width, side=side: self.set_side_width(side, width)
            )
            handle.released.connect(self._commit_widths)
            self.handles[side] = handle
        self._layout_children()

    # -- geometry ---------------------------------------------------------
    def _layout_children(self):
        width = self.width()
        height = self.height()
        self.views.setGeometry(0, 0, width, height)
        for side, stack in self._stacks.items():
            opened = self._open.get(side, True)
            extent = min(max(0, int(self._widths.get(side, 0))), width) if opened else 0
            x = 0 if side == "left" else width - extent
            stack.setGeometry(x, 0, extent, height)
            stack.setVisible(opened and extent > 0)
            handle = self.handles.get(side)
            if handle is None:
                continue
            if opened and extent > 0:
                handle_x = (
                    extent - RESIZE_HANDLE_WIDTH if side == "left" else width - extent
                )
                handle.setGeometry(max(0, handle_x), 0, RESIZE_HANDLE_WIDTH, height)
                handle.show()
                handle.raise_()
            else:
                handle.hide()
        for stack in self._stacks.values():
            if stack.isVisible():
                stack.raise_()

    def resizeEvent(self, event):  # noqa: N802 - Qt virtual method name
        super().resizeEvent(event)
        self._layout_children()

    def showEvent(self, event):  # noqa: N802 - Qt virtual method name
        super().showEvent(event)
        self._layout_children()

    # -- width state ------------------------------------------------------
    def stack(self, side):
        return self._stacks.get(side)

    def side_width(self, side):
        return self._widths.get(side, 0)

    def is_open(self, side):
        return bool(self._open.get(side, False))

    def clamp_side_width(self, side, width):
        try:
            width = int(width)
        except (TypeError, ValueError):
            width = PANEL_MIN_WIDTH
        upper = PANEL_MAX_WIDTH
        if self.isVisible() and self.width() > 0:
            upper = min(upper, max(PANEL_MIN_WIDTH, self.width() - PANEL_MAP_MIN_WIDTH))
        return max(PANEL_MIN_WIDTH, min(upper, width))

    def set_side_width(self, side, width, clamp=True):
        if side not in self._stacks:
            return 0
        value = self.clamp_side_width(side, width) if clamp else max(0, int(width))
        self._widths[side] = value
        self._layout_children()
        return value

    def set_side_open(self, side, open, animate=False):
        if side not in self._stacks:
            return
        self._open[side] = bool(open)
        if open and self.width() > 0:
            extent = min(self._widths.get(side, 0), self.width())
            self._widths[side] = max(0, extent)
        self._layout_children()

    def _commit_widths(self):
        self._layout_children()
        self.widths_changed.emit()
