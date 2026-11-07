"""Blender-style dropdown rows: label left, compact field on the right."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPalette, QPen
from PySide6.QtWidgets import QComboBox, QSizePolicy

FIELD_FRACTION = 0.55


class Dropdown(QComboBox):
    """A combo box drawn as a Blender property row.

    The left side is the panel background with a right-aligned label. The
    right side is a compact field with the current value at its left edge and
    a chevron at its right edge.
    """

    def __init__(self, label="", parent=None):
        super().__init__(parent)
        self._label = label
        self.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.setMinimumContentsLength(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        if label:
            self.setAccessibleName(label)

    def label(self):
        return self._label

    def setLabel(self, text):
        self._label = text
        if text and not self.accessibleName():
            self.setAccessibleName(text)
        self.update()

    def _field_rect(self) -> QRectF:
        width = self.width()
        height = self.height()
        left = round(width * (1.0 - FIELD_FRACTION))
        return QRectF(left, 2, max(0, width - left - 2), max(0, height - 4))

    def _sync_editable(self):
        if not self.isEditable() or self.lineEdit() is None:
            return
        field = self._field_rect()
        self.lineEdit().setStyleSheet(
            "background: transparent; border: none; padding: 0px;"
        )
        self.lineEdit().setTextMargins(0, 0, 0, 0)
        self.lineEdit().setGeometry(
            int(field.left()) + 8,
            int(field.top()) + 1,
            max(0, int(field.width()) - 32),
            max(0, int(field.height()) - 2),
        )

    def resizeEvent(self, event):  # noqa: N802 - Qt virtual method name
        super().resizeEvent(event)
        self._sync_editable()

    def enterEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.update()
        super().leaveEvent(event)

    def _colors(self):
        palette = self.palette()
        if not self.isEnabled():
            return QColor("#242424"), QColor("#242424"), QColor("#777774")
        if self.view().isVisible():
            return QColor("#3584e4"), QColor("#3584e4"), QColor("#ffffff")
        if self.underMouse():
            return (
                QColor("#303030"),
                QColor("#303030"),
                palette.color(QPalette.ColorRole.Text),
            )
        return (
            QColor("#2b2b2b"),
            QColor("#2b2b2b"),
            palette.color(QPalette.ColorRole.Text),
        )

    def showPopup(self):  # noqa: N802 - Qt virtual method name
        super().showPopup()
        self.update()

    def hidePopup(self):  # noqa: N802 - Qt virtual method name
        super().hidePopup()
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt virtual method name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        field = self._field_rect()
        fill, border, text = self._colors()

        painter.setPen(QPen(border, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(field, 3, 3)

        metrics = QFontMetrics(self.font())
        label_rect = QRectF(6, 0, max(0, field.left() - 18), self.height())
        if self._label:
            painter.setPen(text)
            painter.drawText(
                label_rect,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                metrics.elidedText(
                    self._label,
                    Qt.TextElideMode.ElideRight,
                    int(label_rect.width()),
                ),
            )

        value_rect = field.adjusted(8, 0, -26, 0)
        if not self.isEditable():
            painter.setPen(text)
            painter.drawText(
                value_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                metrics.elidedText(
                    self.currentText(),
                    Qt.TextElideMode.ElideRight,
                    int(value_rect.width()),
                ),
            )

        arrow = QPen(text, 1.5)
        arrow.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(arrow)
        cx = field.right() - 11
        cy = field.center().y()
        painter.drawLine(QPointF(cx - 4, cy - 2), QPointF(cx, cy + 2))
        painter.drawLine(QPointF(cx, cy + 2), QPointF(cx + 4, cy - 2))
