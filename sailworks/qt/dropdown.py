"""Blender-style dropdown rows: label left, compact field on the right."""

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QWidget,
)

FIELD_FRACTION = 0.55
# Padding before the value text and space reserved for the chevron.
VALUE_INSET = 8
CHEVRON_WIDTH = 26
# Width of the checkbox column in a Blender-style toggle row.
TOGGLE_SLOT = 15
ROW_SPACING = 8


class _HoverItemDelegate(QStyledItemDelegate):
    """Paint a visible hover row without changing the combo's current index."""

    def paint(self, painter, option, index):  # noqa: N802 - Qt virtual method name
        if (
            option.state & QStyle.StateFlag.State_MouseOver
            and not option.state & QStyle.StateFlag.State_Selected
        ):
            painter.fillRect(option.rect, QColor("#3c3c3c"))
        super().paint(painter, option, index)


class Dropdown(QComboBox):
    """A combo box drawn as a Blender property row.

    The left side is the panel background with a right-aligned label. The
    right side is a compact field with the current value at its left edge and
    a chevron at its right edge.
    """

    def __init__(self, label="", parent=None):
        super().__init__(parent)
        self._label = label
        self._value_inset = VALUE_INSET
        self.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.setMinimumContentsLength(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.view().setFrameShape(QFrame.Shape.NoFrame)
        self.view().setMouseTracking(True)
        self.view().setItemDelegate(_HoverItemDelegate(self.view()))
        if label:
            self.setAccessibleName(label)

    def label(self):
        return self._label

    def setLabel(self, text):
        self._label = text
        if text and not self.accessibleName():
            self.setAccessibleName(text)
        self.update()

    def valueInset(self):
        return self._value_inset

    def setValueInset(self, inset):
        """Match the left padding another field (e.g. a QLineEdit) uses."""
        self._value_inset = max(0, int(inset))
        self._sync_editable()
        self.update()

    def _field_rect(self) -> QRectF:
        width = self.width()
        height = self.height()
        # An unlabelled dropdown has no label band: the field uses the full
        # width. Only a labelled row reserves the left part for its label.
        left = round(width * (1.0 - FIELD_FRACTION)) if self._label else 0
        return QRectF(left, 2, max(0, width - left - 2), max(0, height - 4))

    def value_rect(self) -> QRectF:
        """Rect the current value is painted in, inside the field."""
        return self._field_rect().adjusted(self._value_inset, 0, -CHEVRON_WIDTH, 0)

    def _sync_editable(self):
        if not self.isEditable() or self.lineEdit() is None:
            return
        field = self.value_rect()
        self.lineEdit().setStyleSheet(
            "background: transparent; border: none; padding: 0px;"
        )
        self.lineEdit().setTextMargins(0, 0, 0, 0)
        self.lineEdit().setGeometry(
            int(field.left()),
            int(field.top()) + 1,
            max(0, int(field.width())),
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
        popup = self.view().window()
        popup.setFrameShape(QFrame.Shape.NoFrame)
        popup.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        popup.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        popup.setStyleSheet(
            "QFrame { border: none; background: #1e1e1e; }"
            " QAbstractItemView { border: none; background: #1e1e1e; }"
        )
        super().showPopup()
        self._place_popup(popup)
        self.update()

    def _place_popup(self, popup):
        """Keep the field visible: place the menu below it, or above if needed."""
        screen = self.screen().availableGeometry()
        top = self.mapToGlobal(QPoint(0, 0))
        width = max(self.width(), popup.width())
        height = min(popup.height(), screen.height())
        x = max(screen.left(), min(top.x(), screen.right() - width + 1))
        y = top.y() + self.height()
        if y + height > screen.bottom() and top.y() - height >= screen.top():
            y = top.y() - height
        popup.setGeometry(x, y, width, height)

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

        value_rect = self.value_rect()
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


class FieldRow(QWidget):
    """A dropdown-style label row around an arbitrary compact field."""

    def __init__(self, label="", field=None, parent=None):
        super().__init__(parent)
        self._field = field
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._label = QLabel(label)
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self._label, 1)
        if field is not None:
            field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            layout.addWidget(field)
        if label:
            self.setAccessibleName(label)

    def field(self):
        return self._field

    def minimumSizeHint(self):  # noqa: N802 - Qt virtual method name
        """Report only a height minimum; the parent layout sets the width.

        ``resizeEvent`` pins the label and field to fractions of the row
        width. Fixed widths feed back into the layout's minimum size, so the
        row's minimum width would track its current width and grow on every
        resize, keeping the enclosing scroll areas from ever settling.
        """
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, event):  # noqa: N802 - Qt virtual method name
        super().resizeEvent(event)
        if self._field is None:
            return
        width = self.width()
        field_width = max(0, round(width * FIELD_FRACTION) - 2)
        label_width = max(0, width - field_width - 8)
        if self._field.width() != field_width:
            self._field.setFixedWidth(field_width)
        if self._label.width() != label_width:
            self._label.setFixedWidth(label_width)


class ToggleFieldRow(QWidget):
    """Blender-style property row: label, small checkbox, then a field.

    The checkbox column is always reserved, even on rows without a toggle, so
    every field in a stack starts at the same x. The row owns the enable and
    disable behaviour: a row with a checkbox starts with its field disabled
    and enables it while the checkbox is checked. ``checked_changed`` relays
    that state instead of letting callers wire the checkbox themselves.
    """

    checked_changed = Signal(bool)

    def __init__(self, label="", field=None, checkbox=None, parent=None):
        super().__init__(parent)
        self._field = field
        self._checkbox = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(ROW_SPACING)
        slot = QWidget(self)
        slot.setFixedWidth(TOGGLE_SLOT)
        slot_layout = QHBoxLayout(slot)
        slot_layout.setContentsMargins(0, 0, 0, 0)
        slot_layout.setSpacing(0)
        # ``checkbox=False`` asks for the empty column only; the field stays
        # enabled and there is no toggle to report.
        if checkbox is not False:
            if checkbox is None:
                checkbox = QCheckBox(slot)
            if isinstance(checkbox, QCheckBox) and not checkbox.text():
                if not checkbox.accessibleName():
                    checkbox.setAccessibleName(f"Enable {label}" if label else "Enable")
            checkbox.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            slot_layout.addWidget(checkbox, 0, Qt.AlignmentFlag.AlignVCenter)
            self._checkbox = checkbox
        layout.addWidget(slot, 0, Qt.AlignmentFlag.AlignVCenter)
        self._label = QLabel(label)
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self._label, 1)
        if field is not None:
            field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            layout.addWidget(field)
        if self._checkbox is not None:
            self._checkbox.toggled.connect(self._toggle_changed)
            if field is not None:
                field.setEnabled(self._checkbox.isChecked())
        if label:
            self.setAccessibleName(label)

    def checkbox(self):
        return self._checkbox

    def field(self):
        return self._field

    def is_checked(self):
        return self._checkbox is not None and self._checkbox.isChecked()

    def set_checked(self, checked):
        if self._checkbox is None:
            return
        self._checkbox.setChecked(bool(checked))

    def _toggle_changed(self, checked):
        if self._field is not None:
            self._field.setEnabled(checked)
        self.checked_changed.emit(checked)

    def minimumSizeHint(self):  # noqa: N802 - Qt virtual method name
        """Report only a height minimum; the parent layout sets the width.

        ``resizeEvent`` pins the label and field to fractions of the row
        width. Fixed widths feed back into the layout's minimum size, so the
        row's minimum width would track its current width and grow on every
        resize, keeping the enclosing scroll areas from ever settling.
        """
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, event):  # noqa: N802 - Qt virtual method name
        super().resizeEvent(event)
        if self._field is None:
            return
        width = self.width()
        available = max(0, width - TOGGLE_SLOT - ROW_SPACING)
        field_width = max(0, round(available * FIELD_FRACTION) - 2)
        label_width = max(0, available - field_width - ROW_SPACING)
        if self._field.width() != field_width:
            self._field.setFixedWidth(field_width)
        if self._label.width() != label_width:
            self._label.setFixedWidth(label_width)
