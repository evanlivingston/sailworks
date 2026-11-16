"""Controls for the Historical years ensemble routing mode.

The panel owns no routing state: it collects the requested member grid, the
acquisition region and the shared target-date current policy for Calculate
route to run. Validation mirrors the pure
rules in :mod:`sailworks.core.historicalrouting` so the panel rejects a
specification the engine would reject anyway.
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from sailworks.core.historicalrouting import (
    DEFAULT_HORIZON_HOURS,
    DEFAULT_OFFSET_DAYS,
    DEFAULT_YEARS,
    HORIZON_STEP_HOURS,
    MAX_HORIZON_HOURS,
    MAX_MEMBERS,
    MAX_OFFSET_DAYS,
    MAX_YEARS,
    MIN_HORIZON_HOURS,
    MIN_OFFSET_DAYS,
    MIN_YEARS,
)

from .dropdown import ToggleFieldRow
from .historicalroutingprogress import HistoricalProgress
from .slider import Slider


def _caption(text):
    label = QLabel(text)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


class HistoricalRoutingOptions(QWidget):
    """Inputs for historical-year ensemble routing."""

    values_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._available = True
        self._available_reason = ""
        self._running = False
        self._build()

    # -- construction ----------------------------------------------------
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.years = Slider()
        self.years.setDecimals(0)
        self.years.setRange(MIN_YEARS, MAX_YEARS)
        self.years.setSingleStep(1)
        self.years.setValue(DEFAULT_YEARS)
        self.years.setAccessibleName("Years")
        self.years.setToolTip(
            "Number of calendar years replayed, ending at the departure year."
        )
        self.years.valueChanged.connect(lambda *_: self.values_changed.emit())
        layout.addWidget(ToggleFieldRow("Years", self.years, checkbox=False))

        self.offset_days = Slider()
        self.offset_days.setDecimals(0)
        self.offset_days.setRange(MIN_OFFSET_DAYS, MAX_OFFSET_DAYS)
        self.offset_days.setSingleStep(1)
        self.offset_days.setValue(DEFAULT_OFFSET_DAYS)
        self.offset_days.setAccessibleName("Day offset radius")
        self.offset_days.setToolTip(
            "Days before and after the departure date to test in each year."
        )
        self.offset_days.valueChanged.connect(lambda *_: self.values_changed.emit())
        layout.addWidget(ToggleFieldRow("± days", self.offset_days, checkbox=False))

        self.max_duration = Slider()
        self.max_duration.setDecimals(0)
        self.max_duration.setRange(MIN_HORIZON_HOURS, MAX_HORIZON_HOURS)
        self.max_duration.setSingleStep(HORIZON_STEP_HOURS)
        self.max_duration.setSuffix(" h")
        self.max_duration.setValue(DEFAULT_HORIZON_HOURS)
        self.max_duration.setAccessibleName("Max passage hours")
        self.max_duration.setToolTip("Elapsed route time limit, in six-hour steps.")
        self.max_duration.valueChanged.connect(self._horizon_changed)
        layout.addWidget(
            ToggleFieldRow("Max passage", self.max_duration, checkbox=False)
        )

        self.use_currents = QCheckBox("Target-date currents (shared)")
        self.use_currents.setAccessibleName("Target-date currents (shared)")
        self.use_currents.setToolTip(
            "Every member uses currents from the selected target departure and its timeline."
        )
        self.use_currents.toggled.connect(lambda *_: self.values_changed.emit())
        layout.addWidget(self.use_currents)

        self.estimate = QLabel()
        self.estimate.setWordWrap(True)
        self.estimate.setAccessibleName("Estimate")
        self.estimate.setVisible(False)
        layout.addWidget(self.estimate)

        self.preflight_label = _caption("Preflight")
        self.preflight_label.setVisible(False)
        self.preflight_label.setAccessibleName("Preflight")
        layout.addWidget(self.preflight_label)

        self.preflight = QPlainTextEdit()
        self.preflight.setReadOnly(True)
        self.preflight.setAccessibleName("Preflight details")
        self.preflight.setMaximumHeight(96)
        self.preflight.setVisible(False)
        layout.addWidget(self.preflight)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Status")
        self.status.setVisible(False)
        layout.addWidget(self.status)

        self.progress_widget = HistoricalProgress()
        self.progress_widget.setAccessibleName("Historical routing progress")
        layout.addWidget(self.progress_widget)
        layout.addStretch(1)

    # -- values ----------------------------------------------------------
    def values(self):
        """The ensemble inputs. Region and current priority come from Plan."""
        return {
            "years": int(self.years.value()),
            "offset_days": int(self.offset_days.value()),
            "max_duration_hours": int(self.max_duration.value()),
            "use_currents": self.use_currents.isChecked(),
        }

    def set_values(self, values):
        widgets = (self.years, self.offset_days, self.max_duration, self.use_currents)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            if "years" in values:
                self.years.setValue(int(values["years"]))
            if "offset_days" in values:
                self.offset_days.setValue(int(values["offset_days"]))
            if "max_duration_hours" in values:
                self.max_duration.setValue(int(values["max_duration_hours"]))
            if "use_currents" in values:
                self.use_currents.setChecked(bool(values["use_currents"]))
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        self.values_changed.emit()

    # -- feedback --------------------------------------------------------
    def set_estimate(self, text):
        self.estimate.setText(text or "")
        self.estimate.setVisible(bool(text))

    def set_preflight(self, items):
        items = list(items or [])
        self.preflight.setPlainText("\n".join(str(item) for item in items))
        self.preflight.setVisible(bool(items))
        self.preflight_label.setVisible(bool(items))

    def set_status(self, text):
        self.status.setText(text or "")
        self.status.setVisible(bool(text))

    def set_progress_probe(self, probe):
        """Install the snapshot callable the progress widget samples."""
        self.progress_widget.set_probe(probe)

    def set_weather_progress(self, completed, total, message=""):
        """Show acquisition progress as weather units."""
        self.progress_widget.set_weather_phase(completed, total, message)

    def set_running(self, running):
        self._running = bool(running)
        if self._running:
            self.progress_widget.start()
        else:
            self.progress_widget.stop()

    def set_available(self, available, reason=""):
        self._available = bool(available)
        self._available_reason = reason or ""

    # -- validation ------------------------------------------------------
    def validation_message(self):
        """A problem with the current inputs, or ``""`` when they are usable."""
        values = self.values()
        count = values["years"] * (2 * values["offset_days"] + 1)
        if count > MAX_MEMBERS:
            return f"{count} members exceeds the {MAX_MEMBERS}-member limit."
        if values["max_duration_hours"] % HORIZON_STEP_HOURS:
            return f"max duration hours must be a multiple of {HORIZON_STEP_HOURS}."
        return ""

    # -- internals -------------------------------------------------------
    def _horizon_changed(self, value):
        snapped = round(value / HORIZON_STEP_HOURS) * HORIZON_STEP_HOURS
        snapped = min(MAX_HORIZON_HOURS, max(MIN_HORIZON_HOURS, snapped))
        if snapped != value:
            self.max_duration.blockSignals(True)
            self.max_duration.setValue(snapped)
            self.max_duration.blockSignals(False)
        self.values_changed.emit()
