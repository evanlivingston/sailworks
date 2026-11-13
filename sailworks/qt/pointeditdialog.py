"""Properties dialog for a track or route point."""

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from sailworks.core.geo.rounding import (
    DEFAULT_ROUNDING_RADIUS_NM,
    ROUNDING_LABELS,
    ROUNDING_PORT,
    ROUNDING_STARBOARD,
    normalize_rounding,
    normalize_rounding_spec,
)


class PointEditDialog(QDialog):
    """Edit a point name and, for route marks, its rounding constraint."""

    def __init__(
        self,
        name,
        rounding=None,
        coordinates=None,
        radius_nm=None,
        exit_bearing_min_deg=None,
        exit_bearing_max_deg=None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Point Properties")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(str(name or ""))
        form.addRow("Name", self.name_edit)
        self.rounding_combo = None
        self.radius = None
        self.exit_min = None
        self.exit_max = None
        if rounding is not None:
            self.rounding_combo = QComboBox()
            for value, label in ROUNDING_LABELS.items():
                self.rounding_combo.addItem(label, value)
            self.rounding_combo.setCurrentIndex(
                self.rounding_combo.findData(normalize_rounding(rounding))
            )
            form.addRow("Rounding", self.rounding_combo)

            self.radius = QDoubleSpinBox()
            self.radius.setRange(0.05, 1000.0)
            self.radius.setDecimals(2)
            self.radius.setSingleStep(0.1)
            self.radius.setSuffix(" nm")
            self.radius.setValue(
                DEFAULT_ROUNDING_RADIUS_NM if radius_nm is None else float(radius_nm)
            )
            form.addRow("Radius", self.radius)

            self.exit_min = QDoubleSpinBox()
            self.exit_min.setRange(-1, 360)
            self.exit_min.setDecimals(1)
            self.exit_min.setSpecialValueText("none")
            self.exit_min.setValue(
                -1 if exit_bearing_min_deg is None else float(exit_bearing_min_deg)
            )
            form.addRow("Exit min", self.exit_min)

            self.exit_max = QDoubleSpinBox()
            self.exit_max.setRange(-1, 360)
            self.exit_max.setDecimals(1)
            self.exit_max.setSpecialValueText("none")
            self.exit_max.setValue(
                -1 if exit_bearing_max_deg is None else float(exit_bearing_max_deg)
            )
            form.addRow("Exit max", self.exit_max)

            note = QLabel(
                "Radius and exit apply only when Rounding is Port or Starboard. "
                "Leave both exit fields at none for no exit-heading requirement."
            )
            note.setWordWrap(True)
            note.setProperty("secondary", True)
            form.addRow(note)
            self.rounding_combo.currentIndexChanged.connect(self._sync_rounding_fields)
            self._sync_rounding_fields()
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

    def _sync_rounding_fields(self, *_):
        if self.rounding_combo is None:
            return
        enabled = self.rounding_combo.currentData() in (
            ROUNDING_PORT,
            ROUNDING_STARBOARD,
        )
        for widget in (self.radius, self.exit_min, self.exit_max):
            if widget is not None:
                widget.setEnabled(enabled)

    def accept(self):
        if self.rounding_combo is not None and self.rounding_combo.currentData() in (
            ROUNDING_PORT,
            ROUNDING_STARBOARD,
        ):
            minimum = self.exit_min.value()
            maximum = self.exit_max.value()
            has_minimum = minimum >= 0
            has_maximum = maximum >= 0
            if has_minimum != has_maximum:
                QMessageBox.warning(
                    self,
                    "Rounding",
                    "Set both exit bearings, or leave both at none.",
                )
                return
            if has_minimum and maximum >= 360:
                QMessageBox.warning(
                    self,
                    "Rounding",
                    "Exit maximum must be less than 360 degrees.",
                )
                return
            if has_minimum and minimum > maximum:
                QMessageBox.warning(
                    self,
                    "Rounding",
                    "Exit minimum must not be greater than exit maximum.",
                )
                return
        super().accept()

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
        minimum = self.exit_min.value()
        maximum = self.exit_max.value()
        return normalize_rounding_spec(
            {
                "side": "port" if side == ROUNDING_PORT else "starboard",
                "radius_nm": self.radius.value(),
                "exit_bearing_min_deg": None if minimum < 0 else minimum,
                "exit_bearing_max_deg": None if maximum < 0 else maximum,
            }
        )
