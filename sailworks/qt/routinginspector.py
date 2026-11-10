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
from sailworks.core.marine.routingselection import RoutingSelection

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
        self._selection = None
        self._unsubscribe = None
        self._syncing = False
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

        self.diagnostics = QLabel("No routing result loaded", self)
        self.diagnostics.setWordWrap(True)
        layout.addWidget(self.diagnostics)

        self.readout = QPlainTextEdit(self)
        self.readout.setReadOnly(True)
        self.readout.setPlaceholderText("No routing result loaded")
        layout.addWidget(self.readout, 1)

    @property
    def selection(self) -> RoutingSelection:
        return self._selection

    def set_result(self, result: RoutingResult, selection=None):
        if not isinstance(result, RoutingResult):
            raise TypeError("result must be a RoutingResult")
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._result = result
        self._inspector = RoutingInspector(result)
        self._selection = (
            selection
            if isinstance(selection, RoutingSelection)
            else RoutingSelection(result)
        )
        if self._selection.calculation_id != result.calculation_id:
            raise ValueError("selection belongs to another calculation")
        self._unsubscribe = self._selection.subscribe(self._from_selection)
        self._fill_combos()
        self._diagnostics_text()
        self._from_selection()

    def _fill_combos(self):
        self._syncing = True
        try:
            self.layer_combo.clear()
            for layer in self._inspector.layers():
                self.layer_combo.addItem(layer.layer_id)
            self.leg_combo.clear()
            for leg in self._result.legs:
                self.leg_combo.addItem(leg.leg_id)
        finally:
            self._syncing = False

    def _from_selection(self):
        if self._selection is None:
            return
        self._syncing = True
        try:
            layer_id = self._selection.layer_id
            if layer_id is None and self.layer_combo.count():
                layer_id = self.layer_combo.itemText(0)
                self._selection.select_layer(layer_id)
            index = self.layer_combo.findText(layer_id or "")
            if index >= 0 and index != self.layer_combo.currentIndex():
                self.layer_combo.setCurrentIndex(index)
            leg_id = self._selection.leg_id
            leg_index = self.leg_combo.findText(leg_id or "")
            if leg_index >= 0 and leg_index != self.leg_combo.currentIndex():
                self.leg_combo.setCurrentIndex(leg_index)
        finally:
            self._syncing = False
        self._render_layer()
        self._render_leg()

    def select_nearest_layer(self, epoch_s: int) -> bool:
        if self._inspector is None or self._selection is None:
            return False
        layer = self._inspector.nearest_layer(epoch_s)
        if layer is None:
            return False
        self._selection.select_layer(layer.layer_id)
        return True

    def forecast_time_changed(self, epoch_s: int) -> bool:
        """Link a forecast-time change; only the selection moves."""
        return self.select_nearest_layer(epoch_s)

    def _layer_changed(self, index: int):
        if self._syncing or self._inspector is None or index < 0:
            return
        layers = self._inspector.layers()
        if index >= len(layers):
            return
        self._selection.select_layer(layers[index].layer_id)

    def _leg_changed(self, index: int):
        if self._syncing or self._inspector is None or index < 0:
            return
        if index >= len(self._result.legs):
            return
        self._selection.select_leg(self._result.legs[index].leg_id)

    def _render_layer(self):
        if self._inspector is None:
            return
        layers = self._inspector.layers()
        index = self.layer_combo.currentIndex()
        if index < 0 or index >= len(layers):
            self.time_label.setText("No layers recorded")
            return
        layer = layers[index]
        direction = "reverse" if layer.reverse else "forward"
        self.time_label.setText(
            f"{layer.time_utc}  (+{layer.offset_s} s, {direction}, "
            f"{layer.contour_count} contours)"
        )
        self.layer_selected.emit(layer.layer_id)

    def _render_leg(self):
        if self._inspector is None:
            return
        index = self.leg_combo.currentIndex()
        if index < 0 or index >= len(self._result.legs):
            self.readout.setPlainText("No delivered legs recorded")
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

    def _diagnostics_text(self):
        if self._result is None:
            self.diagnostics.setText("No routing result loaded")
            return
        outcome = self._result.outcome
        features = dict(self._result.capabilities.features or {})
        parts = [
            f"status: {self._result.status}",
            f"validation: {outcome.validation.state}"
            + (" (independent)" if outcome.validation.independent else ""),
        ]
        for name in ("inspection_geometry", "waves", "shoreline", "depth"):
            if name in features:
                parts.append(f"{name}: {features[name]}")
        parts.append("trace times: " + self._result.capabilities.trace_times)
        parts.append("inspection geometry is bounded and may be sampled")
        self.diagnostics.setText(" · ".join(parts))
