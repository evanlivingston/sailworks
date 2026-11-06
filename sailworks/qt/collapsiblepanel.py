"""A dock body with a Blender-style collapsible header row."""

import math

from PySide6.QtCore import QEvent, QVariantAnimation, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# Blender's panel curve, shortened from 0.30 s for a snappier toggle.
PANEL_ANIMATION_MS = 160
_UNCONSTRAINED = (1 << 24) - 1


def blender_panel_ease(progress: float) -> float:
    """Blender's panel open fraction for linear progress 0..1."""
    ramp = math.sqrt(max(0.0, min(1.0, progress)))
    return ramp * ramp * (3.0 - 2.0 * ramp)


class CollapsiblePanel(QWidget):
    """Wrap panel content with an optional header and a collapsible body.

    Tabbed docks show the header row, giving each tab a visible triangle
    arrow. A lone or floating dock hides the row and uses the arrow in its
    title bar instead, so the panel title is never shown twice.
    """

    def __init__(self, title, content, parent=None):
        super().__init__(parent)
        self.setObjectName("collapsible-panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = QWidget(self)
        self.header.setObjectName("panel-header")
        self.header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.header.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        row = QHBoxLayout(self.header)
        row.setContentsMargins(6, 2, 6, 2)
        row.setSpacing(6)
        self.collapse_button = QToolButton(self.header)
        self.collapse_button.setObjectName("panel-collapse")
        self.collapse_button.setAutoRaise(True)
        self.collapse_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.collapse_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collapse_button.setArrowType(Qt.ArrowType.DownArrow)
        self.collapse_button.setFixedSize(18, 18)
        self.title = QLabel(title, self.header)
        self.title.setObjectName("panel-title")
        self.title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.title.setMinimumWidth(0)
        row.addWidget(self.collapse_button)
        row.addWidget(self.title, 1)
        self.close_button = QToolButton(self.header)
        self.close_button.setObjectName("panel-close")
        self.close_button.setText("✕")
        self.close_button.setFixedSize(18, 18)
        self.close_button.setAutoRaise(True)
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.setToolTip("Remove panel")
        self.close_button.setAccessibleName("Remove panel")
        self.close_button.hide()
        row.addWidget(self.close_button)
        self._closable = False
        self.header.installEventFilter(self)

        self.body = content
        previous = content.parentWidget()
        scroll = previous.parentWidget() if previous is not None else None
        if isinstance(scroll, QScrollArea) and scroll.widget() is content:
            # A QScrollArea keeps resizing its old widget after a reparent;
            # detach it so this panel's layout owns the body.
            scroll.takeWidget()
        content.setParent(self)
        layout.addWidget(self.header, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(content, 1)
        self.header.hide()
        self._collapsed = False
        self._animation: QVariantAnimation | None = None

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool, animate: bool = True):
        if self._animation is not None:
            self._animation.stop()
            self._animation.deleteLater()
            self._animation = None
        self._collapsed = collapsed
        self.collapse_button.setArrowType(
            Qt.ArrowType.RightArrow if collapsed else Qt.ArrowType.DownArrow
        )
        self._sync_labels(collapsed)
        if not animate or not self.isVisible():
            self._finish_collapse(collapsed)
            return
        start = self.body.height() if self.body.isVisible() else 0
        end = 0 if collapsed else self.body.sizeHint().height()
        if start == end:
            self._finish_collapse(collapsed)
            return
        self.body.setVisible(True)
        self.body.setFixedHeight(max(0, start))
        self.setFixedHeight(self._header_height() + max(0, start))
        animation = QVariantAnimation(self)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setDuration(PANEL_ANIMATION_MS)
        animation.valueChanged.connect(
            lambda progress: self._animate_height(start, end, progress)
        )
        animation.finished.connect(lambda: self._finish_collapse(collapsed))
        self._animation = animation
        animation.start()

    def _animate_height(self, start: int, end: int, progress: float):
        height = max(0, round(start + (end - start) * blender_panel_ease(progress)))
        self.body.setFixedHeight(height)
        self.setFixedHeight(self._header_height() + height)

    def _finish_collapse(self, collapsed: bool):
        animation = self._animation
        self._animation = None
        if animation is not None:
            animation.deleteLater()
        self.body.setMinimumHeight(0)
        self.body.setMaximumHeight(_UNCONSTRAINED)
        self.setMinimumHeight(0)
        self.setMaximumHeight(_UNCONSTRAINED)
        self.body.setVisible(not collapsed)
        self.updateGeometry()

    def _header_height(self) -> int:
        return self.header.sizeHint().height()

    def set_header_visible(self, visible: bool):
        self.header.setVisible(visible)

    def set_closable(self, closable: bool):
        self._closable = closable
        if not closable:
            self.close_button.hide()
        self._sync_labels(self._collapsed)

    def eventFilter(self, watched, event):  # noqa: N802 - Qt virtual method name
        if watched is self.header:
            if event.type() == QEvent.Type.Enter and self._closable:
                self.close_button.show()
            elif event.type() == QEvent.Type.Leave:
                self.close_button.hide()
        return super().eventFilter(watched, event)

    def _sync_labels(self, collapsed: bool):
        action = "Expand" if collapsed else "Collapse"
        self.collapse_button.setToolTip(f"{action} {self.title.text()} panel")
        self.collapse_button.setAccessibleName(f"{action} {self.title.text()} panel")
        self.close_button.setToolTip(f"Remove {self.title.text()} panel")
        self.close_button.setAccessibleName(f"Remove {self.title.text()} panel")
