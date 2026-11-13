"""Properties dialog for a track or route point."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from sailworks.core.geo.rounding import (
    ROUNDING_LABELS,
    ROUNDING_PORT,
    ROUNDING_STARBOARD,
    normalize_rounding,
    normalize_rounding_spec,
)
from .dropdown import FIELD_FRACTION, Dropdown

# One label column, one field column: every row starts at the same x.
LABEL_COLUMN = 0
FIELD_COLUMN = 1
MARGIN = 12
SPACING = 8
# The dropdown paints its value inside the right-hand FIELD_FRACTION of the
# widget: 8px of left padding, a 26px chevron and 2px of field margins. Ask
# for enough width that the longest rounding label is never elided.
DROPDOWN_CHROME = 44


class PointEditDialog(QDialog):
    """Edit a point name and, for route marks, its rounding constraint."""

    def __init__(
        self,
        name,
        rounding=None,
        coordinates=None,
        radius_nm=None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Point Properties")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.setSpacing(SPACING)

        form = QGridLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(SPACING * 2)
        form.setVerticalSpacing(SPACING)
        form.setColumnStretch(FIELD_COLUMN, 1)
        self.form = form

        self.name_edit = QLineEdit(str(name or ""))
        self.name_label = self._form_label("Name")
        form.addWidget(self.name_label, 0, LABEL_COLUMN)
        form.addWidget(self.name_edit, 0, FIELD_COLUMN)

        self.rounding_combo = None
        self.rounding_label = None
        self.rounding_details = None
        # The persisted radius is inert legacy metadata: it is carried through
        # untouched so saving the dialog never rewrites or drops it.
        self.radius_nm = radius_nm
        row = 1
        if rounding is not None:
            self.rounding_label = self._form_label("Rounding")
            # The dropdown draws its own right-aligned label; this row already
            # has one in the label column, so the widget is used unlabelled.
            self.rounding_combo = Dropdown()
            self.rounding_combo.setAccessibleName("Rounding")
            for value, label in ROUNDING_LABELS.items():
                self.rounding_combo.addItem(label, value)
            self.rounding_combo.setCurrentIndex(
                self.rounding_combo.findData(normalize_rounding(rounding))
            )
            self.rounding_combo.setMinimumWidth(self._rounding_width())
            form.addWidget(self.rounding_label, row, LABEL_COLUMN)
            form.addWidget(self.rounding_combo, row, FIELD_COLUMN)
            row += 1

        self.position_label = None
        self.position_value = None
        if coordinates is not None:
            self.position_label = self._form_label("Position")
            self.position_value = QLabel(f"{coordinates[0]:.6f}, {coordinates[1]:.6f}")
            self.position_value.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            form.addWidget(self.position_label, row, LABEL_COLUMN)
            form.addWidget(self.position_value, row, FIELD_COLUMN)

        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _form_label(text):
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return label

    def _rounding_width(self):
        """Width that keeps the longest rounding label inside the field."""
        metrics = self.rounding_combo.fontMetrics()
        widest = max(
            (metrics.horizontalAdvance(label) for label in ROUNDING_LABELS.values()),
            default=0,
        )
        return int((widest + DROPDOWN_CHROME + 8) / FIELD_FRACTION)

    def point_name(self):
        return self.name_edit.text().strip()

    def point_rounding(self):
        if self.rounding_combo is None:
            return None
        return self.rounding_combo.currentData()

    def point_rounding_spec(self):
        if self.rounding_combo is None:
            return None
        side = self.rounding_combo.currentData()
        if side not in (ROUNDING_PORT, ROUNDING_STARBOARD):
            return None
        return normalize_rounding_spec(
            {
                "side": "port" if side == ROUNDING_PORT else "starboard",
                "radius_nm": self.radius_nm,
            }
        )
