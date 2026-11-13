"""Isochrone inspector panel for a retained native routing result."""

from __future__ import annotations

import math
from collections.abc import Mapping

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from sailworks.core.marine.routinginspection import (
    InspectionContext,
    RoutingInspector,
    diagnostics,
)
from sailworks.core.marine.routingresult import RoutingResult
from sailworks.core.marine.routingselection import RoutingSelection
from .routingplot import RoutingLegPlot

READOUT_ORDER = (
    "heading_degrees",
    "course_over_ground_degrees",
    "speed_through_water_knots",
    "speed_over_ground_knots",
    "true_wind_speed_knots",
    "true_wind_angle_degrees",
    "wave_height_metres",
    "wave_period_seconds",
    "wave_direction_from_degrees",
    "propulsion_mode",
    "tack",
    "warning_count",
)


class RoutingInspectorPanel(QWidget):
    """Layer list, trace and leg readouts for one routing calculation."""

    layer_selected = Signal(str)
    leg_selected = Signal(str)
    alternative_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._result = None
        self._inspector = None
        self._selection = None
        self._unsubscribe = None
        self._syncing = False
        self._restore_missing = ()
        self._context_error = None
        self._layer_visibility = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)

        self.alternative_combo = QComboBox(self)
        self.alternative_combo.currentIndexChanged.connect(self._alternative_changed)
        form = QFormLayout()
        form.addRow("Alternative", self.alternative_combo)
        layout.addLayout(form)

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
        self.trace_label = QLabel("none", self)
        form.addRow("Pinned trace", self.trace_label)
        layout.addLayout(form)

        self.diagnostics = QLabel("No routing result loaded", self)
        self.diagnostics.setWordWrap(True)
        layout.addWidget(self.diagnostics)

        self.plot = RoutingLegPlot(self)
        layout.addWidget(self.plot)

        self.readout = QPlainTextEdit(self)
        self.readout.setReadOnly(True)
        self.readout.setPlaceholderText("No routing result loaded")
        layout.addWidget(self.readout, 1)

    @property
    def selection(self) -> RoutingSelection:
        return self._selection

    def set_result(
        self,
        result: RoutingResult,
        selection=None,
        context=None,
        context_error=None,
        layer_visibility=None,
    ):
        if not isinstance(result, RoutingResult):
            raise TypeError("result must be a RoutingResult")
        # Resolve and preflight the stored context before touching the current
        # result or its subscription. A context from another calculation is a
        # visible diagnostic, never a half-installed switch.
        if context is not None and not isinstance(context, InspectionContext):
            try:
                context = InspectionContext.from_dict(context)
            except ValueError as exc:
                context = None
                context_error = str(exc)
        if context is not None and context.calculation_id != result.calculation_id:
            context_error = (
                "stored context belongs to calculation "
                f"{context.calculation_id}; this result is {result.calculation_id}"
            )
            context = None
        next_selection = (
            selection
            if isinstance(selection, RoutingSelection)
            else RoutingSelection(result)
        )
        if next_selection.calculation_id != result.calculation_id:
            # Never apply one calculation's selection to another; keep that
            # selection untouched and report the mismatch instead.
            context_error = context_error or (
                "selection belongs to another calculation; installed a fresh one"
            )
            next_selection = RoutingSelection(result)
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._result = result
        self._inspector = RoutingInspector(result)
        self._layer_visibility = layer_visibility
        self._selection = next_selection
        self._restore_missing = ()
        self._context_error = str(context_error) if context_error is not None else None
        if context is not None:
            self._restore_missing = context.apply(self._selection)
        if context_error is not None:
            self._context_error = str(context_error)
        self._unsubscribe = self._selection.subscribe(self._from_selection)
        self.plot.set_result(result, self._selection)
        self._fill_combos()
        self._diagnostics_text()
        self._from_selection()

    def set_alternatives(self, labels, current_index=0):
        """List calculations without loading or recalculating any of them."""
        self._syncing = True
        try:
            self.alternative_combo.clear()
            self.alternative_combo.addItems([str(label) for label in labels])
            if not self.alternative_combo.count():
                return
            if current_index is None:
                self.alternative_combo.setCurrentIndex(-1)
                return
            self.alternative_combo.setCurrentIndex(
                max(0, min(int(current_index), self.alternative_combo.count() - 1))
            )
        finally:
            self._syncing = False

    def clear_result(self, message="No routing result loaded"):
        """Drop a previous result so no stale inspection data is left behind."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._result = None
        self._inspector = None
        self._selection = None
        self._restore_missing = ()
        self._context_error = None
        self._syncing = True
        try:
            self.layer_combo.clear()
            self.leg_combo.clear()
            self.time_label.setText("No layer")
            self.trace_label.setText("none")
        finally:
            self._syncing = False
        self.readout.setPlainText("")
        self.plot.clear()
        self.diagnostics.setText(str(message))

    def show_error(self, message):
        """Surface an unavailable artifact instead of stale inspection data."""
        self.clear_result(message)

    def pin_trace(self, trace_id):
        if self._selection is None:
            return False
        self._selection.select_trace(trace_id)
        return True

    @property
    def inspection_context(self):
        if self._selection is None:
            return None
        return InspectionContext.from_selection(self._selection)

    def _alternative_changed(self, index):
        if self._syncing or index < 0:
            return
        self.alternative_selected.emit(index)

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
            self.trace_label.setText(self._selection.trace_id or "none")
        finally:
            self._syncing = False
        self._render_layer()
        self._render_leg()

    def select_nearest_layer(self, epoch_s: int) -> bool:
        if self._inspector is None or self._selection is None:
            return False
        layer = self._inspector.nearest_layer(epoch_s, self._layer_visibility)
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
        lines.append(self._rounding_text(leg))
        self.readout.setPlainText("\n".join(lines))
        self.leg_selected.emit(leg.leg_id)

    @staticmethod
    def _distance_nm(left, right):
        latitude1, longitude1 = map(math.radians, (left[0], left[1]))
        latitude2, longitude2 = map(math.radians, (right[0], right[1]))
        dlat = latitude2 - latitude1
        dlon = longitude2 - longitude1
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(latitude1) * math.cos(latitude2) * math.sin(dlon / 2) ** 2
        )
        return 3440.065 * 2 * math.asin(math.sqrt(max(0.0, min(1.0, a))))

    def _rounding_text(self, leg):
        for report in self._result.provenance.roundings:
            mark = report.get("mark")
            if not isinstance(mark, Mapping):
                continue
            radius = report.get("radius_nm")
            if radius is None:
                continue
            if (
                self._distance_nm(
                    (leg.end.lat, leg.end.lon), (mark["latitude"], mark["longitude"])
                )
                > float(radius) + 1e-3
            ):
                continue
            side = report.get("side", "unknown")
            achieved = report.get("achieved_closest_distance_nm")
            terminal = report.get("achieved_terminal_course_deg")
            satisfied = report.get("satisfied")
            failure = report.get("failure_code", "unknown")
            name = report.get("mark_name")
            prefix = f"{name} — " if name else ""
            text = f"rounding: {prefix}{side} {float(radius):.2f} nm"
            if achieved is not None:
                text += f", achieved {float(achieved):.2f} nm"
            if terminal is not None:
                text += f", terminal {float(terminal):.1f}°"
            if not satisfied:
                text += f" (not satisfied: {failure})"
            return text
        return "rounding: exact waypoint"

    def _diagnostics_text(self):
        if self._result is None:
            self.diagnostics.setText("No routing result loaded")
            return
        lines = [f"{state.code}: {state.state}" for state in diagnostics(self._result)]
        roundings = self._result.provenance.roundings
        if roundings:
            lines.append("")
            lines.append("Mark roundings:")
            for report in roundings:
                name = report.get("mark_name")
                mark = report.get("mark")
                if not name and isinstance(mark, Mapping):
                    latitude = mark.get("latitude")
                    longitude = mark.get("longitude")
                    if latitude is not None and longitude is not None:
                        name = f"{float(latitude):.4f}, {float(longitude):.4f}"
                name = name or "mark"
                side = report.get("side", "unknown")
                text = f"{name}: {side}"
                radius = report.get("radius_nm")
                if radius is not None:
                    text += f", radius {float(radius):.2f} nm"
                achieved = report.get("achieved_closest_distance_nm")
                if achieved is not None:
                    text += f", achieved {float(achieved):.2f} nm"
                course = report.get("achieved_terminal_course_deg")
                if course is not None:
                    text += f", terminal {float(course):.1f}°"
                if not report.get("satisfied", False):
                    text += (
                        " (not satisfied: "
                        + str(report.get("failure_code", "unknown"))
                        + ")"
                    )
                lines.append("  " + text)
        if self._restore_missing:
            lines.append(
                "inspection context: stored "
                + ", ".join(self._restore_missing)
                + " no longer recorded by this result"
            )
        if self._context_error:
            lines.append(f"inspection context: {self._context_error}")
        self.diagnostics.setText("\n".join(lines))
