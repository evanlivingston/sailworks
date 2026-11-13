"""Properties dialog for a track or route point."""

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
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
from .dropdown import Dropdown


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
        form = QFormLayout()
        self.name_edit = QLineEdit(str(name or ""))
        form.addRow("Name", self.name_edit)
        self.rounding_combo = None
        self.rounding_details = None
        # The persisted radius is inert legacy metadata: it is carried through
        # untouched so saving the dialog never rewrites or drops it.
        self.radius_nm = radius_nm
        if rounding is not None:
            self.rounding_combo = Dropdown("Rounding")
            for value, label in ROUNDING_LABELS.items():
                self.rounding_combo.addItem(label, value)
            self.rounding_combo.setCurrentIndex(
                self.rounding_combo.findData(normalize_rounding(rounding))
            )
            form.addRow(self.rounding_combo)

        if coordinates is not None:
            form.addRow(
                "Position", QLabel(f"{coordinates[0]:.6f}, {coordinates[1]:.6f}")
            )
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

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
