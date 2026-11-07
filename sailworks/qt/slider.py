"""Blender-style number slider: label left, filled bar with the value inside."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import QSizePolicy, QSlider

from .dropdown import FIELD_FRACTION

ROW_HEIGHT = 32


class Slider(QSlider):
    """Horizontal slider drawn as a BUI number-slider row, without a handle."""

    def __init__(self, label="", parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._label = label
        self._text = ""
        self._suffix = ""
        self.setFixedHeight(ROW_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        if label:
            self.setAccessibleName(label)

    def label(self):
        return self._label

    def setLabel(self, text):
        self._label = text
        if text and not self.accessibleName():
            self.setAccessibleName(text)
        self.update()

    def text(self):
        return self._text

    def setText(self, text):
        self._text = text
        self.update()

    def setValue(self, value):  # noqa: N802 - Qt virtual method name
        super().setValue(value)
        self._text = f"{self.value()}{self._suffix}"
        self.update()

    def setSuffix(self, suffix):
        self._suffix = suffix
        self._text = f"{self.value()}{suffix}"
        self.update()

    def _field_rect(self) -> QRectF:
        width = self.width()
        left = round(width * (1.0 - FIELD_FRACTION))
        return QRectF(left, 2, max(0, width - left - 2), max(0, self.height() - 4))

    def _value_at(self, x) -> int:
        field = self._field_rect()
        span = self.maximum() - self.minimum()
        if field.width() <= 0 or span <= 0:
            return self.minimum()
        fraction = min(1.0, max(0.0, (x - field.left()) / field.width()))
        return round(self.minimum() + fraction * span)

    def _set_from_x(self, x):
        value = self._value_at(x)
        if value != self.value():
            self.setValue(value)

    def mousePressEvent(self, event):  # noqa: N802 - Qt virtual method name
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.setSliderDown(True)
            self._set_from_x(event.position().x())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt virtual method name
        if self.isSliderDown() and event.buttons() & Qt.MouseButton.LeftButton:
            self._set_from_x(event.position().x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt virtual method name
        if event.button() == Qt.MouseButton.LeftButton:
            self._set_from_x(event.position().x())
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.update()
        super().leaveEvent(event)

    def _colors(self):
        if not self.isEnabled():
            return QColor("#242424"), QColor("#3a4656"), QColor("#777774")
        if self.underMouse():
            return QColor("#303030"), QColor("#3584e4"), QColor("#ffffff")
        return QColor("#2b2b2b"), QColor("#3584e4"), QColor("#eeeeec")

    def paintEvent(self, event):  # noqa: N802 - Qt virtual method name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        field = self._field_rect()
        base, fill, text_color = self._colors()
        metrics = QFontMetrics(self.font())

        if self._label:
            label_rect = QRectF(6, 0, max(0, field.left() - 18), self.height())
            painter.setPen(text_color)
            painter.drawText(
                label_rect,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                metrics.elidedText(
                    self._label,
                    Qt.TextElideMode.ElideRight,
                    int(label_rect.width()),
                ),
            )

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(base)
        painter.drawRoundedRect(field, 3, 3)

        span = self.maximum() - self.minimum()
        fraction = (self.value() - self.minimum()) / span if span else 0.0
        if fraction > 0:
            painter.save()
            painter.setClipRect(
                QRectF(
                    field.left(),
                    field.top(),
                    field.width() * fraction,
                    field.height(),
                )
            )
            painter.setBrush(fill)
            painter.drawRoundedRect(field, 3, 3)
            painter.restore()

        label = self._text or str(self.value())
        text_rect = field.adjusted(8, 0, -8, 0)
        painter.setPen(text_color)
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(
                label, Qt.TextElideMode.ElideRight, int(text_rect.width())
            ),
        )
