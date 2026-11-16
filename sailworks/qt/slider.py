"""Blender-style number slider: label left, filled bar with the value inside."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import QSizePolicy, QSlider

from .dropdown import field_columns, label_band

ROW_HEIGHT = 32


class Slider(QSlider):
    """Horizontal slider drawn as a BUI number-slider row, without a handle.

    The slider keeps integer positions internally. ``setDecimals`` scales that
    range so the public ``value``/``setValue``/``minimum``/``maximum`` API
    stays in display units, including fractional values.
    """

    def __init__(self, label="", parent=None):
        self._label = label
        self._text = ""
        self._suffix = ""
        self._decimals = 0
        self._scale = 1
        self._minimum = 0.0
        self._maximum = 99.0
        self._single_step = 1.0
        # Space a trailing widget (an info button) takes from the row; see
        # Dropdown.set_column_gutter.
        self._column_gutter = 0
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setFixedHeight(ROW_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.valueChanged.connect(lambda *_: self._refresh_text())
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

    def set_column_gutter(self, pixels):
        """Reserve ``pixels`` at the row's right edge for a trailing widget.

        The label and track columns are computed from the full row width, so
        a row that ends in an info button keeps the same track left edge as
        every other row; only its track's right edge stops sooner.
        """
        gutter = max(0, int(pixels))
        if gutter != self._column_gutter:
            self._column_gutter = gutter
            self.update()

    def column_gutter(self):
        return self._column_gutter

    def _column_width(self):
        return self.width() + self._column_gutter

    def setText(self, text):
        self._text = text
        self.update()

    def setDecimals(self, decimals):
        self._decimals = max(0, int(decimals))
        self._scale = 10**self._decimals
        super().setRange(
            round(self._minimum * self._scale), round(self._maximum * self._scale)
        )
        super().setSingleStep(max(1, round(self._single_step * self._scale)))
        self._refresh_text()

    def setRange(self, minimum, maximum):  # noqa: N802 - Qt virtual method name
        self._minimum = float(minimum)
        self._maximum = float(maximum)
        super().setRange(
            round(self._minimum * self._scale), round(self._maximum * self._scale)
        )
        self._refresh_text()

    def minimum(self):
        return self._minimum

    def maximum(self):
        return self._maximum

    def setSingleStep(self, step):  # noqa: N802 - Qt virtual method name
        self._single_step = float(step)
        super().setSingleStep(max(1, round(self._single_step * self._scale)))

    def singleStep(self):
        return self._single_step

    def value(self):
        return super().value() / self._scale

    def setValue(self, value):  # noqa: N802 - Qt virtual method name
        super().setValue(round(float(value) * self._scale))
        self._refresh_text()

    def setSuffix(self, suffix):
        self._suffix = suffix
        self._refresh_text()

    def _refresh_text(self):
        if self._decimals:
            self._text = f"{self.value():.{self._decimals}f}{self._suffix}"
        else:
            self._text = f"{int(round(self.value()))}{self._suffix}"
        self.update()

    def _field_rect(self) -> QRectF:
        width = self.width()
        # A labelled slider paints its label in the left band, so the track
        # starts at the shared control column. Without a label there is no
        # band to reserve: the track spans the whole widget and lines up with
        # the other fields.
        _, control_left, _ = field_columns(self._column_width())
        left = control_left if self._label else 0
        return QRectF(left, 2, max(0, width - left - 2), max(0, self.height() - 4))

    def _value_at(self, x) -> float:
        field = self._field_rect()
        span = self._maximum - self._minimum
        if field.width() <= 0 or span <= 0:
            return self._minimum
        fraction = min(1.0, max(0.0, (x - field.left()) / field.width()))
        step = self._single_step if self._single_step > 0 else 1 / self._scale
        steps = round((self._minimum + fraction * span - self._minimum) / step)
        return min(self._maximum, max(self._minimum, self._minimum + steps * step))

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
        return QColor("#2b2b2b"), QColor("#3584e4"), QColor("#e6e6e6")

    def paintEvent(self, event):  # noqa: N802 - Qt virtual method name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        field = self._field_rect()
        base, fill, text_color = self._colors()
        metrics = QFontMetrics(self.font())

        if self._label:
            label_rect = label_band(self._column_width(), self.height())
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
