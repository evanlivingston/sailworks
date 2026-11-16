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


def field_columns(width, *, toggle_slot=0):
    """Label width, control-column x and field width for a Blender-style row.

    The control column starts at the same x on every row, whether the row
    carries a checkbox or not: ``width * (1.0 - FIELD_FRACTION)``. The label
    ends ``ROW_SPACING`` before that column, so label right edges agree across
    a stack of rows. A row with a checkbox keeps a ``TOGGLE_SLOT`` slot at the
    control column and insets its field past the slot; a row without one
    starts its field at the control column. Every field ends flush with the
    row's right edge, which the caller gets back as a width.
    """
    control_left = max(0, round(width * (1.0 - FIELD_FRACTION)))
    label_width = max(0, control_left - ROW_SPACING)
    inset = toggle_slot + ROW_SPACING if toggle_slot else 0
    field_width = max(0, width - control_left - inset)
    return label_width, control_left, field_width


def label_band(width, height):
    """Rect a painted label fills: from x=0 to ``ROW_SPACING`` before the field.

    Widgets that paint their own label (``Dropdown``, ``Slider``) use this so
    their label ends where a layout-built label does in a ``ToggleFieldRow``.
    """
    label_width, _, _ = field_columns(width)
    return QRectF(0, 0, label_width, height)


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
        # Space a trailing widget (an info button) takes from the row. The
        # columns come from the row width plus this gutter, so a trailing
        # button never moves the control's left edge.
        self._column_gutter = 0
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

    def set_column_gutter(self, pixels):
        """Reserve ``pixels`` at the row's right edge for a trailing widget.

        The label and control columns are computed from the full row width,
        so a row that ends in an info button keeps the same control left edge
        as every other row; only its field's right edge stops sooner.
        """
        gutter = max(0, int(pixels))
        if gutter != self._column_gutter:
            self._column_gutter = gutter
            self.update()

    def column_gutter(self):
        return self._column_gutter

    def _column_width(self):
        return self.width() + self._column_gutter

    def setValueInset(self, inset):
        """Match the left padding another field (e.g. a QLineEdit) uses."""
        self._value_inset = max(0, int(inset))
        self._sync_editable()
        self.update()

    def _field_rect(self) -> QRectF:
        width = self.width()
        height = self.height()
        # The control column comes from the shared row model, so a labelled
        # row's field starts where a toggle row's checkbox does. An unlabelled
        # dropdown has no label band: the field uses the full width.
        _, control_left, _ = field_columns(self._column_width())
        left = control_left if self._label else 0
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
        label_rect = label_band(self._column_width(), self.height())
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
    """A dropdown-style label row around an arbitrary compact field.

    There is no checkbox, so the field starts at the control column itself:
    the same x a checkbox sits at in ``ToggleFieldRow`` and a slider track
    starts at in a checkbox-free row. Label right edges agree across a stack.
    """

    def __init__(self, label="", field=None, parent=None):
        super().__init__(parent)
        self._field = field
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(ROW_SPACING)
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
        label_width, _control_left, field_width = field_columns(width)
        if self._field.width() != field_width:
            self._field.setFixedWidth(field_width)
        if self._label.width() != label_width:
            self._label.setFixedWidth(label_width)


class ToggleFieldRow(QWidget):
    """Blender-style property row: label, optional checkbox, then a field.

    The checkbox, when present, sits at the control column and insets the
    field past its slot; a ``checkbox=False`` row has no slot widget at all,
    so its control starts at that same column. The row owns the enable and
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
        self._label = QLabel(label)
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        # Blender's order: the label owns the stretch, then the checkbox
        # column, then the field. ``checkbox=False`` asks for no column at
        # all, so the field starts at the control column.
        layout.addWidget(self._label, 1)
        if checkbox is not False:
            slot = QWidget(self)
            slot.setFixedWidth(TOGGLE_SLOT)
            slot_layout = QHBoxLayout(slot)
            slot_layout.setContentsMargins(0, 0, 0, 0)
            slot_layout.setSpacing(0)
            if checkbox is None:
                checkbox = QCheckBox(slot)
            if isinstance(checkbox, QCheckBox) and not checkbox.text():
                if not checkbox.accessibleName():
                    checkbox.setAccessibleName(f"Enable {label}" if label else "Enable")
            checkbox.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            slot_layout.addWidget(checkbox, 0, Qt.AlignmentFlag.AlignVCenter)
            self._checkbox = checkbox
            layout.addWidget(slot, 0, Qt.AlignmentFlag.AlignVCenter)
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
        toggle_slot = TOGGLE_SLOT if self._checkbox is not None else 0
        label_width, _control_left, field_width = field_columns(
            width, toggle_slot=toggle_slot
        )
        if self._field.width() != field_width:
            self._field.setFixedWidth(field_width)
        if self._label.width() != label_width:
            self._label.setFixedWidth(label_width)
