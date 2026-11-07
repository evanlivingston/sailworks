"""Blender-style number slider: a filled bar with the value drawn inside."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import QSizePolicy, QSlider


class Slider(QSlider):
    """Horizontal slider drawn as a BUI number-slider bar with no handle."""

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._text = ""
        self.setMinimumHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)

    def text(self):
        return self._text

    def setText(self, text):
        self._text = text
        self.update()

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
        rect = QRectF(self.rect()).adjusted(0, 2, 0, -2)
        base, fill, text = self._colors()

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(base)
        painter.drawRoundedRect(rect, 4, 4)

        span = self.maximum() - self.minimum()
        fraction = (self.value() - self.minimum()) / span if span else 0.0
        if fraction > 0:
            painter.save()
            painter.setClipRect(
                QRectF(rect.left(), rect.top(), rect.width() * fraction, rect.height())
            )
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 4, 4)
            painter.restore()

        label = self._text or str(self.value())
        text_rect = rect.adjusted(8, 0, -8, 0)
        painter.setPen(text)
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            QFontMetrics(self.font()).elidedText(
                label, Qt.TextElideMode.ElideRight, int(text_rect.width())
            ),
        )
