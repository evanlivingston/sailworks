"""Isochrone inspector panel for a retained native routing result."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from sailworks.core.marine.routinginspection import RoutingInspector
from sailworks.core.marine.routingresult import RoutingResult

READOUT_ORDER = (
    "heading_degrees",
    "course_over_ground_degrees",
    "speed_through_water_knots",
    "speed_over_ground_knots",
    "true_wind_speed_knots",
    "true_wind_angle_degrees",
    "propulsion_mode",
    "tack",
    "warning_count",
)


class RoutingInspectorPanel(QWidget):
    """Layer list, trace and leg readouts for one routing calculation."""

    layer_selected = Signal(str)
    leg_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._result = None
        self._inspector = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)

        self.layer_combo = QComboBox(self)
        self.layer_combo.currentIndexChanged.connect(self._layer_changed)
        form = QFormLayout()
        form.addRow("Layer", self.layer_combo)
        self.time_label = QLabel("No layer", self)
        self.time_label.setWordWrap(True)
        form.addRow("Time", self.time_label)
        self.leg_combo = QComboBox(self)
        self.leg_combo.currentIndexChanged.connect(self._leg_changed)
        form.addRow("Leg", self.leg_combo)
        layout.addLayout(form)

        self.readout = QPlainTextEdit(self)
        self.readout.setReadOnly(True)
        self.readout.setPlaceholderText("No routing result loaded")
        layout.addWidget(self.readout, 1)

    def set_result(self, result: RoutingResult):
        if not isinstance(result, RoutingResult):
            raise TypeError("result must be a RoutingResult")
        self._result = result
        self._inspector = RoutingInspector(result)
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        for layer in self._inspector.layers():
            self.layer_combo.addItem(layer.layer_id)
        self.layer_combo.blockSignals(False)
        self.leg_combo.clear()
        for leg in result.legs:
            self.leg_combo.addItem(leg.leg_id)
        if self.layer_combo.count():
            self.layer_combo.setCurrentIndex(0)
            self._layer_changed(0)
        else:
            self.time_label.setText("No layers recorded")
        if self.leg_combo.count():
            self.leg_combo.setCurrentIndex(0)
            self._leg_changed(0)
        else:
            self.readout.setPlainText("No delivered legs recorded")

    def select_nearest_layer(self, epoch_s: int) -> bool:
        if self._inspector is None:
            return False
        layer = self._inspector.nearest_layer(epoch_s)
        if layer is None:
            return False
        index = self.layer_combo.findText(layer.layer_id)
        if index < 0:
            return False
        self.layer_combo.setCurrentIndex(index)
        return True

    def _layer_changed(self, index: int):
        if self._inspector is None or index < 0:
            return
        layers = self._inspector.layers()
        if index >= len(layers):
            return
        layer = layers[index]
        direction = "reverse" if layer.reverse else "forward"
        self.time_label.setText(
            f"{layer.time_utc}  (+{layer.offset_s} s, {direction}, "
            f"{layer.contour_count} contours)"
        )
        self.layer_selected.emit(layer.layer_id)

    def _leg_changed(self, index: int):
        if self._inspector is None or index < 0:
            return
        if index >= len(self._result.legs):
            return
        leg = self._result.legs[index]
        values = self._inspector.leg_readout(leg.leg_id)
        lines = [f"{leg.start_utc} → {leg.end_utc}"]
        for name in READOUT_ORDER:
            if name in values["values"]:
                lines.append(f"{name}: {values['values'][name]}")
            else:
                lines.append(f"{name}: unknown")
        if values["missing"]:
            lines.append("missing: " + ", ".join(values["missing"]))
        self.readout.setPlainText("\n".join(lines))
        self.leg_selected.emit(leg.leg_id)
