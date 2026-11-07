"""Blender-style dropdown rows: label on the left, current value on the right."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QFontMetrics, QPalette
from PySide6.QtWidgets import (
    QComboBox,
    QSizePolicy,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
)


class Dropdown(QComboBox):
    """A combo box drawn as a Blender panel row.

    The label occupies the left side of the control and the current value is
    right-aligned before the drop arrow, so a labelled dropdown needs one row
    instead of a separate form label and field.
    """

    def __init__(self, label="", parent=None):
        super().__init__(parent)
        self._label = label
        self.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.setMinimumContentsLength(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        if label:
            self.setAccessibleName(label)

    def label(self):
        return self._label

    def setLabel(self, text):
        self._label = text
        if text and not self.accessibleName():
            self.setAccessibleName(text)
        self._sync_editable()
        self.update()

    def _content_rect(self, option):
        field = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxEditField,
            self,
        )
        return QRectF(field).adjusted(8, 0, -8, 0)

    def _label_width(self):
        if not self._label:
            return 0
        return QFontMetrics(self.font()).horizontalAdvance(self._label) + 4

    def _sync_editable(self):
        if self.isEditable() and self.lineEdit() is not None:
            self.lineEdit().setStyleSheet("background: transparent;")
            self.lineEdit().setTextMargins(self._label_width() + 8, 0, 0, 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_editable()

    def _muted_color(self):
        if not self.isEnabled():
            return self.palette().color(
                QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text
            )
        return self.palette().color(QPalette.ColorRole.PlaceholderText)

    def _value_color(self):
        group = (
            QPalette.ColorGroup.Active
            if self.isEnabled()
            else QPalette.ColorGroup.Disabled
        )
        return self.palette().color(group, QPalette.ColorRole.Text)

    def _draw_label(self, painter, rect, width):
        metrics = QFontMetrics(self.font())
        painter.setPen(self._muted_color())
        painter.drawText(
            QRectF(rect.left(), rect.top(), width, rect.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(self._label, Qt.TextElideMode.ElideRight, int(width)),
        )

    def paintEvent(self, event):
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        value = option.currentText
        if self.isEditable():
            super().paintEvent(event)
            if self._label:
                painter = QStylePainter(self)
                rect = self._content_rect(option)
                width = min(self._label_width(), rect.width() * 0.55)
                self._draw_label(painter, rect, width)
            return

        painter = QStylePainter(self)
        option.currentText = ""
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        rect = self._content_rect(option)
        if self._label:
            label_width = min(self._label_width(), rect.width() * 0.55)
            self._draw_label(painter, rect, label_width)
            rect.setLeft(rect.left() + label_width + 8)
        metrics = QFontMetrics(self.font())
        painter.setPen(self._value_color())
        painter.drawText(
            rect,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(value, Qt.TextElideMode.ElideRight, int(rect.width())),
        )
