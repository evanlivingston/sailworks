"""A Blender-style vertical stack of auto-height collapsible panels.

Panels follow their content height instead of sharing a splitter, so collapsing
one pulls the next one up. The left and right stacks hand panels to each other
when dragged across the viewport.

Dragging follows Blender's panel interaction: the grabbed panel floats under
the cursor, the other panels open a gap where it will land, and releasing
animates the panel back into that gap.
"""

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .collapsiblepanel import PANEL_ANIMATION_MS, CollapsiblePanel, blender_panel_ease
from .scrollchrome import style_scrollarea

GAP_ANIMATION_MS = 120


def _lerp_rect(start: QRect, end: QRect, progress: float) -> QRect:
    return QRect(
        round(start.x() + (end.x() - start.x()) * progress),
        round(start.y() + (end.y() - start.y()) * progress),
        round(start.width() + (end.width() - start.width()) * progress),
        round(start.height() + (end.height() - start.height()) * progress),
    )


class PanelStack(QWidget):
    """Scrollable column of collapsible panels with Blender-style dragging."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel-stack")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.scroll = QScrollArea(self)
        style_scrollarea(self.scroll)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.container = QWidget()
        self.container.setObjectName("panel-stack-container")
        self.stack = QVBoxLayout(self.container)
        self.stack.setContentsMargins(0, 0, 0, 0)
        self.stack.setSpacing(1)
        self.stack.addStretch(1)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll)

        self._panels: list[CollapsiblePanel] = []
        self._drag_panel: CollapsiblePanel | None = None
        self._drag_start = QPoint()
        self._drag_offset = 0
        self._drag_height = 0
        self._drag_index = -1
        self._dragging = False
        self._drop_index = -1
        self._placeholder: QFrame | None = None
        self._gap_animation: QVariantAnimation | None = None
        self._settle_animation: QVariantAnimation | None = None
        self._shadow: QGraphicsDropShadowEffect | None = None
        self.peer: PanelStack | None = None
        self.scroll.verticalScrollBar().rangeChanged.connect(
            lambda *_: QTimer.singleShot(0, self._sync_container_width)
        )

    def set_peer(self, peer: "PanelStack"):
        self.peer = peer

    def resizeEvent(self, event):  # noqa: N802 - Qt virtual method name
        super().resizeEvent(event)
        self._sync_container_width()

    def _sync_container_width(self):
        width = self.scroll.viewport().width()
        if width > 0 and self.container.width() != width:
            self.container.setFixedWidth(width)

    def add_panel(self, panel: CollapsiblePanel) -> CollapsiblePanel:
        if panel in self._panels:
            return panel
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        panel.set_header_visible(True)
        panel.set_closable(True)
        panel.header.setCursor(Qt.CursorShape.OpenHandCursor)
        panel.header.installEventFilter(self)
        panel.title.installEventFilter(self)
        if not getattr(panel, "_stack_wired", False):
            panel.collapse_button.clicked.connect(
                lambda _checked=False, panel=panel: panel.set_collapsed(
                    not panel.collapsed
                )
            )
            panel.close_button.clicked.connect(
                lambda _checked=False, panel=panel: self.remove_panel(panel)
            )
            panel._stack_wired = True
        self.stack.insertWidget(len(self._panels), panel)
        self._panels.append(panel)
        QTimer.singleShot(0, self._sync_container_width)
        self.changed.emit()
        return panel

    def panels(self):
        return tuple(self._panels)

    def remove_panel(self, panel: CollapsiblePanel):
        if panel not in self._panels or self._drag_panel is panel:
            return
        self.stack.removeWidget(panel)
        self._panels.remove(panel)
        panel.hide()
        # Keep the panel and its body alive for the owning application; it is
        # simply no longer part of the visible stack.
        panel.setParent(self)
        panel.hide()
        QTimer.singleShot(0, self._sync_container_width)
        self.changed.emit()

    def eventFilter(self, watched, event):  # noqa: N802 - Qt virtual method name
        dragging = self._drag_panel
        if dragging is not None and (
            watched is dragging.header or watched is dragging.title
        ):
            panel = dragging
        else:
            panel = next(
                (
                    candidate
                    for candidate in self._panels
                    if watched is candidate.header or watched is candidate.title
                ),
                None,
            )
        if panel is None:
            return super().eventFilter(watched, event)

        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == (
            Qt.MouseButton.LeftButton
        ):
            self._drag_panel = panel
            self._drag_start = event.globalPosition().toPoint()
            self._drag_offset = max(
                0,
                self.container.mapFromGlobal(self._drag_start).y()
                - panel.geometry().top(),
            )
            self._dragging = False
            panel.header.grabMouse()
            return True

        if kind == QEvent.Type.MouseMove and self._drag_panel is panel:
            cursor = event.globalPosition().toPoint()
            if not self._dragging:
                if (
                    cursor - self._drag_start
                ).manhattanLength() < QApplication.startDragDistance():
                    return True
                self._begin_drag(panel)
            self._move_drag(cursor)
            return True

        if kind == QEvent.Type.MouseButtonRelease and self._drag_panel is panel:
            if self._dragging:
                self._end_drag()
            else:
                panel.set_collapsed(not panel.collapsed)
                self._release_mouse(panel)
            return True

        return super().eventFilter(watched, event)

    # -- drag ------------------------------------------------------------
    def _begin_drag(self, panel: CollapsiblePanel):
        self._drag_panel = panel
        self._dragging = True
        self._drag_height = panel.height()
        self._drag_index = self._panels.index(panel)
        self._drop_index = self._drag_index
        self._set_dragging(panel, True)
        panel.header.setCursor(Qt.CursorShape.ClosedHandCursor)

        self.stack.removeWidget(panel)
        self._panels.remove(panel)
        panel.setParent(self.container)
        panel.show()

        shadow = QGraphicsDropShadowEffect(panel)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 150))
        panel.setGraphicsEffect(shadow)
        self._shadow = shadow

        placeholder = QFrame(self.container)
        placeholder.setObjectName("panel-drop-gap")
        placeholder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        placeholder.setFixedHeight(self._drag_height)
        self.stack.insertWidget(self._drag_index, placeholder)
        self._placeholder = placeholder

    def _move_drag(self, cursor: QPoint):
        panel = self._drag_panel
        if panel is None:
            return
        peer = self.peer
        if (
            peer is not None
            and peer is not self
            and peer.isVisible()
            and peer.rect().contains(peer.mapFromGlobal(cursor))
        ):
            self._handoff_to(peer, cursor)
            return
        cursor_y = self.container.mapFromGlobal(cursor).y()
        y = cursor_y - self._drag_offset
        limit = max(0, self.container.height() - self._drag_height)
        panel.setGeometry(
            0, max(0, min(y, limit)), self.container.width(), self._drag_height
        )
        panel.raise_()
        self._update_gap(cursor_y)

    def _target_index(self, cursor_y: int) -> int:
        seen = 0
        for index in range(self.stack.count()):
            widget = self.stack.itemAt(index).widget()
            if widget is None:
                continue
            if widget is self._placeholder:
                if cursor_y < widget.geometry().center().y():
                    return seen
            else:
                if cursor_y < widget.geometry().center().y():
                    return seen
                seen += 1
        return len(self._panels)

    def _update_gap(self, cursor_y: int):
        target = self._target_index(cursor_y)
        if target == self._drop_index:
            return
        self._move_gap(target)

    def _handoff_to(self, peer: "PanelStack", cursor: QPoint):
        panel = self._drag_panel
        if panel is None:
            return
        peer_y = peer.container.mapFromGlobal(cursor).y()
        index = peer._target_index(peer_y)
        if self._gap_animation is not None:
            self._gap_animation.stop()
            self._gap_animation.deleteLater()
            self._gap_animation = None
        if self._placeholder is not None:
            self.stack.removeWidget(self._placeholder)
            self._placeholder.deleteLater()
            self._placeholder = None
        self._drag_panel = None
        self._dragging = False
        self._drop_index = -1
        self._set_dragging(panel, False)
        panel.setGraphicsEffect(None)
        self._shadow = None
        peer._begin_external_drag(panel, cursor, index)
        self.changed.emit()

    def _begin_external_drag(self, panel: CollapsiblePanel, cursor: QPoint, index: int):
        self._drag_panel = panel
        self._dragging = True
        self._drag_height = panel.height()
        self._drag_index = index
        self._drop_index = index
        self._drag_offset = min(self._drag_height, 15)
        panel.header.installEventFilter(self)
        panel.title.installEventFilter(self)
        self._set_dragging(panel, True)
        panel.header.setCursor(Qt.CursorShape.ClosedHandCursor)
        panel.setParent(self.container)
        panel.show()
        panel.header.grabMouse()

        shadow = QGraphicsDropShadowEffect(panel)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 150))
        panel.setGraphicsEffect(shadow)
        self._shadow = shadow

        placeholder = QFrame(self.container)
        placeholder.setObjectName("panel-drop-gap")
        placeholder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        placeholder.setFixedHeight(self._drag_height)
        self.stack.insertWidget(index, placeholder)
        self._placeholder = placeholder
        self._move_drag(cursor)

    def _move_gap(self, target: int):
        placeholder = self._placeholder
        if placeholder is None:
            return
        self._drop_index = target
        self.stack.removeWidget(placeholder)
        self.stack.insertWidget(target, placeholder)
        self._animate_gap(0, self._drag_height)

    def _animate_gap(self, start: int, end: int):
        placeholder = self._placeholder
        if placeholder is None:
            return
        if self._gap_animation is not None:
            self._gap_animation.stop()
            self._gap_animation.deleteLater()
        placeholder.setFixedHeight(start)
        animation = QVariantAnimation(self)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setDuration(GAP_ANIMATION_MS)
        animation.valueChanged.connect(
            lambda progress: placeholder.setFixedHeight(
                round(start + (end - start) * blender_panel_ease(progress))
            )
        )
        animation.finished.connect(lambda: self._finish_gap(animation, end))
        self._gap_animation = animation
        animation.start()

    def _finish_gap(self, animation: QVariantAnimation, end: int):
        if self._gap_animation is not animation:
            animation.deleteLater()
            return
        self._gap_animation = None
        animation.deleteLater()
        if self._placeholder is not None:
            self._placeholder.setFixedHeight(end)

    def _end_drag(self):
        panel = self._drag_panel
        placeholder = self._placeholder
        if panel is None or placeholder is None:
            return
        if self._gap_animation is not None:
            self._gap_animation.stop()
            self._gap_animation.deleteLater()
            self._gap_animation = None
        placeholder.setFixedHeight(self._drag_height)
        self._release_mouse(panel)
        start = panel.geometry()
        end = QRect(
            0,
            placeholder.geometry().y(),
            self.container.width(),
            self._drag_height,
        )
        target = self._drop_index
        animation = QVariantAnimation(self)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setDuration(PANEL_ANIMATION_MS)
        animation.valueChanged.connect(
            lambda progress: panel.setGeometry(
                _lerp_rect(start, end, blender_panel_ease(progress))
            )
        )
        animation.finished.connect(lambda: self._finish_drag(target, animation))
        self._settle_animation = animation
        animation.start()

    def _finish_drag(self, target: int, animation: QVariantAnimation):
        panel = self._drag_panel
        placeholder = self._placeholder
        self._settle_animation = None
        animation.deleteLater()
        if placeholder is not None:
            self.stack.removeWidget(placeholder)
            placeholder.deleteLater()
            self._placeholder = None
        if panel is not None:
            panel.setGraphicsEffect(None)
            self._shadow = None
            panel.setParent(self.container)
            self.stack.insertWidget(target, panel)
            self._panels.insert(target, panel)
            panel.show()
            panel.updateGeometry()
            self._set_dragging(panel, False)
            self._release_mouse(panel)
        self._drag_panel = None
        self._dragging = False
        self._drop_index = -1
        self.changed.emit()

    def _release_mouse(self, panel: CollapsiblePanel):
        panel.header.releaseMouse()
        panel.header.setCursor(Qt.CursorShape.OpenHandCursor)

    def _set_dragging(self, panel, dragging):
        panel.header.setProperty("dragging", dragging)
        style = panel.header.style()
        style.unpolish(panel.header)
        style.polish(panel.header)
        panel.header.update()
